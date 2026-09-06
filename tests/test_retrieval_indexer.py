"""Tests for the incremental indexer.

Per RETRIEVAL-DESIGN.md §2.3: this is "one of the most operationally
important design decisions in this whole document" -- a one-line edit to a
large note must cost one embedding call, not a whole-file re-embed, and an
append-only file (like our Daily/YYYY-MM-DD.md living-brain notes) must cost
roughly one embedding call per append.
"""

from pathlib import Path

import pytest

from obsidian_vault_mcp.retrieval.embeddings import DeterministicFakeEmbedder
from obsidian_vault_mcp.retrieval.indexer import RetrievalIndexer
from obsidian_vault_mcp.retrieval.store import RetrievalStore


class _CountingEmbedder:
    """Wraps a fake embedder and records every text it was ever asked to embed."""

    def __init__(self, dimension=16):
        self._inner = DeterministicFakeEmbedder(dimension=dimension)
        self.dimension = dimension
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return self._inner.embed(texts)

    @property
    def total_texts_embedded(self) -> int:
        return sum(len(c) for c in self.calls)


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture
def indexer_setup(tmp_path, vault):
    embedder = _CountingEmbedder(dimension=16)
    store = RetrievalStore(tmp_path / "retrieval.sqlite", embedding_dim=16)
    indexer = RetrievalIndexer(store=store, embedder=embedder, vault_root=vault)
    yield indexer, store, embedder, vault
    store.close()


def test_index_file_makes_new_content_queryable(indexer_setup):
    indexer, store, embedder, vault = indexer_setup
    (vault / "note.md").write_text("# Title\n\nSome content about revenue.\n")

    indexer.index_file("note.md")

    qvec = embedder.embed(["revenue"])[0]
    results = store.query(qvec, path_prefix="", top_k=5)
    assert any("revenue" in r.text for r in results)


def test_reindexing_identical_content_makes_zero_embedding_calls(indexer_setup):
    indexer, store, embedder, vault = indexer_setup
    (vault / "note.md").write_text("# Title\n\nSome content about revenue.\n")
    indexer.index_file("note.md")
    embedder.calls.clear()

    indexer.index_file("note.md")  # nothing changed on disk

    assert embedder.calls == []


def test_appending_one_line_to_large_file_embeds_only_the_new_content(indexer_setup):
    indexer, store, embedder, vault = indexer_setup
    original = "# Daily\n\n" + "".join(
        f"## Entry {i}\n\nSomething happened in entry {i}.\n\n" for i in range(10)
    )
    (vault / "Daily.md").write_text(original)
    indexer.index_file("Daily.md")
    embedder.calls.clear()

    appended = original + "## Entry 10\n\nA brand new appended entry just now.\n"
    (vault / "Daily.md").write_text(appended)
    indexer.index_file("Daily.md")

    # Only the new tail content should have been sent to the embedder --
    # not a re-embed of all 10 prior entries.
    all_embedded_text = " ".join(t for call in embedder.calls for t in call)
    assert "brand new appended entry" in all_embedded_text
    assert "Something happened in entry 0" not in all_embedded_text
    assert embedder.total_texts_embedded == 1


def test_editing_one_section_only_re_embeds_that_section(indexer_setup):
    indexer, store, embedder, vault = indexer_setup
    content = (
        "# Doc\n\n"
        "## Alpha\n\nAlpha original text.\n\n"
        "## Beta\n\nBeta original text.\n"
    )
    (vault / "note.md").write_text(content)
    indexer.index_file("note.md")
    embedder.calls.clear()

    edited = content.replace("Beta original text.", "Beta EDITED text.")
    (vault / "note.md").write_text(edited)
    indexer.index_file("note.md")

    all_embedded_text = " ".join(t for call in embedder.calls for t in call)
    assert "Beta EDITED" in all_embedded_text
    assert "Alpha original" not in all_embedded_text


def test_removed_section_is_deleted_from_store_without_re_embedding_survivors(indexer_setup):
    indexer, store, embedder, vault = indexer_setup
    content = (
        "# Doc\n\n"
        "## Alpha\n\nAlpha text.\n\n"
        "## Beta\n\nBeta text.\n"
    )
    (vault / "note.md").write_text(content)
    indexer.index_file("note.md")
    embedder.calls.clear()

    (vault / "note.md").write_text("# Doc\n\n## Alpha\n\nAlpha text.\n")
    indexer.index_file("note.md")

    assert embedder.calls == []  # nothing NEW to embed, only a removal
    results = store.query([0.1] * 16, path_prefix="", top_k=10, max_distance=1e9)
    assert all("Beta text" not in r.text for r in results)
    assert any("Alpha text" in r.text for r in results)


def test_duplicate_chunk_text_in_one_file_embeds_only_once(indexer_setup, monkeypatch):
    """Two chunks that happen to chunk down to identical embedded text (a
    real repeated log line, found live against Jett's vault -- e.g. a
    repeated status line inside one oversized section) must not cost two
    embedding API calls for the same text, and must not crash on the
    (file_path, content_hash) unique constraint (covered end-to-end at the
    store layer in test_retrieval_store.py; this proves the indexer doesn't
    even ask the embedder to do duplicate work)."""
    indexer, store, embedder, vault = indexer_setup
    (vault / "log.md").write_text("# Log\n\nplaceholder\n")

    from obsidian_vault_mcp.retrieval.chunker import Chunk
    duplicate_chunks = [
        Chunk(text="same recurring line", parent_id="p1", heading_path=("log.md",), line_start=1, content_hash="dup-hash"),
        Chunk(text="same recurring line", parent_id="p1", heading_path=("log.md",), line_start=50, content_hash="dup-hash"),
    ]
    monkeypatch.setattr(
        "obsidian_vault_mcp.retrieval.indexer.chunk_markdown",
        lambda path, content, **kw: duplicate_chunks,
    )

    indexer.index_file("log.md")  # must not raise (UNIQUE constraint)

    # The same text string must never be sent to the embedder twice in one call.
    for call in embedder.calls:
        assert len(call) == len(set(call))


def test_delete_file_removes_its_chunks(indexer_setup):
    indexer, store, embedder, vault = indexer_setup
    (vault / "note.md").write_text("# Title\n\nSome content.\n")
    indexer.index_file("note.md")

    indexer.delete_file("note.md")

    results = store.query([0.1] * 16, path_prefix="", top_k=10, max_distance=1e9)
    assert results == []


def test_full_reindex_covers_every_markdown_file_and_skips_excluded_dirs(indexer_setup):
    indexer, store, embedder, vault = indexer_setup
    (vault / "a.md").write_text("# A\n\nAlpha content.\n")
    sub = vault / "sub"
    sub.mkdir()
    (sub / "b.md").write_text("# B\n\nBravo content.\n")
    obsidian = vault / ".obsidian"
    obsidian.mkdir()
    (obsidian / "config.md").write_text("# Config\n\nShould be skipped.\n")

    indexer.full_reindex()

    results = store.query([0.1] * 16, path_prefix="", top_k=50, max_distance=1e9)
    paths = {r.file_path for r in results}
    assert "a.md" in paths
    assert "sub/b.md" in paths
    assert not any(".obsidian" in p for p in paths)
