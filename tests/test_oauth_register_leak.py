"""Regression test for the /oauth/register client_secret leak.

Emergency fix, 2026-08-18: /oauth/register (unauthenticated -- see auth.py's
_AUTH_EXEMPT_PATHS) used to hand back config.VAULT_OAUTH_CLIENT_SECRET, the
real shared secret, to any caller. Combined with grant_type=client_credentials
at /oauth/token (which only checks that secret, never the PIN gate on
/oauth/authorize), that was a full bypass: register -> token in 2
unauthenticated requests yielded the real VAULT_MCP_TOKEN. Root-caused while
merging in jimprosser/obsidian-web-mcp's independent fix for the same bug
class (their commit e4924f0).

This locks in both directions: the leak stays closed, AND a legitimate
client that already has the real VAULT_OAUTH_CLIENT_SECRET configured
out-of-band (never obtained via /oauth/register) still works.
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
    """The core exploit: register, then try to use what it gave you."""
    client = _client(monkeypatch)
    reg = client.post("/oauth/register", json={"client_name": "attacker"}).json()

    resp = client.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": reg["client_id"],
        "client_secret": reg["client_secret"],
    })
    assert resp.status_code == 401
    assert "access_token" not in resp.json()


def test_legitimate_client_credentials_still_works_with_the_real_secret(monkeypatch):
    """No regression: whoever has the real out-of-band secret still gets a token."""
    client = _client(monkeypatch)
    resp = client.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": "vault-mcp-client",
        "client_secret": "the-real-shared-secret",
    })
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "the-real-bearer-token"


def test_two_registrations_get_different_secrets(monkeypatch):
    client = _client(monkeypatch)
    reg1 = client.post("/oauth/register", json={}).json()
    reg2 = client.post("/oauth/register", json={}).json()
    assert reg1["client_secret"] != reg2["client_secret"]
