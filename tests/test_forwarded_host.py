"""Regression tests: a spoofed Host / X-Forwarded-Host header must not let an
attacker control the URLs the server advertises during OAuth discovery.

Merged in 2026-09-06 from jimprosser/obsidian-web-mcp's independent fix for the
same class of bug (upstream commit 669775a) -- ported to this server's actual
routes/config rather than copied verbatim (see config.py's VAULT_MCP_PUBLIC_URL
and VAULT_MCP_FORWARDED_ALLOW_IPS docstrings). Two layers of defense:
  1. VAULT_MCP_PUBLIC_URL pins the advertised origin (app layer -- oauth_metadata
     and oauth_protected_resource in oauth.py).
  2. uvicorn forwarded_allow_ips="127.0.0.1" (config default) stops the proxy
     layer from honoring X-Forwarded-* from non-loopback clients.

This server has no WWW-Authenticate challenge on 401s -- that's a different,
not-yet-merged upstream commit (#35) -- so unlike upstream's version of this
test file, there is no www_authenticate test here. The OAuth metadata routes
under test are all in auth.py's _AUTH_EXEMPT_PATHS, so no bearer-auth
middleware is needed to exercise them (see tests/test_oauth_s1.py for the same
minimal Starlette(routes=oauth.oauth_routes) pattern used elsewhere here).

Note on the test client: Starlette's TestClient speaks ASGI directly and does
NOT run uvicorn's ProxyHeadersMiddleware, so X-Forwarded-Host alone never
reaches request.base_url here. We therefore spoof via the Host header instead
-- that is the scope value a *trusting* proxy would synthesize from
X-Forwarded-Host, so it is the faithful app-layer stand-in for the attack. The
forwarded_allow_ips default test covers the proxy layer that would otherwise
turn X-Forwarded-Host into Host.
"""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import config, oauth

SPOOFED = "evil.example"
PUBLIC = "https://vault-mcp.example.com"
SPOOF_HEADERS = {"Host": SPOOFED, "X-Forwarded-Host": SPOOFED, "X-Forwarded-Proto": "https"}


@pytest.fixture(autouse=True)
def reset_state():
    yield


@pytest.fixture
def client(monkeypatch):
    # Isolate from any real env: no pinned public URL unless a test sets one.
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", "")
    app = Starlette(routes=oauth.oauth_routes)
    return TestClient(app)


# --- The spoofable surface (documents the mechanism the fix neutralizes) -------

def test_advertised_origin_follows_host_when_unpinned(client):
    """Without VAULT_MCP_PUBLIC_URL, the advertised origin follows the (proxied)
    Host header. This is exactly the surface an over-trusting proxy turns into an
    X-Forwarded-Host spoofing vector -- and what pinning the public URL closes."""
    r = client.get("/.well-known/oauth-protected-resource", headers={"Host": SPOOFED})
    assert SPOOFED in r.json()["resource"]


# --- The fix: VAULT_MCP_PUBLIC_URL pins every advertised URL -------------------

def test_public_url_pins_protected_resource_metadata(client, monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", PUBLIC)
    r = client.get("/.well-known/oauth-protected-resource", headers=SPOOF_HEADERS)
    body = r.json()
    assert body["resource"] == PUBLIC
    assert body["authorization_servers"] == [PUBLIC]
    assert SPOOFED not in r.text


def test_public_url_pins_authorization_server_metadata(client, monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", PUBLIC)
    r = client.get("/.well-known/oauth-authorization-server", headers=SPOOF_HEADERS)
    body = r.json()
    assert body["issuer"] == PUBLIC
    assert body["authorization_endpoint"] == f"{PUBLIC}/oauth/authorize"
    assert body["token_endpoint"] == f"{PUBLIC}/oauth/token"
    assert body["registration_endpoint"] == f"{PUBLIC}/oauth/register"
    assert SPOOFED not in r.text


def test_public_url_trailing_slash_is_normalized(client, monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", PUBLIC + "/")
    r = client.get("/.well-known/oauth-protected-resource", headers=SPOOF_HEADERS)
    assert r.json()["resource"] == PUBLIC  # no doubled or trailing slash


# --- Fallback preserved: unset public URL still uses the request base_url ------

def test_falls_back_to_base_url_when_public_url_unset(client):
    r = client.get("/.well-known/oauth-protected-resource", headers={"Host": "real.host"})
    assert r.json()["resource"] == "http://real.host"


# --- Proxy layer: uvicorn must not trust forwarded headers from any source ----

def test_forwarded_allow_ips_default_is_not_wildcard():
    """The uvicorn forwarded_allow_ips default must stay loopback-only so a remote
    client cannot have its X-Forwarded-* headers honored (regression guard for the
    `forwarded_allow_ips="*"` that made the advertised origin spoofable)."""
    assert config.VAULT_MCP_FORWARDED_ALLOW_IPS != "*"
    assert "127.0.0.1" in config.VAULT_MCP_FORWARDED_ALLOW_IPS
