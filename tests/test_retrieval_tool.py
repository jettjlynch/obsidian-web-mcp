"""Tests for the `vault_search_semantic` MCP tool -- the thing that actually
gets exposed alongside `vault_search` and `vault_search_frontmatter`
(RETRIEVAL-DESIGN.md §5 step 7: a new tool sitting alongside the existing
two, not replacing them).

This server is single-tenant: there's no per-request identity/scope like
prouds-mcp's multi-user model (no identity.py, no UserContext). Folder
scoping here is the process-wide VAULT_SCOPE_ROOT, exactly like every other
tool in tools/search.py -- so scoping tests monkeypatch `config.VAULT_SCOPE_ROOT`
rather than setting a per-request user, matching tests/test_scope.py's own
pattern for the rest of this server's tools.
"""

import json

import pytest

import obsidian_vault_mcp.server as server
from obsidian_vault_mcp import config
from obsidian_vault_mcp.retrieval.chunker import Chunk
from obsidian_vault_mcp.retrieval.embeddings import DeterministicFakeEmbedder
from obsidian_vault_mcp.retrieval.store import RetrievalStore
from obsidian_vault_mcp.tools.search import vault_search_semantic

DIM = 8


def _chunk(text, content_hash, heading_path):
    return Chunk(text=text, parent_id=content_hash, heading_path=heading_path, line_start=1, content_hash=content_hash, frontmatter={})


@pytest.fixture
def configured_retrieval(tmp_path, monkeypatch):
    embedder = DeterministicFakeEmbedder(dimension=DIM)
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    monkeypatch.setattr(server, "retrieval_store", store)
    monkeypatch.setattr(server, "retrieval_embedder", embedder)
    yield store, embedder
    store.close()


def test_tool_errors_gracefully_when_retrieval_not_configured(monkeypatch):
    monkeypatch.setattr(server, "retrieval_store", None)
    monkeypatch.setattr(server, "retrieval_embedder", None)
    out = json.loads(vault_search_semantic(query="anything"))
    assert "error" in out


def test_tool_returns_results_for_full_vault_access(configured_retrieval, monkeypatch):
    monkeypatch.setattr(config, "VAULT_SCOPE_ROOT", "")  # default: full access
    store, embedder = configured_retrieval
    vec = embedder.embed(["revenue was up this quarter"])[0]
    store.apply("Finance/q3.md", [_chunk("revenue was up this quarter", "h1", ("Finance/q3.md", "Q3"))], {"h1": vec})

    out = json.loads(vault_search_semantic(query="revenue was up this quarter"))
    assert out["no_good_answer"] is False
    assert out["results"][0]["path"] == "Finance/q3.md"


def test_tool_says_no_good_answer_explicitly_when_nothing_found(configured_retrieval, monkeypatch):
    monkeypatch.setattr(config, "VAULT_SCOPE_ROOT", "")
    out = json.loads(vault_search_semantic(query="anything at all"))
    assert out["no_good_answer"] is True
    assert out["results"] == []
    assert "note" in out  # explicit visible message per §3.3, not silent


def test_tool_confines_scoped_server_and_reroots_returned_paths(configured_retrieval, monkeypatch):
    store, embedder = configured_retrieval
    vec = embedder.embed(["hawk confidential plan"])[0]
    store.apply("Hawk/plan.md", [_chunk("hawk confidential plan", "h1", ("Hawk/plan.md",))], {"h1": vec})
    vec2 = embedder.embed(["general open note"])[0]
    store.apply("General/open.md", [_chunk("general open note", "h2", ("General/open.md",))], {"h2": vec2})

    monkeypatch.setattr(config, "VAULT_SCOPE_ROOT", "General")

    out = json.loads(vault_search_semantic(query="hawk confidential plan"))
    # A server scoped to General, even queried with Hawk content word-for-word,
    # must never surface it.
    assert all(r["path"] != "Hawk/plan.md" for r in out["results"])
    assert all("Hawk" not in r["path"] for r in out["results"])

    out2 = json.loads(vault_search_semantic(query="general open note"))
    assert out2["results"][0]["path"] == "open.md"  # re-rooted, not "General/open.md"
