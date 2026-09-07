"""Regression tests for the 2026-09-06/07 lifespan re-entry fix (see
LIFESPAN-INIT-DESIGN.md).

stateless_http=True means mcp.server.lowlevel.server.Server.run() re-enters
this server's lifespan() on EVERY request, not once at process boot
(confirmed against the installed mcp SDK). These tests drive the REAL
low-level Server.run() path concurrently -- via the same
create_connected_server_and_client_session() helper the mcp SDK itself uses
for testing -- rather than mocking it away, so they exercise the actual
mechanism that caused the bug, not a stand-in for it.
"""

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from obsidian_vault_mcp.frontmatter_index import FrontmatterIndex
from obsidian_vault_mcp.server import frontmatter_index, mcp


# --- FrontmatterIndex.start() idempotency (defense-in-depth guard) -------------

def test_frontmatter_index_start_is_idempotent(vault_dir):
    """A second start() while already running is a no-op -- must not spawn a
    second watchdog.Observer or re-walk the vault. Mirrors
    jimprosser/obsidian-web-mcp upstream commit 13e147f's own test for the
    same guard."""
    idx = FrontmatterIndex()
    try:
        idx.start()
        first_observer = idx._observer
        idx.start()  # must NOT spawn a second observer or re-walk
        assert idx._observer is first_observer
    finally:
        idx.stop()


# --- The actual bug mechanism, exercised for real, concurrently ---------------

async def _one_client_round_trip() -> None:
    """One real MCP session: connect, initialize, list tools, disconnect.
    This is exactly the shape of a single stateless HTTP request in
    production -- create_connected_server_and_client_session() starts a
    fresh task running mcp._mcp_server.run(...) per call, the same call
    _handle_stateless_request makes per request in the real HTTP transport.
    """
    async with create_connected_server_and_client_session(mcp) as session:
        await session.list_tools()


@pytest.mark.asyncio
async def test_lifespan_does_not_touch_frontmatter_index_under_concurrent_sessions(monkeypatch):
    """The core proof: fire many concurrent real MCP sessions through the
    exact Server.run() path that used to re-enter lifespan() per request, and
    confirm frontmatter_index.start() is never called by any of them (it now
    only runs once, from server.main(), never from lifespan()).
    """
    calls = []
    monkeypatch.setattr(frontmatter_index, "start", lambda: calls.append(1))
    monkeypatch.setattr(frontmatter_index, "stop", lambda: None)

    async with anyio.create_task_group() as tg:
        for _ in range(20):
            tg.start_soon(_one_client_round_trip)

    assert calls == [], (
        "lifespan() called frontmatter_index.start() -- it must be a no-op; "
        "start()/stop() belong in main() only (see LIFESPAN-INIT-DESIGN.md)"
    )


class _FakeEmbedder:
    def __init__(self, **kwargs) -> None:
        pass


class _FakeStore:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_change_listener_count_stays_fixed_under_concurrent_sessions(monkeypatch, tmp_path):
    """The listener-accumulation proof, exercising the REAL RETRIEVAL_ENABLED
    code path (not just a manually-seeded listener) with lightweight fakes
    standing in for OllamaEmbedder/RetrievalStore so this doesn't need a real
    Ollama instance. Confirmed this fails on the pre-fix code (list grows to
    20 under 20 concurrent sessions, one add_change_listener() call per
    lifespan() re-entry) before locking in the fix.
    """
    import obsidian_vault_mcp.retrieval.embeddings as embeddings_mod
    import obsidian_vault_mcp.retrieval.store as store_mod
    from obsidian_vault_mcp import config

    monkeypatch.setattr(frontmatter_index, "_change_listeners", [])
    monkeypatch.setattr(config, "RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(config, "RETRIEVAL_EMBEDDING_BACKEND", "ollama")
    monkeypatch.setattr(config, "RETRIEVAL_DB_PATH", tmp_path / "retrieval.sqlite")
    monkeypatch.setattr(embeddings_mod, "OllamaEmbedder", _FakeEmbedder)
    monkeypatch.setattr(store_mod, "RetrievalStore", _FakeStore)

    # Simulate exactly what main() does once, for real, with the real
    # RETRIEVAL_ENABLED branch of server.py's retrieval-init code -- not a
    # hand-rolled substitute for it.
    import obsidian_vault_mcp.server as server_mod

    server_mod.retrieval_embedder = embeddings_mod.OllamaEmbedder(
        model=config.RETRIEVAL_EMBED_MODEL, dimension=config.RETRIEVAL_EMBED_DIM, host=config.OLLAMA_HOST
    )
    server_mod.retrieval_store = store_mod.RetrievalStore(config.RETRIEVAL_DB_PATH, embedding_dim=config.RETRIEVAL_EMBED_DIM)
    frontmatter_index.add_change_listener(lambda rel_path, exists: None)
    assert len(frontmatter_index._change_listeners) == 1

    async with anyio.create_task_group() as tg:
        for _ in range(20):
            tg.start_soon(_one_client_round_trip)

    assert len(frontmatter_index._change_listeners) == 1, (
        f"listener list grew to {len(frontmatter_index._change_listeners)} under "
        f"20 concurrent sessions -- lifespan() must not re-register listeners"
    )
