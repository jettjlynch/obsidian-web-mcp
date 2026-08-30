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
"""

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import VAULT_MCP_TOKEN
from .oauth import is_valid_issued_token

# Paths that don't require bearer auth (OAuth flow + health)
_AUTH_EXEMPT_PATHS = {
    "/health",
    "/.well-known/oauth-authorization-server",
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/register",
}


async def _send_json(send: Send, status_code: int, payload: dict) -> None:
    """Emit a minimal JSON response over raw ASGI (no Starlette Response)."""
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
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

        if not VAULT_MCP_TOKEN:
            await _send_json(send, 500, {"error": "Server misconfigured: no auth token set"})
            return

        # ASGI headers: list of (name, value) byte tuples; names are lowercased.
        auth_header = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth_header = value.decode("latin-1")
                break

        if not auth_header.startswith("Bearer "):
            await _send_json(send, 401, {"error": "Missing or malformed Authorization header"})
            return

        token = auth_header[7:]
        # M-2 fix (2026-08-30): was plain `!=` -- timing-unsafe string
        # comparison on the one token that gates the whole vault. Every other
        # secret comparison in oauth.py already used hmac.compare_digest;
        # this was the one place that didn't.
        # Accepts the legacy static token (still valid for already-issued /
        # out-of-band consumers until S2 finishes the token split) OR a
        # per-client token issued by the S1 authorization_code flow.
        if not (hmac.compare_digest(token, VAULT_MCP_TOKEN) or is_valid_issued_token(token)):
            await _send_json(send, 401, {"error": "Invalid token"})
            return

        await self.app(scope, receive, send)
