"""Regression test (port of upstream jimprosser/obsidian-web-mcp #81).

Upstream's bearer middleware compared request.url.path to the exempt set; that value
is parsed back out of a URL string, so an encoded "?" or "#" truncated it ("/health%3F/x"
read as the exempt "/health"). This fork's pure-ASGI BearerAuthMiddleware already reads
scope["path"] (the decoded path), so it is not affected; this test locks that in so a
future refactor back to request.url.path fails loudly.
"""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from obsidian_vault_mcp.auth import BearerAuthMiddleware


def _client(served: list) -> TestClient:
    async def handler(request):
        served.append(request.scope["path"])
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/health?/x", handler, methods=["GET", "POST"]),
            Route("/health#/x", handler, methods=["GET", "POST"]),
            Route("/oauth/token?/x", handler, methods=["GET", "POST"]),
            Route("/health", handler, methods=["GET"]),
        ]
    )
    app.add_middleware(BearerAuthMiddleware)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/health%3F/x", "/health%23/x", "/oauth/token%3F/x"])
def test_encoded_delimiter_does_not_borrow_an_exemption(path):
    served: list = []
    c = _client(served)
    assert c.get(path).status_code == 401
    assert c.post(path).status_code == 401
    assert served == []


def test_exempt_path_stays_exempt_with_a_real_query_string():
    served: list = []
    c = _client(served)
    assert c.get("/health?probe=1").status_code == 200
