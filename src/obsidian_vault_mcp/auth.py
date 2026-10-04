"""Bearer token authentication for the vault MCP server.

Implemented as PURE ASGI middleware -- deliberately NOT a Starlette
``BaseHTTPMiddleware``.

``BaseHTTPMiddleware`` runs the downstream app inside its own anyio task group
and bridges the request/response through a memory object stream. When it wraps
the MCP *stateless streamable-HTTP* transport (which owns its own memory streams
and task groups), that inner stream is closed before the JSON-RPC message is
processed, raising ``anyio.ClosedResourceError`` -> HTTP 500 on every
``POST /mcp``. Pure ASGI middleware introduces no task group, so the transport's
streams stay open.

Root-caused 2026-06-15 (claude.ai Obsidian connector "returned an error":
authenticated POST /mcp 500ing via ClosedResourceError at streamable_http.py:543).
Auth logic and responses are identical to the previous BaseHTTPMiddleware version.

C-2 (2026-09-05): this middleware also determines the *scope* the presented
token was granted (via oauth.get_token_scope -- per-client tokens carry
whatever the consent page approved; the legacy static VAULT_MCP_TOKEN no
longer authenticates against this server at all as of the same day's
follow-up, see get_token_scope's docstring) and records it in
token_scope.current_scope for the duration of the request. It does NOT gate
individual tools here -- which tool is being called isn't known until the
JSON-RPC body is parsed, well downstream of this pure-ASGI middleware (same
reasoning rate_limit.py documents for why its own enforcement lives at the
tool-wrapper layer, not here). See token_scope.py and audit.py's
`audited()` decorator for the actual gate.
"""

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from .oauth import get_token_scope
from . import config, token_scope

# Paths that don't require bearer auth (OAuth flow + health)
_AUTH_EXEMPT_PATHS = {
    "/health",
    "/.well-known/oauth-authorization-server",
    # RFC 9728: protected-resource metadata MUST be reachable without a
    # token -- it's how a client discovers where to authenticate in the
    # first place. Was missing here despite oauth.py's own
    # oauth_protected_resource docstring already claiming "must be
    # publicly reachable (see auth.py's _AUTH_EXEMPT_PATHS)" -- that
    # claim was wrong until now; found live 2026-09-06 while gathering
    # before/after evidence for the X-Forwarded-* origin-spoofing fix.
    "/.well-known/oauth-protected-resource",
    # oauth.py registers oauth_protected_resource at this second path too
    # (Route("/.well-known/oauth-protected-resource/mcp", ...) -- same
    # RFC 9728 handler, same "must be reachable with no token" requirement.
    # _AUTH_EXEMPT_PATHS is exact-match, not a prefix match, so the base
    # path's exemption above does NOT cover this one -- needs its own entry.
    "/.well-known/oauth-protected-resource/mcp",
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/register",
}


def _www_authenticate(scope: Scope, error: str) -> bytes:
    """RFC 9728 Bearer challenge pointing clients at protected-resource metadata.

    Port of upstream 60d7a13 (#35) into this fork's pure-ASGI middleware.
    Without it a 401 is just a failed request; with it a spec-compliant MCP
    client (Claude Code, ChatGPT, claude.ai) knows to fetch the metadata and
    start the OAuth flow ("Needs authentication" rather than "Failed to
    connect"). The origin goes through config.advertised_base_url, the same
    spoof-resistant helper oauth_metadata / oauth_protected_resource use
    (VAULT_MCP_PUBLIC_URL when pinned, else the request's own origin). The
    metadata path mirrors the routes oauth.py serves: /mcp requests point at
    .../oauth-protected-resource/mcp, everything else at the base path.
    """
    host = ""
    for name, value in scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1")
            break
    if not host:
        server = scope.get("server") or ("localhost", None)
        host = server[0] if server[1] in (None, 80, 443) else f"{server[0]}:{server[1]}"
    request_base = f"{scope.get('scheme', 'http')}://{host}{scope.get('root_path', '')}"
    base_url = config.advertised_base_url(request_base)
    path = scope.get("path", "")
    suffix = "/mcp" if path == "/mcp" or path.startswith("/mcp/") else ""
    resource_metadata = f"{base_url}/.well-known/oauth-protected-resource{suffix}"
    # Never let a quote/backslash from a client-supplied Host break out of
    # the quoted-string parameter.
    resource_metadata = resource_metadata.replace('"', "").replace("\\", "")
    return (
        f'Bearer realm="mcp", resource_metadata="{resource_metadata}", error="{error}"'
    ).encode("latin-1", errors="replace")


async def _send_json(
    send: Send, status_code: int, payload: dict, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    """Emit a minimal JSON response over raw ASGI (no Starlette Response)."""
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ]
            + (extra_headers or []),
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Validates Bearer tokens on all requests except OAuth and health endpoints.

    Pure ASGI (see module docstring for why this must NOT be a BaseHTTPMiddleware).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only guard HTTP; pass lifespan/websocket scopes straight through.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path", "") in _AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # ASGI headers: list of (name, value) byte tuples; names are lowercased.
        auth_header = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth_header = value.decode("latin-1")
                break

        if not auth_header.startswith("Bearer "):
            await _send_json(
                send, 401, {"error": "Missing or malformed Authorization header"},
                [(b"www-authenticate", _www_authenticate(scope, "invalid_request"))],
            )
            return

        token = auth_header[7:]
        # get_token_scope looks up whatever scope a per-client token issued
        # by the S1/C-2 OAuth flow was granted. The legacy static token no
        # longer authenticates here at all (C-2 follow-up, 2026-09-05 -- see
        # get_token_scope's docstring for why and what was checked first).
        # None means the token is unknown/expired -- invalid.
        granted_scope = get_token_scope(token)
        if granted_scope is None:
            await _send_json(
                send, 401, {"error": "Invalid token"},
                [(b"www-authenticate", _www_authenticate(scope, "invalid_token"))],
            )
            return

        # Thread the granted scope down to audit.py's tool-wrapper gate for
        # the lifetime of this request only -- see token_scope.py and this
        # module's docstring for why the gate itself can't live here.
        scope_reset = token_scope.current_scope.set(granted_scope)
        try:
            await self.app(scope, receive, send)
        finally:
            token_scope.current_scope.reset(scope_reset)
