"""Regression test for the /oauth/register client_secret leak.

Emergency fix, 2026-08-18: /oauth/register (unauthenticated -- see auth.py's
_AUTH_EXEMPT_PATHS) used to hand back config.VAULT_OAUTH_CLIENT_SECRET, the
real shared secret, to any caller. Combined with grant_type=client_credentials
at /oauth/token (which only checks that secret, never the PIN gate on
/oauth/authorize), that was a full bypass: register -> token in 2
unauthenticated requests yielded the real VAULT_MCP_TOKEN. Root-caused while
merging in jimprosser/obsidian-web-mcp's independent fix for the same bug
class (their commit e4924f0).

2026-09-05: the client_credentials grant this bug depended on was removed
outright (C-2 follow-up -- checked first, nothing legitimate ever used it;
see oauth_token's docstring and OPERATIONS.md). The exploit path this file
guards against is now closed twice over: the leak fix below, AND the grant
it would have been redeemed against no longer exists at all. Kept both
tests (updated for the new response shape) rather than deleted, since the
register-leak half is a real, independent regression risk on its own.
"""

from starlette.applications import Starlette

from obsidian_vault_mcp import config, oauth


def _client(monkeypatch):
    monkeypatch.setattr(config, "VAULT_OAUTH_CLIENT_SECRET", "the-real-shared-secret")
    monkeypatch.setattr(config, "VAULT_OAUTH_CLIENT_ID", "vault-mcp-client")
    monkeypatch.setattr(config, "VAULT_MCP_TOKEN", "the-real-bearer-token")

    from starlette.testclient import TestClient
    app = Starlette(routes=oauth.oauth_routes)
    return TestClient(app)


def test_register_does_not_return_the_shared_secret(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/oauth/register", json={"client_name": "attacker"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_secret"] != "the-real-shared-secret"


def test_registered_secret_cannot_obtain_a_token_via_client_credentials(monkeypatch):
    """The core exploit: register, then try to use what it gave you.

    Was a 401 (invalid_client) when the grant still existed; now a flat 400
    (unsupported_grant_type) since the grant was removed entirely -- either
    way, no token, which is what this test actually guards.
    """
    client = _client(monkeypatch)
    reg = client.post("/oauth/register", json={"client_name": "attacker"}).json()

    resp = client.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": reg["client_id"],
        "client_secret": reg["client_secret"],
    })
    assert resp.status_code == 400
    assert "access_token" not in resp.json()


def test_client_credentials_is_disabled_even_with_the_real_secret(monkeypatch):
    """2026-09-05: the grant is gone outright, not just gated -- confirmed
    nothing legitimate used it (see oauth_token's docstring) before removing
    it, so even the real out-of-band secret no longer gets a token this way."""
    client = _client(monkeypatch)
    resp = client.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": "vault-mcp-client",
        "client_secret": "the-real-shared-secret",
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"
    assert "access_token" not in resp.json()


def test_two_registrations_get_different_secrets(monkeypatch):
    client = _client(monkeypatch)
    reg1 = client.post("/oauth/register", json={}).json()
    reg2 = client.post("/oauth/register", json={}).json()
    assert reg1["client_secret"] != reg2["client_secret"]
