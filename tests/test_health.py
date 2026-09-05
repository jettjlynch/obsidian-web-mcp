"""Tests for the /health route (closes the 404 gap found during C-2 follow-up).

auth.py's _AUTH_EXEMPT_PATHS listed "/health" from the start, but no route
ever answered it -- any request there 404'd. mcp_watchdog.sh worked around
this by doing a full authenticated MCP `initialize` round trip instead of a
cheap unauthenticated probe, which is what made it a hidden consumer of the
static VAULT_MCP_TOKEN.
"""

import json

from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp.server import health_routes, frontmatter_index


def _client():
    app = Starlette(routes=health_routes)
    return TestClient(app)


def test_health_requires_no_auth_header(monkeypatch):
    """The literal point of this route: no Authorization header, still 200."""
    client = _client()
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_body_has_no_vault_content_or_auth_state():
    client = _client()
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["index_loaded"], bool)
    # No path/filename/token/scope/PIN-shaped keys leak into this response --
    # a monitoring script with zero credentials should learn nothing about
    # vault contents or auth state from it, only "is the process up".
    assert set(body.keys()) == {"status", "index_loaded"}


def test_health_reflects_frontmatter_index_readiness(monkeypatch):
    monkeypatch.setattr(frontmatter_index, "_observer", None)
    assert _client().get("/health").json()["index_loaded"] is False

    monkeypatch.setattr(frontmatter_index, "_observer", object())  # any non-None sentinel
    assert _client().get("/health").json()["index_loaded"] is True
