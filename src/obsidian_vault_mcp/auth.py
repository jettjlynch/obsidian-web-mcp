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

Merged 2026-08-18 with upstream's OAuth-hardening pass (constant-time token
compare, RFC 9728 WWW-Authenticate challenges, and request-context threading for
the audit log) -- all of that logic is preserved here, just read via a plain
``starlette.requests.Request`` built from the raw ASGI scope/receive rather than
returned through ``call_next``, and responded to over ``send`` directly instead
of returning a ``Response`` (which is what ``BaseHTTPMiddleware`` needs and what
breaks the streamable-HTTP transport).
"""

import hmac
import json
import uuid

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config
from .config import VAULT_MCP_TOKEN
from .context import reset_request_context, set_request_context

# Paths that don't require bearer auth (OAuth flow + health)
_AUTH_EXEMPT_PATHS = {
    "/health",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/register",
}

# (method, path) pairs exempt from auth. The MCP spec 2025-06-18 probe on / must
# answer GET/HEAD without credentials. This is ONLY active when MCP is mounted off
# root (VAULT_MCP_PATH != "/"); when MCP is at root the transport owns GET/HEAD /
# and must stay fully authenticated, so the set is empty and behaviour is unchanged.
_AUTH_EXEMPT_METHOD_PATHS = (
    {("GET", "/"), ("HEAD", "/")} if config.VAULT_MCP_PATH != "/" else set()
)


def _www_authenticate(request: Request, error: str) -> str:
    """RFC 9728 challenge header pointing clients at the protected-resource metadata.

    Without it a 401 just looks like a failed request; with it, a spec-compliant MCP
    client (e.g. Claude Code, ChatGPT) knows to fetch the metadata and start the OAuth
    flow -- "Needs authentication" instead of "Failed to connect". The resource URL is
    derived from VAULT_MCP_PUBLIC_URL when set (otherwise request.base_url), matching the
    oauth_metadata / oauth_protected_resource endpoints. Pinning the public URL keeps a
    spoofed Host/X-Forwarded-Host header from pointing clients at an attacker's server.
    """
    base_url = config.advertised_base_url(str(request.base_url))
    resource_metadata = f"{base_url}/.well-known/oauth-protected-resource"
    return f'Bearer realm="mcp", resource_metadata="{resource_metadata}", error="{error}"'


async def _send_json(send: Send, status_code: int, payload: dict, headers: dict[str, str] | None = None) -> None:
    """Emit a minimal JSON response over raw ASGI (no Starlette Response)."""
    body = json.dumps(payload).encode("utf-8")
    response_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("latin-1")),
    ]
    for name, value in (headers or {}).items():
        response_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": response_headers,
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

        # A plain Request view over the ASGI scope -- header/base_url parsing only,
        # never used to bridge the response (that stays on `send`, see module docstring).
        request = Request(scope, receive=receive)

        if request.url.path in _AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if (request.method, request.url.path) in _AUTH_EXEMPT_METHOD_PATHS:
            await self.app(scope, receive, send)
            return

        if not VAULT_MCP_TOKEN:
            await _send_json(send, 500, {"error": "Server misconfigured: no auth token set"})
            return

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            await _send_json(
                send,
                401,
                {"error": "Missing or malformed Authorization header"},
                headers={"WWW-Authenticate": _www_authenticate(request, "invalid_request")},
            )
            return

        token = auth_header[7:]
        # Constant-time compare: avoid leaking the token via response timing (#2).
        if not hmac.compare_digest(token, VAULT_MCP_TOKEN):
            await _send_json(
                send,
                401,
                {"error": "Invalid token"},
                headers={"WWW-Authenticate": _www_authenticate(request, "invalid_token")},
            )
            return

        # Thread the authenticated principal (plus a request id and best-effort client
        # hint) to the tool layer for the audit log. The raw token never leaves this
        # context; audit.build_audit_record stores only its SHA-256 hash. client_id is a
        # User-Agent-derived hint -- it becomes a true per-client id if the static bearer
        # token is ever replaced with per-client tokens.
        client = request.headers.get("user-agent", "").strip()[:200] or None
        ctx_token = set_request_context(principal=token, request_id=uuid.uuid4().hex, client=client)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_context(ctx_token)
