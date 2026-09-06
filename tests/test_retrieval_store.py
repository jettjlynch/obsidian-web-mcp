"""Tests for the sqlite-vec-backed chunk store.

Per RETRIEVAL-DESIGN.md §2.2-2.4 and §3.1: one row per chunk holding text,
metadata, and embedding together; content-hash diffing per file for
incremental re-indexing; folder-scoping enforced as a query-time filter
INSIDE the retrieval query, never as a post-hoc pass over already-fetched
results.
"""

import pytest

from obsidian_vault_mcp.retrieval.chunker import Chunk
from obsidian_vault_mcp.retrieval.store import RetrievalStore

DIM = 4


def _chunk(text, parent_id="p1", heading_path=("f.md",), line_start=1, content_hash=None, frontmatter=None):
    import hashlib
    h = content_hash or hashlib.sha256(text.encode()).hexdigest()
    return Chunk(
        text=text,
        parent_id=parent_id,
        heading_path=heading_path,
        line_start=line_start,
        content_hash=h,
        frontmatter=frontmatter or {},
    )


def _vec(*xs):
    return list(xs) + [0.0] * (DIM - len(xs))


@pytest.fixture
def store(tmp_path):
    s = RetrievalStore(tmp_path / "retrieval.sqlite", embedding_dim=DIM)
    yield s
    s.close()


# --- diff / incremental re-indexing --------------------------------------

def test_initial_diff_all_chunks_need_embedding(store):
    chunks = [_chunk("alpha text"), _chunk("beta text", content_hash="hash-b")]
    to_embed, reused = store.diff("note.md", chunks)
    assert {c.content_hash for c in to_embed} == {c.content_hash for c in chunks}
    assert reused == []


def test_unchanged_chunks_need_no_re_embedding_after_apply(store):
    chunks = [_chunk("alpha text", content_hash="hash-a"), _chunk("beta text", content_hash="hash-b")]
    store.apply("note.md", chunks, {"hash-a": _vec(1, 0), "hash-b": _vec(0, 1)})

    # Re-run chunking on IDENTICAL content -> identical hashes.
    to_embed, reused = store.diff("note.md", chunks)
    assert to_embed == []
    assert {c.content_hash for c in reused} == {"hash-a", "hash-b"}


def test_one_changed_chunk_only_that_one_needs_re_embedding(store):
    original = [_chunk("alpha text", content_hash="hash-a"), _chunk("beta text", content_hash="hash-b")]
    store.apply("note.md", original, {"hash-a": _vec(1, 0), "hash-b": _vec(0, 1)})

    edited = [_chunk("alpha text", content_hash="hash-a"), _chunk("beta text CHANGED", content_hash="hash-b2")]
    to_embed, reused = store.diff("note.md", edited)
    assert {c.content_hash for c in to_embed} == {"hash-b2"}
    assert {c.content_hash for c in reused} == {"hash-a"}


def test_apply_reuses_existing_embedding_for_unchanged_hash(store):
    chunks = [_chunk("alpha text", content_hash="hash-a")]
    store.apply("note.md", chunks, {"hash-a": _vec(1, 0, 0, 0)})

    # Re-apply with the SAME hash but no new embedding supplied for it --
    # must not error, must keep serving the original vector.
    store.apply("note.md", chunks, {})
    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=5)
    assert len(results) == 1
    assert results[0].content_hash == "hash-a"


def test_apply_deletes_chunks_whose_hash_no_longer_present(store):
    original = [_chunk("alpha", content_hash="hash-a"), _chunk("beta", content_hash="hash-b")]
    store.apply("note.md", original, {"hash-a": _vec(1, 0), "hash-b": _vec(0, 1)})

    edited = [_chunk("alpha", content_hash="hash-a")]  # beta removed/merged away
    store.apply("note.md", edited, {})

    results = store.query(_vec(0, 1, 0, 0), path_prefix="", top_k=10)
    assert all(r.content_hash != "hash-b" for r in results)


def test_delete_file_removes_all_its_chunks(store):
    chunks = [_chunk("alpha", content_hash="hash-a"), _chunk("beta", content_hash="hash-b")]
    store.apply("note.md", chunks, {"hash-a": _vec(1, 0), "hash-b": _vec(0, 1)})
    store.delete_file("note.md")

    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=10)
    assert results == []


def test_identical_hash_in_two_different_files_does_not_cross_contaminate(store):
    # Same content_hash used in two unrelated files -- per §2.3's last point,
    # hash bookkeeping is per-file, so editing/removing it in one file must
    # never delete the other file's chunk.
    shared_hash = "shared-hash"
    store.apply("fileA.md", [_chunk("same text", content_hash=shared_hash)], {shared_hash: _vec(1, 0)})
    store.apply("fileB.md", [_chunk("same text", content_hash=shared_hash)], {shared_hash: _vec(1, 0)})

    # Remove it from fileA only.
    store.apply("fileA.md", [], {})

    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=10)
    assert any(r.file_path == "fileB.md" for r in results)
    assert all(r.file_path != "fileA.md" for r in results)


# --- folder-scoping (query-time, structural) ------------------------------

def test_query_scoped_to_prefix_excludes_other_folders(store):
    store.apply("Hawk/secret.md", [_chunk("hawk content", content_hash="h1")], {"h1": _vec(1, 0, 0, 0)})
    store.apply("General/open.md", [_chunk("general content", content_hash="h2")], {"h2": _vec(1, 0, 0, 0.01)})

    results = store.query(_vec(1, 0, 0, 0), path_prefix="General", top_k=10)
    assert all(r.file_path.startswith("General/") for r in results)
    assert all(r.file_path != "Hawk/secret.md" for r in results)


def test_scope_prefix_does_not_match_sibling_folder_with_shared_prefix(tmp_path):
    s = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        s.apply("General/open.md", [_chunk("in scope", content_hash="hg")], {"hg": _vec(1, 0, 0, 0)})
        s.apply("GeneralOther/leak.md", [_chunk("out of scope", content_hash="ho")], {"ho": _vec(1, 0, 0, 0)})

        results = s.query(_vec(1, 0, 0, 0), path_prefix="General", top_k=10)
        assert all(r.file_path != "GeneralOther/leak.md" for r in results)
        assert any(r.file_path == "General/open.md" for r in results)
    finally:
        s.close()


def test_scope_exact_file_match_at_scope_root(tmp_path):
    """A user scoped to 'General' should also see a file literally named
    'General' at the vault root (edge case), not only files under 'General/'."""
    s = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        s.apply("General", [_chunk("root-level file named General", content_hash="hr")], {"hr": _vec(1, 0, 0, 0)})
        results = s.query(_vec(1, 0, 0, 0), path_prefix="General", top_k=10)
        assert any(r.file_path == "General" for r in results)
    finally:
        s.close()


def test_query_with_no_scope_match_returns_empty_not_unfiltered(store):
    store.apply("Hawk/secret.md", [_chunk("hawk content", content_hash="h1")], {"h1": _vec(1, 0, 0, 0)})
    results = store.query(_vec(1, 0, 0, 0), path_prefix="NoSuchScope", top_k=10)
    assert results == []


def test_empty_prefix_means_unrestricted_root_access(store):
    store.apply("Hawk/secret.md", [_chunk("hawk content", content_hash="h1")], {"h1": _vec(1, 0, 0, 0)})
    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=10)
    assert any(r.file_path == "Hawk/secret.md" for r in results)


# --- structured filters layered on top of similarity ----------------------

def test_must_contain_phrase_filter_narrows_results(store):
    store.apply(
        "note.md",
        [
            _chunk("no single point of failure here", content_hash="h1"),
            _chunk("totally unrelated other content", content_hash="h2"),
        ],
        {"h1": _vec(1, 0, 0, 0), "h2": _vec(1, 0, 0, 0.001)},
    )
    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=10, must_contain="single point of failure")
    assert len(results) == 1
    assert "single point of failure" in results[0].text


def test_must_not_contain_phrase_filter_excludes_matches(store):
    store.apply(
        "note.md",
        [
            _chunk("draft status content", content_hash="h1"),
            _chunk("final status content", content_hash="h2"),
        ],
        {"h1": _vec(1, 0, 0, 0), "h2": _vec(1, 0, 0, 0.001)},
    )
    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=10, must_not_contain="draft")
    assert all("draft" not in r.text for r in results)


def test_frontmatter_field_filter_narrows_results(store):
    store.apply(
        "a.md",
        [_chunk("content a", content_hash="ha", frontmatter={"status": "active"})],
        {"ha": _vec(1, 0, 0, 0)},
    )
    store.apply(
        "b.md",
        [_chunk("content b", content_hash="hb", frontmatter={"status": "draft"})],
        {"hb": _vec(1, 0, 0, 0.001)},
    )
    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=10, frontmatter_field="status", frontmatter_value="active")
    assert len(results) == 1
    assert results[0].file_path == "a.md"


# --- similarity cutoff -----------------------------------------------------

def test_similarity_cutoff_excludes_weak_matches(store):
    store.apply(
        "note.md",
        [
            _chunk("close match", content_hash="h1"),
            _chunk("far match", content_hash="h2"),
        ],
        {"h1": _vec(1, 0, 0, 0), "h2": _vec(0, 0, 0, 1)},
    )
    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=10, max_distance=0.5)
    file_hashes = {r.content_hash for r in results}
    assert "h1" in file_hashes
    assert "h2" not in file_hashes


# --- real-world frontmatter shapes (found live against Jett's vault) -------

def test_frontmatter_with_a_date_value_does_not_crash_apply(store):
    """python-frontmatter/PyYAML parses an unquoted YAML date (e.g.
    `date: 2026-06-05`) into a real datetime.date object, not a string --
    common in Jett's real notes. json.dumps chokes on that by default."""
    import datetime
    chunks = [_chunk("session notes", content_hash="hd", frontmatter={"date": datetime.date(2026, 6, 5)})]
    store.apply("note.md", chunks, {"hd": _vec(1, 0, 0, 0)})

    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=5)
    assert len(results) == 1
    assert "2026-06-05" in str(results[0].frontmatter["date"])


def test_duplicate_content_hash_within_one_apply_call_does_not_crash(store):
    """Two chunks chunked from the same real file can end up with identical
    embedded text (e.g. a short repeated log line) -- both new to the store
    in the same apply() call. This must not violate the (file_path,
    content_hash) unique constraint; §2.3's hash bookkeeping identifies
    content, and identical content collapses to one stored row."""
    chunks = [
        _chunk("duplicate line", content_hash="dup", parent_id="p1", line_start=1),
        _chunk("duplicate line", content_hash="dup", parent_id="p2", line_start=50),
    ]
    store.apply("note.md", chunks, {"dup": _vec(1, 0, 0, 0)})  # must not raise

    results = store.query(_vec(1, 0, 0, 0), path_prefix="", top_k=5)
    assert len([r for r in results if r.content_hash == "dup"]) == 1
