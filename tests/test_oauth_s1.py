"""Regression tests for S1 (2026-08-30): redirect_uri allowlist, client_id
verification, and per-client tokens on /oauth/authorize + /oauth/token.

The 2026-08-17 PIN gate (tested implicitly here via VAULT_OAUTH_AUTHORIZE_PIN)
stopped unauthenticated callers from ever reaching approval, but it did NOT
validate redirect_uri (open redirect -- a PIN-holder could still be steered
into approving a code sent to an attacker's URI) or stop the token endpoint
handing back the one shared static VAULT_MCP_TOKEN to every successful flow.
This locks in the S1 fix for both.
"""

import hashlib
import base64
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import config, oauth


PIN = "1234"
STATIC_TOKEN = "the-real-shared-static-token"
JARVIS_REDIRECT = "jarvisapp://oauth/callback"


def _isolate_oauth_state(monkeypatch, tmp_path):
    """Full isolation: fresh client registry + token store, no real files
    touched, jarvis-app re-seeded exactly as the real module does at import.
    """
    monkeypatch.setattr(config, "VAULT_OAUTH_AUTHORIZE_PIN", PIN)
    monkeypatch.setattr(config, "VAULT_MCP_TOKEN", STATIC_TOKEN)
    monkeypatch.setattr(config, "VAULT_OAUTH_STATIC_CLIENT_ID", "jarvis-app")
    monkeypatch.setattr(config, "VAULT_OAUTH_REDIRECT_URIS", [JARVIS_REDIRECT])

    monkeypatch.setattr(oauth, "_CLIENTS_FILE", tmp_path / "oauth_clients.json")
    monkeypatch.setattr(oauth, "_TOKENS_FILE", tmp_path / "oauth_tokens.json")
    monkeypatch.setattr(oauth, "_registered_clients", {})
    monkeypatch.setattr(oauth, "_issued_tokens", {})
    monkeypatch.setattr(oauth, "_auth_codes", {})

    oauth._register_client("jarvis-app", [JARVIS_REDIRECT], "jarvis-app (static, pre-registered)")


def _client(monkeypatch, tmp_path):
    _isolate_oauth_state(monkeypatch, tmp_path)
    app = Starlette(routes=oauth.oauth_routes)
    return TestClient(app, follow_redirects=False)


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# --- Open redirect (M-1) --------------------------------------------------

def test_authorize_get_rejects_unregistered_redirect_uri(monkeypatch, tmp_path):
    """An attacker-supplied redirect_uri never even reaches the PIN form."""
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": "https://attacker.example.com/steal",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    })
    assert resp.status_code == 400
    assert "PIN" not in resp.text  # never shown the consent form


def test_authorize_get_rejects_unregistered_client_id(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "not-a-real-client",
        "redirect_uri": JARVIS_REDIRECT,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    })
    assert resp.status_code == 400


def test_authorize_post_rejects_bad_redirect_uri_even_with_correct_pin(monkeypatch, tmp_path):
    """Defense in depth: the POST re-check catches a tampered hidden field
    even if somehow reached with the right PIN."""
    client = _client(monkeypatch, tmp_path)
    resp = client.post("/oauth/authorize", data={
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": "https://attacker.example.com/steal",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
        "pin": PIN,
    })
    assert resp.status_code == 400
    assert resp.status_code != 302  # never redirects to the unvalidated URI


def test_authorize_get_with_valid_jarvis_app_request_shows_pin_form(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": JARVIS_REDIRECT,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    })
    assert resp.status_code == 200
    assert "PIN" in resp.text


# --- Per-client tokens, not the shared static one (rest of C-1) ----------

def test_full_pkce_flow_issues_a_fresh_token_not_the_static_one(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    verifier, challenge = _pkce_pair()

    authz = client.post("/oauth/authorize", data={
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": JARVIS_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "pin": PIN,
    })
    assert authz.status_code == 302
    location = authz.headers["location"]
    assert location.startswith(JARVIS_REDIRECT)
    code = location.split("code=")[1].split("&")[0]

    token_resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": JARVIS_REDIRECT,
        "code_verifier": verifier,
    })
    assert token_resp.status_code == 200
    body = token_resp.json()
    assert body["access_token"] != STATIC_TOKEN
    assert body["expires_in"] == oauth._TOKEN_TTL_SECONDS
    assert oauth.is_valid_issued_token(body["access_token"])
    # C-2 (2026-09-05): the consent form's write checkbox was never ticked
    # above, so this must default to read-only, not silently grant write.
    assert body["scope"] == "read"
    assert oauth.get_issued_token_scope(body["access_token"]) == "read"


def test_dynamically_registered_client_gets_its_own_redirect_uri_checked(monkeypatch, tmp_path):
    """claude.ai-style flow: register, then authorize must match what THAT
    client declared -- not jarvis-app's allowlist, not any other client's."""
    client = _client(monkeypatch, tmp_path)
    reg = client.post("/oauth/register", json={
        "client_name": "claude.ai connector",
        "redirect_uris": ["https://claude.ai/api/mcp/callback"],
    }).json()

    # Its own declared redirect_uri: allowed.
    ok = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": "https://claude.ai/api/mcp/callback",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    })
    assert ok.status_code == 200

    # jarvis-app's redirect_uri, used by THIS client_id: still rejected --
    # allowlist is per-client, not a single global list.
    cross = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": JARVIS_REDIRECT,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    })
    assert cross.status_code == 400


# --- auth.py: per-client tokens accepted, static token NO LONGER accepted -

def test_bearer_middleware_rejects_static_token_and_junk_but_accepts_issued(monkeypatch, tmp_path):
    """2026-09-05 (C-2 follow-up): the static token used to be accepted
    here unconditionally -- that's exactly what made C-2's scope split
    live-ineffective (claude.ai's connector had been authenticating with it
    since before per-client tokens existed, sailing past the new scope gate
    as "write" regardless of the consent-page checkbox). Renamed from
    `..._accepts_static_token_and_issued_token_rejects_junk` to reflect the
    new, intentional behavior -- this is not a relaxed assertion, it's the
    opposite one."""
    _isolate_oauth_state(monkeypatch, tmp_path)
    from obsidian_vault_mcp.auth import BearerAuthMiddleware

    calls = []

    async def inner_app(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = BearerAuthMiddleware(inner_app)

    def make_scope(token: str) -> dict:
        return {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

    async def run(token):
        sent = []
        async def send(msg):
            sent.append(msg)
        await mw(make_scope(token), None, send)
        return sent[0]["status"]

    import asyncio

    # Static token: now rejected -- the whole point of this change.
    assert asyncio.run(run(STATIC_TOKEN)) == 401

    # Freshly issued per-client token: still accepted.
    issued = oauth._issue_token("jarvis-app", "read", audience="vault")
    assert asyncio.run(run(issued)) == 200

    # Garbage token: rejected, unchanged.
    assert asyncio.run(run("not-a-real-token")) == 401
