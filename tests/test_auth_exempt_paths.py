"""Regression test: both routes serving RFC 9728 protected-resource metadata
must not require a bearer token.

oauth.py registers oauth_protected_resource at TWO paths --
/.well-known/oauth-protected-resource and .../oauth-protected-resource/mcp --
and _AUTH_EXEMPT_PATHS is an exact-match set, not a prefix match, so each
needed its own entry. Protected-resource metadata is how a client discovers
where to authenticate in the first place -- gating it behind the very auth
it's meant to bootstrap defeats the point. oauth.py's own
oauth_protected_resource docstring already claimed "must be reachable without
a bearer token (see auth.py's _AUTH_EXEMPT_PATHS)", but neither path was
actually in that set, so every real request to either one 401'd.

Base path found live 2026-09-06 while gathering before/after evidence for the
X-Forwarded-* origin-spoofing fix; fixed same day. The /mcp-suffixed sibling
was spotted then too (same handler, same exact-match gap) and fixed as an
explicit follow-up.
"""

import asyncio

from obsidian_vault_mcp.auth import BearerAuthMiddleware, _AUTH_EXEMPT_PATHS


async def _run(path: str, headers: list[tuple[bytes, bytes]]) -> int:
    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = BearerAuthMiddleware(inner_app)
    scope = {"type": "http", "path": path, "headers": headers}
    sent = []

    async def send(msg):
        sent.append(msg)

    await mw(scope, None, send)
    return sent[0]["status"]


def test_protected_resource_path_is_in_exempt_set():
    """Direct assertion on the set itself -- the actual bug was a missing
    entry, not middleware logic, so pin the entry explicitly."""
    assert "/.well-known/oauth-protected-resource" in _AUTH_EXEMPT_PATHS


def test_protected_resource_mcp_path_is_in_exempt_set():
    """Same handler, different registered path -- exact-match set membership
    means the base path's entry above does not cover this one."""
    assert "/.well-known/oauth-protected-resource/mcp" in _AUTH_EXEMPT_PATHS


def test_protected_resource_reachable_with_no_auth_header():
    status = asyncio.run(_run("/.well-known/oauth-protected-resource", headers=[]))
    assert status == 200


def test_protected_resource_mcp_reachable_with_no_auth_header():
    status = asyncio.run(_run("/.well-known/oauth-protected-resource/mcp", headers=[]))
    assert status == 200


def test_protected_resource_reachable_with_bogus_auth_header():
    """Exempt means exempt -- a garbage/expired token must not turn this into
    a 401 either; the path bypasses the token check entirely."""
    status = asyncio.run(
        _run("/.well-known/oauth-protected-resource", headers=[(b"authorization", b"Bearer garbage")])
    )
    assert status == 200


def test_protected_resource_mcp_reachable_with_bogus_auth_header():
    status = asyncio.run(
        _run("/.well-known/oauth-protected-resource/mcp", headers=[(b"authorization", b"Bearer garbage")])
    )
    assert status == 200


def test_unrelated_path_still_requires_auth():
    """Sanity check that the exemption is scoped to these two paths, not a
    typo that accidentally opened up the middleware generally."""
    status = asyncio.run(_run("/mcp", headers=[]))
    assert status == 401
