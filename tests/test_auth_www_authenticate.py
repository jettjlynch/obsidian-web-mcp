"""Port of upstream 60d7a13 (#35): 401s carry an RFC 9728 Bearer challenge."""

import asyncio

from obsidian_vault_mcp import config
from obsidian_vault_mcp.auth import BearerAuthMiddleware


def _run(path, headers, scheme="https"):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    sent = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "path": path, "headers": headers, "scheme": scheme}
    asyncio.run(BearerAuthMiddleware(inner)(scope, None, send))
    start = sent[0]
    return start["status"], dict(start["headers"])


def test_missing_header_401_has_challenge_for_mcp(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", "")
    status, hdrs = _run("/mcp", [(b"host", b"vault.example")])
    assert status == 401
    ch = hdrs[b"www-authenticate"].decode()
    assert ch.startswith('Bearer realm="mcp"')
    assert 'resource_metadata="https://vault.example/.well-known/oauth-protected-resource/mcp"' in ch
    assert 'error="invalid_request"' in ch


def test_invalid_token_401_has_invalid_token_error(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", "")
    status, hdrs = _run("/other", [(b"host", b"vault.example"), (b"authorization", b"Bearer nope")])
    assert status == 401
    ch = hdrs[b"www-authenticate"].decode()
    assert 'resource_metadata="https://vault.example/.well-known/oauth-protected-resource"' in ch
    assert 'error="invalid_token"' in ch


def test_pinned_public_url_wins_over_spoofed_host(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", "https://pinned.example")
    _, hdrs = _run("/mcp", [(b"host", b"evil.example")])
    ch = hdrs[b"www-authenticate"].decode()
    assert "pinned.example" in ch and "evil.example" not in ch


def test_quotes_in_host_cannot_break_out(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_PUBLIC_URL", "")
    _, hdrs = _run("/mcp", [(b"host", b'evil", error="x')])
    ch = hdrs[b"www-authenticate"].decode()
    assert ch.count('"') == 6  # three quoted params, nothing injected


def test_exempt_path_still_200(monkeypatch):
    status, hdrs = _run("/health", [])
    assert status == 200 and b"www-authenticate" not in hdrs
