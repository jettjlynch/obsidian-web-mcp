"""Real-subprocess regression test for the 2026-09-06/07 lifespan re-entry fix
(see LIFESPAN-INIT-DESIGN.md). Adapted from prouds-mcp's
tests/test_frontmatter_index_lifecycle.py (commit b8c199e, 2026-08-03), which
diagnosed and fixed the identical bug in that sibling codebase -- fitted here
to this repo's actual auth model (PIN-gated OAuth consent + per-client
scoped tokens, not prouds-mcp's multi-user PROUDS_USERS_FILE).

Starts a REAL server subprocess (real uvicorn, real watchdog.Observer, real
process boundary) rather than testing any piece in isolation, because the bug
is inherently about live concurrency and native OS threads under a running
server -- none of which an in-process unit test can exercise as faithfully
as tests/test_lifespan_init.py's in-memory-transport tests do for the
Server.run()-level mechanism. Both are needed; they prove different layers.
"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if r.status_code == 200:
                return
        except httpx.HTTPError as e:
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(f"server never became healthy on port {port}: {last_err}")


@pytest.fixture
def live_server(tmp_path):
    """Start a real vault-mcp server subprocess against an isolated
    vault/OAuth-state, pre-seeded with a valid read-scope bearer token so
    real MCP requests can be sent without driving the interactive PIN flow.
    Yields (port, token, log_path); terminates cleanly."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "note.md").write_text("# test\n")

    oauth_state_dir = tmp_path / "oauth_state"
    oauth_state_dir.mkdir()

    token = secrets.token_urlsafe(32)
    (oauth_state_dir / "oauth_tokens.json").write_text(json.dumps({
        token: {"client_id": "test-client", "expires_at": time.time() + 3600, "scope": "read"}
    }))

    port = _free_port()
    log_path = tmp_path / "server.log"

    env = {
        **os.environ,
        "VAULT_PATH": str(vault_dir),
        "VAULT_MCP_PORT": str(port),
        "VAULT_OAUTH_STATE_DIR": str(oauth_state_dir),
        "VAULT_OAUTH_AUTHORIZE_PIN": secrets.token_hex(8),
        "RETRIEVAL_ENABLED": "",  # keep the base concurrency test independent of Ollama
    }

    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "obsidian_vault_mcp.server"],
            env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
    try:
        _wait_healthy(port)
        yield port, token, log_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def _one_real_mcp_call(port: int, token: str) -> dict:
    """One real HTTP round trip through the actual stateless transport:
    initialize -> notifications/initialized -> tools/list. This is exactly
    the code path that used to re-enter lifespan() per request."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        init = await client.post(
            f"http://127.0.0.1:{port}/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "concurrency-test", "version": "0.0.1"},
                },
            },
        )
        await client.post(
            f"http://127.0.0.1:{port}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        tools = await client.post(
            f"http://127.0.0.1:{port}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        return {"init_status": init.status_code, "tools_status": tools.status_code}


def test_server_stays_responsive_under_concurrent_requests(live_server):
    """The real-world reproduction: fire a burst of concurrent real MCP
    requests (the exact trigger that pegged prouds-mcp at 100% CPU and hung
    it on 2026-08-03) and confirm the server stays responsive throughout,
    with no fsevents 'already scheduled' collision in its log."""
    port, token, log_path = live_server

    async def _burst():
        return await asyncio.gather(*[_one_real_mcp_call(port, token) for _ in range(15)])

    results = asyncio.run(_burst())

    assert len(results) == 15
    for r in results:
        assert r["init_status"] == 200, r
        assert r["tools_status"] == 200, r

    # The server must still answer /health after the burst -- the literal
    # symptom of the 2026-08-03 hang was the process staying alive but
    # unresponsive to everything, including new connections.
    r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
    assert r.status_code == 200

    log_text = log_path.read_text()
    assert "already scheduled" not in log_text, (
        "fsevents watch collision reappeared -- lifespan() is re-entering "
        "frontmatter_index.start() again (regression of the 2026-09-07 fix)"
    )

    # Black-box proof of the fix: the full vault walk/observer start only
    # ever happens once, not once per request, regardless of how many
    # concurrent requests were served. FrontmatterIndex.start() itself logs
    # "...files in %.2f seconds" (frontmatter_index.py); main() logs a
    # second, differently-worded line right after ("...files indexed",
    # server.py) -- one real start() call legitimately produces BOTH lines
    # once each, so this counts the start()-internal one specifically to
    # avoid conflating "two lines from one call" with "two calls".
    assert log_text.count("files in") == 1, (
        f"expected exactly 1 real FrontmatterIndex.start() call (one "
        f"'...files in N.NN seconds' log line), found {log_text.count('files in')} "
        f"-- start() must run once at process startup, not per request"
    )
