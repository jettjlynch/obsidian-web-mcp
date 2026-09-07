"""OAuth 2.0 authorization code flow with PKCE for Claude app MCP integration.

Claude's MCP connector uses the full OAuth authorization code flow:
1. Discovers metadata at /.well-known/oauth-authorization-server
2. Dynamically registers at /oauth/register (or uses pre-configured credentials)
3. Redirects user's browser to /oauth/authorize
4. Server shows a PIN-entry consent page; on a correct PIN it issues an auth code
   and redirects back
5. Claude exchanges the code at /oauth/token for a bearer token
6. Claude uses the bearer token on all MCP requests

/oauth/authorize used to auto-approve immediately (no login, no consent page) --
fine only while the server's hostname was secret. Once it moved behind a public
Cloudflare Tunnel hostname (Certificate-Transparency-logged, i.e. discoverable),
that meant anyone who found the hostname could complete register -> authorize ->
token and obtain the real bearer token in two unauthenticated requests -- PKCE
alone doesn't stop this since an attacker controls both ends of it. Fixed
2026-08-17 (see VAULT_OAUTH_AUTHORIZE_PIN in config.py): /oauth/authorize now
requires a PIN, entered once per client, before a code is ever issued.

That closed the auto-approve hole but NOT the rest of SECURITY.md's C-1/M-1:
redirect_uri was still unvalidated (open redirect -- a PIN-holder could still
be steered into approving a code sent to an attacker's URI) and every
successful flow still handed back the one shared static VAULT_MCP_TOKEN.
Closed 2026-08-30 (S1): /oauth/authorize now checks client_id + redirect_uri
against a real per-client exact-match allowlist (see the client registry
below) before ever showing the PIN form, and the token endpoint issues a
fresh per-client random token instead of the static one. The static token
remains valid for already-issued/out-of-band consumers until S2 finishes the
token split -- see OPERATIONS.md.

2026-09-05 (C-2): S1 closed *how* a token is minted but not *what it grants*
-- every per-client token issued above was still handed the exact same
access as the static one: vault_write/append/delete/move/etc were all
reachable by anything that authenticated at all. The consent page now
carries a write-access checkbox (default UNCHECKED) alongside the PIN;
issued tokens carry a "read" or "write" scope accordingly, and auth.py
threads the granted scope down to audit.py's tool-wrapper choke point (same
placement rate-limiting already uses, and for the same reason -- see
token_scope.py) which denies write-class tools to read-scope tokens. Full
detail: OPERATIONS.md's 2026-09-05 section, SECURITY.md's C-2 entry.

2026-09-05 (C-2 follow-up, same day): the above was live-ineffective. The
static token from the previous paragraph was STILL accepted (grandfathered
to unconditional "write") alongside per-client ones -- and claude.ai's
connector turned out to have been authenticating with exactly that static
token since 2026-08-17, before per-client tokens even existed, so every
real write sailed past the new scope gate regardless of the consent-page
checkbox. Fixed: get_token_scope() no longer recognizes the static token at
all (see its docstring for what was checked before removing it, and why
this is a narrow slice of H-2/S2, not the full bind-fix-and-secret-split).
The client_credentials grant -- which handed back that same static token --
was removed outright rather than left issuing tokens that would now 401 on
first use; confirmed nothing legitimate used it first (its docstring on
oauth_token has the evidence). A real /health route was added the same day
so mcp_watchdog.sh's automated probe -- previously a hidden consumer of the
static token itself -- no longer needs any credential at all.

2026-09-07 (H-2's phone-rollout gap, found the day after app-bridge's own
BRIDGE_TOKEN split): app-bridge (separate process/repo, port 8421) has
never recognized the vault-audience access_token this endpoint issues --
neither the legacy static VAULT_MCP_TOKEN nor app-bridge's new BRIDGE_TOKEN
match it, so jarvis-app's bridge.ts calls have likely 401'd since S1
(2026-08-30). PLAN.md's S2 spec always called for this endpoint to mint a
bridge-scoped token as part of the same flow ("the OAuth server mints
bridge-scoped tokens... public MCP token != bridge token") -- that part
was simply never built. Fixed: _issue_token gained an explicit `audience`
("vault"/"bridge"), this endpoint now mints and returns both in one
response (`access_token` unchanged, new `bridge_token` field), and
get_issued_token_scope/get_token_scope now only honor "vault"-audience
entries (a bridge token presented here doesn't authenticate at all,
preserving "public MCP token != bridge token" even though both live in the
same _issued_tokens store). App-bridge validates its half via a new,
side-effect-free token_store.py -- see that module's docstring for why it
does NOT reuse this file's in-memory _issued_tokens dict directly (a real
cross-process staleness bug, caught before shipping, not guessed at).
"""

import hashlib
import hmac
import html as _html
import json
import logging
import os
import secrets
import stat
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, HTMLResponse
from starlette.routing import Route

from . import config

logger = logging.getLogger(__name__)

# In-memory store for authorization codes (short-lived)
# Maps code -> {client_id, redirect_uri, code_challenge, code_challenge_method, expires_at}
_auth_codes: dict[str, dict] = {}

# Clean up expired codes periodically
def _cleanup_codes():
    now = time.time()
    expired = [k for k, v in _auth_codes.items() if v["expires_at"] < now]
    for k in expired:
        del _auth_codes[k]


# --- Client registry + per-client token store (S1, 2026-08-30) -----------
#
# Closes the rest of C-1/M-1: the 2026-08-17 PIN gate stops an unauthenticated
# caller from ever reaching approval, but /oauth/authorize still redirected to
# whatever redirect_uri was supplied, unvalidated (open redirect), and the
# token endpoint still handed back the one shared static VAULT_MCP_TOKEN to
# any successful flow. This registry gives /oauth/authorize a real per-client
# exact-match allowlist to check redirect_uri against, and lets the token
# endpoint issue a fresh random token per client instead.
#
# Two JSON files beside the server (config.OAUTH_STATE_DIR), 0600, gitignored:
#   oauth_clients.json:  client_id -> {redirect_uris: [...], client_name}
#   oauth_tokens.json:   access_token -> {client_id, expires_at, scope}
#                         scope is "read" or "write" (C-2, 2026-09-05).

_CLIENTS_FILE = config.OAUTH_STATE_DIR / "oauth_clients.json"
_TOKENS_FILE = config.OAUTH_STATE_DIR / "oauth_tokens.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        logger.warning(f"OAuth state file {path} unreadable/corrupt -- starting from empty")
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, matches jjvault/.env convention


_registered_clients: dict[str, dict] = _load_json(_CLIENTS_FILE)
_issued_tokens: dict[str, dict] = _load_json(_TOKENS_FILE)


def _purge_unscoped_tokens() -> int:
    """C-2 migration (2026-09-05): tokens issued by S1 (2026-08-30) through
    today have no "scope" key -- they predate the read/write split entirely.

    Explicit decision, not a default worth burying in a comment: these are
    NOT grandfathered into write access. That would silently hand full
    access to whatever happened to hold a token at deploy time, which is
    exactly the kind of silent-grant this task exists to close. Instead they
    are invalidated outright -- the holder's next request 401s, which for
    both real clients (jarvis-app, claude.ai's connector) means transparently
    re-running the existing PKCE + PIN flow and getting back a real, scoped
    token. Cost is bounded: tokens are already TTL'd at 24h, so this is at
    most one extra unscheduled re-auth, not a standing outage.

    Returns the number of entries purged (0 on a fresh store or one that's
    already fully migrated) -- called once at import time below, and exposed
    here as its own function so tests can exercise the migration without
    needing to reimport the module.
    """
    unscoped = [t for t, v in _issued_tokens.items() if "scope" not in v]
    if not unscoped:
        return 0
    logger.warning(
        f"Token-scope migration: purging {len(unscoped)} pre-scope issued "
        f"token(s) issued before the C-2 read/write split -- holders must "
        f"re-authorize to get a scoped token."
    )
    for t in unscoped:
        del _issued_tokens[t]
    _save_json(_TOKENS_FILE, _issued_tokens)
    return len(unscoped)


_purge_unscoped_tokens()

# Access-token lifetime for tokens issued by the authorization_code grant.
# "Real lifetime, not indefinite" per the task spec -- 24h rather than a more
# aggressive 15-60min SHOULD-tier value from SECURITY.md §5, because
# jarvis-app's auth.ts has no refresh_token grant: expiry means re-running the
# full PKCE flow, which now means re-entering the PIN. A short TTL would mean
# Jett re-entering his PIN multiple times a day on his own phone for a
# single-user personal app; 24h closes the literal "not indefinite" gap
# without that daily-use regression. Flagging this trade-off for Jett rather
# than silently picking a number -- tighten if he wants stronger rotation
# (S2/H-2 already covers reuse-detection separately).
_TOKEN_TTL_SECONDS = 86400


def _register_client(client_id: str, redirect_uris: list[str], client_name: str = "") -> None:
    _registered_clients[client_id] = {
        "redirect_uris": [u for u in redirect_uris if u],
        "client_name": client_name,
    }
    _save_json(_CLIENTS_FILE, _registered_clients)


def _redirect_uri_allowed(client_id: str, redirect_uri: str) -> bool:
    client = _registered_clients.get(client_id)
    return bool(client) and redirect_uri in client["redirect_uris"]


def _cleanup_tokens() -> None:
    now = time.time()
    expired = [t for t, v in _issued_tokens.items() if v["expires_at"] < now]
    if expired:
        for t in expired:
            del _issued_tokens[t]
        _save_json(_TOKENS_FILE, _issued_tokens)


def _issue_token(client_id: str, scope: str, audience: str) -> str:
    """`scope` and `audience` are required, not defaulted -- every call site
    (there are two now, both in _handle_authorization_code below) must say
    explicitly what it's granting rather than relying on an implicit default
    that could silently change meaning later. The default-to-read-unless-
    approved *policy* lives in oauth_authorize's consent-form handling, not
    here.

    `audience` (2026-09-07, H-2's phone-rollout gap): "vault" for a token
    meant to authenticate against THIS server's own MCP tools, "bridge" for
    one meant for app-bridge (a separate process/repo, port 8421). Both live
    in the SAME _issued_tokens store/file -- the isolation between them is
    audience-checked at read time (get_issued_token_scope only ever returns
    a scope for "vault" tokens; app-bridge's token_store.lookup_issued_token
    is audience-agnostic by design, since only app-bridge calls it and it
    checks audience=="bridge" itself), not enforced by keeping separate
    files. This is deliberate: PLAN.md's S2 spec calls for "the OAuth server
    mints bridge-scoped tokens... a shared verification approach" -- one
    token format/store, checked fresh by whichever process needs to, rather
    than two disconnected credential systems.
    """
    if scope not in ("read", "write"):
        raise ValueError(f"invalid token scope: {scope!r}")
    if audience not in ("vault", "bridge"):
        raise ValueError(f"invalid token audience: {audience!r}")
    _cleanup_tokens()
    token = secrets.token_urlsafe(32)
    _issued_tokens[token] = {
        "client_id": client_id,
        "expires_at": time.time() + _TOKEN_TTL_SECONDS,
        "scope": scope,
        "audience": audience,
    }
    _save_json(_TOKENS_FILE, _issued_tokens)
    return token


def is_valid_issued_token(token: str) -> bool:
    """Is `token` a currently-valid per-client issued token (NOT the legacy
    static token -- that's a separate check, see get_token_scope). Used by
    tests to confirm a token came from the real per-client issuance path.
    """
    entry = _issued_tokens.get(token)
    return bool(entry) and entry["expires_at"] >= time.time()


def get_issued_token_scope(token: str) -> str | None:
    """Scope of a currently-valid per-client issued VAULT-audience token, or
    None if it's unknown/expired/missing (including a pre-migration entry
    that somehow survived _purge_unscoped_tokens() -- treated as ungranted,
    fail closed, rather than assumed) OR a bridge-audience token presented
    here (2026-09-07 -- see _issue_token's docstring on why both audiences
    share one store: this is the audience check that keeps a leaked/misused
    bridge token from also authenticating against this server's own vault
    MCP tools). A missing "audience" key is treated as "vault" -- entries
    issued before this field existed predate the split entirely and were
    always vault-only; no need to invalidate live sessions over adding a
    field (unlike the scope-purge above, which was invalidating for a real
    security reason, not just schema hygiene).
    """
    _cleanup_tokens()
    entry = _issued_tokens.get(token)
    if not entry or entry["expires_at"] < time.time():
        return None
    if entry.get("audience", "vault") != "vault":
        return None
    return entry.get("scope")


def get_token_scope(token: str) -> str | None:
    """Scope granted to `token` -- "read", "write", or None if it doesn't
    authenticate at all. This is what auth.py's bearer middleware calls.

    2026-09-05 (C-2 follow-up): the legacy static VAULT_MCP_TOKEN NO LONGER
    authenticates against this server at all. Until this change it was
    grandfathered here to unconditional "write" -- which turned out to make
    the entire C-2 scope split live-ineffective: claude.ai's connector had
    been authenticating with this exact static token since 2026-08-17
    (before per-client tokens even existed), so every write it made sailed
    straight past the new scope gate as "write" by design, regardless of
    the consent-page checkbox. This is the narrow fix for that -- forcing
    every real client onto the scoped per-client path C-2 already built.
    NOT the full H-2/S2 (bridge bind fix + cross-domain secret split,
    separate findings, still open) -- this only removes the static token's
    authority *against this server*. The app-bridge process (port 8421,
    separate codebase entirely) independently compares incoming requests
    against its own copy of this same secret value and is untouched by this
    change -- verified, not assumed (see OPERATIONS.md's 2026-09-05 §2
    entry). Before removing this, confirmed (grepping this server's own
    retained logs, jarvis-app's source, and this repo's docs) that nothing
    else legitimately authenticates against *this* server with the static
    token: jarvis-app's auth.ts, claude.ai's connector, and (as of this
    same change) mcp_watchdog.sh's health probe all now use, or already
    used, something other than this path.
    """
    return get_issued_token_scope(token)


# jarvis-app never calls /oauth/register -- its own auth.ts hardcodes
# client_id='jarvis-app' and goes straight to /oauth/authorize. Pre-register
# it here so the allowlist check below has something to check it against.
if config.VAULT_OAUTH_STATIC_CLIENT_ID not in _registered_clients:
    _register_client(
        config.VAULT_OAUTH_STATIC_CLIENT_ID,
        config.VAULT_OAUTH_REDIRECT_URIS,
        "jarvis-app (static, pre-registered)",
    )


async def oauth_metadata(request: Request) -> JSONResponse:
    """RFC 8414 OAuth authorization server metadata."""
    base_url = config.advertised_base_url(str(request.base_url))
    return JSONResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "grant_types_supported": ["authorization_code"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    })


def _consent_form_html(params: dict, error: str | None = None, requested_scope: str = "read") -> str:
    """Minimal PIN-entry consent page. Renders fine inside claude.ai's browser
    redirect and jarvis-app's ASWebAuthenticationSession alike -- both are real
    interactive browser contexts, this is a normal OAuth consent screen.

    All param values are attacker-influenceable (they come straight off the
    incoming request) and get echoed back as hidden fields, so they're
    HTML-escaped before embedding.

    C-2 (2026-09-05): the write-access checkbox is what makes write scope
    "explicitly requested/approved" rather than silently granted -- it's
    UNCHECKED by default regardless of what the client asked for.
    `requested_scope` only pre-checks the box as a convenience when the
    client's authorize request hinted scope=write; the human still has to
    see it checked and submit with the correct PIN for it to mean anything.
    Nothing about the granted scope is trusted from a hidden field -- the
    checkbox's on-submit state is the only input read back (see
    oauth_authorize's POST branch).
    """
    hidden = "\n".join(
        f'<input type="hidden" name="{_html.escape(k)}" value="{_html.escape(v or "")}">'
        for k, v in params.items()
    )
    error_html = f'<p style="color:#c00">{_html.escape(error)}</p>' if error else ""
    write_checked = "checked" if requested_scope == "write" else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize Vault Access</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 380px;
        margin: 4rem auto; padding: 0 1rem; }}
input[type=password] {{ width: 100%; padding: 0.6rem; font-size: 1rem; box-sizing: border-box;
                         margin-top: 0.75rem; }}
label.scope {{ display: flex; align-items: flex-start; gap: 0.5rem; margin-top: 1rem;
               font-size: 0.85rem; line-height: 1.3; }}
label.scope input {{ margin-top: 0.2rem; }}
button {{ width: 100%; margin-top: 1rem; padding: 0.7rem; font-size: 1rem; }}
</style></head>
<body>
<h2>Authorize vault access</h2>
<p>A client is requesting access to your Obsidian vault. Enter your PIN to approve.</p>
{error_html}
<form method="POST">
{hidden}
<label class="scope">
<input type="checkbox" name="scope" value="write" {write_checked}>
<span>Also allow <strong>write access</strong> (create, edit, delete, and move notes). Unchecked grants read-only.</span>
</label>
<input type="password" name="pin" placeholder="PIN" autofocus required autocomplete="off">
<button type="submit">Approve</button>
</form>
</body></html>"""


async def oauth_authorize(request: Request):
    """OAuth 2.0 authorization endpoint.

    GET renders a PIN-entry consent form carrying the original OAuth params as
    hidden fields. POST verifies the PIN and, only then, issues an auth code and
    redirects back to the client -- see module docstring for why this gate exists.
    """
    params = {
        "response_type": request.query_params.get("response_type", ""),
        "client_id": request.query_params.get("client_id", ""),
        "redirect_uri": request.query_params.get("redirect_uri", ""),
        "state": request.query_params.get("state", ""),
        "code_challenge": request.query_params.get("code_challenge", ""),
        "code_challenge_method": request.query_params.get("code_challenge_method", "S256"),
    }

    if request.method == "GET":
        # Exact-match redirect_uri/client_id check before ever showing the
        # PIN form -- no reason to prompt Jett for his PIN on a request that
        # could never succeed. Query-string only: GET is the only method
        # where these values actually live in the query string (a form POST
        # doesn't resend it -- see the POST branch below), so this check
        # must NOT run unconditionally for both methods or it would 400
        # every real POST before ever reading the form body.
        if not params["redirect_uri"] or not _redirect_uri_allowed(params["client_id"], params["redirect_uri"]):
            logger.warning(
                f"OAuth authorize: redirect_uri not allowed for client_id={params['client_id']!r} "
                f"-- returning error, NOT showing the PIN form"
            )
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri not registered for this client_id"},
                status_code=400,
            )
        # C-2: a client MAY hint scope=write in the query string to pre-check
        # the consent-page box (pure UX -- e.g. so claude.ai's connector
        # doesn't force Jett to hunt for the checkbox every time). This hint
        # is never trusted as the granted scope by itself; only the box's
        # state on actual form submission is (see the POST branch).
        requested_scope = "write" if request.query_params.get("scope") == "write" else "read"
        return HTMLResponse(_consent_form_html(params, requested_scope=requested_scope))

    # POST: params travel back as hidden fields (the browser doesn't resend
    # the original query string on form submission).
    form = await request.form()
    for key in params:
        params[key] = form.get(key, params[key])
    pin = form.get("pin", "")
    # C-2: unchecked checkboxes are simply absent from form data (standard
    # HTML behavior) -- form.get("scope") is None unless the box was ticked,
    # so this defaults to "read" with no separate default branch needed.
    scope = "write" if form.get("scope") == "write" else "read"

    # Re-verify against the (now form-sourced) params -- see comment above.
    if not params["redirect_uri"] or not _redirect_uri_allowed(params["client_id"], params["redirect_uri"]):
        logger.warning("OAuth authorize: redirect_uri/client_id failed re-check on POST -- code NOT issued")
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri not registered for this client_id"},
            status_code=400,
        )

    if not config.VAULT_OAUTH_AUTHORIZE_PIN or not hmac.compare_digest(pin, config.VAULT_OAUTH_AUTHORIZE_PIN):
        logger.warning("OAuth authorize: incorrect or missing PIN -- code NOT issued")
        return HTMLResponse(
            _consent_form_html(params, error="Incorrect PIN.", requested_scope=scope), status_code=401
        )

    response_type = params["response_type"]
    redirect_uri = params["redirect_uri"]
    state = params["state"]

    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    # Generate authorization code
    _cleanup_codes()
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": params["client_id"],
        "redirect_uri": redirect_uri,
        "code_challenge": params["code_challenge"],
        "code_challenge_method": params["code_challenge_method"],
        "scope": scope,
        "expires_at": time.time() + 300,  # 5 minute expiry
    }

    logger.info(
        f"OAuth authorization code issued (PIN verified, scope={scope!r}), "
        f"redirecting to {redirect_uri[:50]}..."
    )

    # Redirect back to Claude with the code
    out = {"code": code}
    if state:
        out["state"] = state

    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{separator}{urlencode(out)}",
        status_code=302,
    )


async def oauth_token(request: Request) -> JSONResponse:
    """OAuth 2.0 token endpoint -- authorization code grant with PKCE.

    client_credentials grant REMOVED 2026-09-05 (C-2 follow-up, checked
    before removing per the task's own instruction, not assumed): the only
    trace of it anywhere in this server's retained logs is a single FAILED
    attempt (2026-08-18 08:16:44, tied to the register-leak/PIN-gate-bypass
    incident that day's emergency fix closed) -- zero successful
    "OAuth token issued via client_credentials grant" log lines exist,
    ever. No legitimate client uses it: jarvis-app's auth.ts and claude.ai's
    registered connector both go through authorization_code exclusively,
    and this server's own oauth_metadata() has only ever advertised
    ["authorization_code"] in grant_types_supported -- client_credentials
    was never even a documented feature. It also handed back the raw static
    VAULT_MCP_TOKEN, which would 401 on first real use once the static
    token stops authenticating at all (see get_token_scope) -- a grant that
    "succeeds" but issues a dead token is worse than one that fails
    honestly. Removed outright rather than left to silently rot. Detail:
    OPERATIONS.md's 2026-09-05 section.
    """
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = form.get("grant_type", "")

    if grant_type == "authorization_code":
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")
        return await _handle_authorization_code(form, client_id, client_secret)
    elif grant_type == "client_credentials":
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": (
                    "client_credentials is disabled on this server -- use "
                    "authorization_code (PKCE)."
                ),
            },
            status_code=400,
        )
    else:
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )


async def _handle_authorization_code(form, client_id: str, client_secret: str) -> JSONResponse:
    """Exchange an authorization code for a bearer token."""
    code = form.get("code", "")
    redirect_uri = form.get("redirect_uri", "")
    code_verifier = form.get("code_verifier", "")

    _cleanup_codes()

    if code not in _auth_codes:
        return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired code"}, status_code=400)

    code_data = _auth_codes.pop(code)

    # Verify redirect_uri matches
    if redirect_uri and code_data["redirect_uri"] and redirect_uri != code_data["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

    # Verify PKCE code_challenge if one was provided during authorization
    if code_data["code_challenge"]:
        if not code_verifier:
            return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier required"}, status_code=400)

        # S256: BASE64URL(SHA256(code_verifier)) must match code_challenge
        import base64
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        if not hmac.compare_digest(computed_challenge, code_data["code_challenge"]):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

    # Per-client random token (S1, 2026-08-30) -- never the shared static
    # VAULT_MCP_TOKEN. auth.py's bearer middleware accepts this alongside the
    # static token, which stays valid for already-issued/out-of-band
    # consumers until S2 finishes the token split (see OPERATIONS.md).
    # Scope (C-2, 2026-09-05) is whatever was approved on the consent page --
    # "read" unless the write checkbox was ticked; see oauth_authorize.
    scope = code_data["scope"]
    access_token = _issue_token(code_data["client_id"], scope, audience="vault")

    # bridge_token (2026-09-07, H-2's phone-rollout gap): PLAN.md's S2 spec
    # always called for "the OAuth server mints bridge-scoped tokens" as
    # part of this same flow -- this was simply never implemented, which is
    # why the phone's bridge.ts calls have been sending its vault-audience
    # access_token to app-bridge since S1 (2026-08-30) and getting 401s
    # (neither the legacy VAULT_MCP_TOKEN nor app-bridge's own BRIDGE_TOKEN
    # recognize it, by design). Minted in the SAME response as the vault
    # token -- one PKCE flow, two distinct token values ("public MCP token
    # != bridge token" per PLAN.md's own constraint), not two separate
    # browser round trips. `bridge_token` is not part of RFC 6749; jarvis-app's
    # auth.ts reads it off expo-auth-session's TokenResponse.rawResponse
    # (see that file's comment for why that's the correct, verified-not-
    # assumed way to read a non-standard field from that library).
    bridge_token = _issue_token(code_data["client_id"], scope, audience="bridge")

    logger.info(
        f"OAuth tokens issued via authorization_code grant "
        f"(client_id={code_data['client_id']!r}, scope={scope!r}, vault+bridge)"
    )
    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": _TOKEN_TTL_SECONDS,
        "scope": scope,
        "bridge_token": bridge_token,
    })



async def oauth_protected_resource(request: Request) -> JSONResponse:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata.

    MCP clients (claude.ai) fetch this before/alongside the authorization-server
    metadata to confirm which auth server protects this resource. Must be
    publicly reachable (see auth.py's _AUTH_EXEMPT_PATHS) -- previously missing
    entirely, which 401'd via the bearer-auth middleware instead of ever
    reaching a real route. Root-caused 2026-07-14 after a Cloudflare Tunnel
    hostname migration surfaced it as a hard connector failure ("no MCP server
    was found at the provided URL").

    2026-09-06: base_url now goes through config.advertised_base_url() (see
    that function's docstring) instead of raw request.base_url -- merged in
    from jimprosser/obsidian-web-mcp's independent fix for the same class of
    bug (upstream commit 669775a). uvicorn trusted X-Forwarded-Host from ANY
    client (forwarded_allow_ips="*"), and this endpoint's advertised
    "resource"/"authorization_servers" URLs were derived straight from that
    spoofable header -- a caller could steer OAuth discovery toward an
    attacker-controlled authorization server. Fixed alongside
    VAULT_MCP_FORWARDED_ALLOW_IPS (now defaults to loopback, not "*") and
    oauth_metadata's matching use of the same helper, above.
    """
    base_url = config.advertised_base_url(str(request.base_url))
    suffix = request.url.path[len("/.well-known/oauth-protected-resource"):]
    return JSONResponse({
        "resource": base_url + suffix,
        "authorization_servers": [base_url],
    })


async def oauth_register(request: Request) -> JSONResponse:
    """Dynamic client registration endpoint.

    Claude calls this during initial setup to register as an OAuth client.
    Returns pre-configured credentials.

    Emergency fix 2026-08-18: this used to return config.VAULT_OAUTH_CLIENT_SECRET
    -- the real, shared secret -- to ANY unauthenticated caller, since /oauth/register
    carries no auth gate (see auth.py's _AUTH_EXEMPT_PATHS). Combined with
    grant_type=client_credentials at /oauth/token (which only checks that secret,
    never the /oauth/authorize PIN), that was a full bypass of the PIN gate added
    earlier the same day: register -> token in 2 unauthenticated requests yielded the
    real VAULT_MCP_TOKEN, the exact hole the PIN gate was meant to close. Root-caused
    while merging in jimprosser/obsidian-web-mcp's independent fix for the same class
    of bug (upstream commit e4924f0), which generates a per-client secret instead --
    matched here. The client_secret returned here is unused by any code path in this
    file (authorization_code doesn't check it; client_credentials only accepts the
    real config.VAULT_OAUTH_CLIENT_SECRET, known only to whoever configured it
    out-of-band) -- it exists only because the DCR response shape requires the field.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Generate a unique client_id for this registration
    client_id = f"vault-mcp-{secrets.token_hex(8)}"
    # Per-client, freshly generated -- NEVER config.VAULT_OAUTH_CLIENT_SECRET.
    client_secret = secrets.token_hex(32)
    client_name = body.get("client_name", "Obsidian Vault MCP Client")
    redirect_uris = body.get("redirect_uris", [])

    # S1 (2026-08-30): persist so /oauth/authorize can validate against this
    # client's OWN declared redirect_uris (RFC 7591-style) -- this is what
    # closes the open-redirect half of C-1/M-1 for dynamically-registered
    # clients (e.g. claude.ai's MCP connector) the same way the pre-registered
    # static entry closes it for jarvis-app, which never calls this endpoint.
    _register_client(client_id, redirect_uris, client_name)

    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": client_name,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "client_secret_post",
    }, status_code=201)


# Starlette routes to mount on the app
oauth_routes = [
    Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource, methods=["GET"]),
    Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Route("/oauth/register", oauth_register, methods=["POST"]),
]
