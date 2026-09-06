"""Tests for the query orchestration layer: confidence cutoff + explicit
"nothing found" handling (§3.3), result diversification (§3.4), and
quoted-phrase extraction layered on top of vector similarity (§3.2).

Folder-scoping itself is NOT re-tested here beyond one wiring check --
its actual enforcement is store.py's job and is adversarially tested in
test_retrieval_store.py. This file exists to prove `semantic_search` passes
the user's scope straight through rather than reimplementing the check.
"""

from obsidian_vault_mcp.retrieval.chunker import Chunk
from obsidian_vault_mcp.retrieval.embeddings import DeterministicFakeEmbedder
from obsidian_vault_mcp.retrieval.query import parse_query_phrases, semantic_search
from obsidian_vault_mcp.retrieval.store import RetrievalStore

DIM = 8


def _chunk(text, parent_id, content_hash, heading_path=("f.md",), line_start=1):
    return Chunk(
        text=text, parent_id=parent_id, heading_path=heading_path,
        line_start=line_start, content_hash=content_hash, frontmatter={},
    )


# --- quoted-phrase extraction ---------------------------------------------

def test_parse_query_no_quotes_returns_query_unchanged():
    text, must, must_not = parse_query_phrases("what did we decide about the vendor")
    assert text == "what did we decide about the vendor"
    assert must is None
    assert must_not is None


def test_parse_query_extracts_must_contain_phrase():
    text, must, must_not = parse_query_phrases('find the clause "no single point of failure"')
    assert must == "no single point of failure"
    assert must_not is None
    assert "no single point of failure" not in text


def test_parse_query_extracts_must_not_contain_phrase():
    text, must, must_not = parse_query_phrases('status update -"draft"')
    assert must_not == "draft"
    assert must is None


# --- confidence cutoff + explicit "nothing found" -------------------------

def test_no_good_answer_when_everything_fails_the_cutoff(tmp_path):
    embedder = DeterministicFakeEmbedder(dimension=DIM)
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        far_vec = [1.0] + [0.0] * (DIM - 1)
        store.apply("note.md", [_chunk("irrelevant", "p1", "h1")], {"h1": far_vec})

        # A query embedding chosen to be maximally far from the only stored vector.
        response = semantic_search(
            query="completely unrelated topic",
            embedder=_FixedEmbedder([0.0] * (DIM - 1) + [1.0]),
            store=store,
            user_scope="",
            max_distance=0.01,
        )
        assert response.no_good_answer is True
        assert response.results == []
    finally:
        store.close()


def test_good_answer_when_something_survives_cutoff(tmp_path):
    embedder = DeterministicFakeEmbedder(dimension=DIM)
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        vec = [1.0] + [0.0] * (DIM - 1)
        store.apply("note.md", [_chunk("relevant content", "p1", "h1")], {"h1": vec})

        response = semantic_search(
            query="doesn't matter", embedder=_FixedEmbedder(vec), store=store,
            user_scope="", max_distance=1000.0,
        )
        assert response.no_good_answer is False
        assert len(response.results) == 1
    finally:
        store.close()


# --- diversification --------------------------------------------------------

def test_diversifies_across_parent_entries_before_padding_with_duplicates(tmp_path):
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        base = [1.0] + [0.0] * (DIM - 1)
        # Three chunks from the SAME parent (one long note split up)...
        store.apply(
            "long-note.md",
            [
                _chunk("same-parent chunk 1", "parentA", "ha1", line_start=1),
                _chunk("same-parent chunk 2", "parentA", "ha2", line_start=5),
                _chunk("same-parent chunk 3", "parentA", "ha3", line_start=9),
            ],
            {"ha1": base, "ha2": base, "ha3": base},
        )
        # ...and one chunk from a different note, slightly less similar.
        other_vec = [0.99] + [0.01] + [0.0] * (DIM - 2)
        store.apply("other-note.md", [_chunk("other note chunk", "parentB", "hb1")], {"hb1": other_vec})

        response = semantic_search(
            query="x", embedder=_FixedEmbedder(base), store=store,
            user_scope="", top_k=2, max_distance=1000.0,
        )

        assert len(response.results) == 2
        parents = {r.parent_id for r in response.results}
        # Diversification means the different-note chunk makes the cut even
        # though it's a slightly worse vector match than parentA's 2nd/3rd chunks.
        assert "parentB" in parents
        assert "parentA" in parents
    finally:
        store.close()


def test_backfills_with_duplicates_only_if_result_set_would_otherwise_be_small(tmp_path):
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        base = [1.0] + [0.0] * (DIM - 1)
        store.apply(
            "long-note.md",
            [
                _chunk("chunk 1", "parentA", "ha1"),
                _chunk("chunk 2", "parentA", "ha2"),
            ],
            {"ha1": base, "ha2": base},
        )
        response = semantic_search(
            query="x", embedder=_FixedEmbedder(base), store=store,
            user_scope="", top_k=2, max_distance=1000.0,
        )
        # Only one parent exists at all -- a second chunk from it is fine
        # rather than returning an artificially short result set.
        assert len(response.results) == 2
    finally:
        store.close()


# --- scope pass-through wiring ----------------------------------------------

def test_user_scope_is_passed_through_to_the_store_query(tmp_path):
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        vec = [1.0] + [0.0] * (DIM - 1)
        store.apply("Hawk/secret.md", [_chunk("hawk", "p1", "h1")], {"h1": vec})
        store.apply("General/open.md", [_chunk("general", "p2", "h2")], {"h2": vec})

        response = semantic_search(
            query="x", embedder=_FixedEmbedder(vec), store=store,
            user_scope="General", max_distance=1000.0,
        )
        assert all(r.file_path.startswith("General/") for r in response.results)
    finally:
        store.close()


# --- "show the search itself" (§4.3) ----------------------------------------

def test_response_includes_a_searched_summary(tmp_path):
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        vec = [1.0] + [0.0] * (DIM - 1)
        store.apply("note.md", [_chunk("some content", "p1", "h1", heading_path=("note.md", "Heading"))], {"h1": vec})
        response = semantic_search(
            query="x", embedder=_FixedEmbedder(vec), store=store,
            user_scope="", max_distance=1000.0,
        )
        assert "note.md" in response.searched_summary or "Heading" in response.searched_summary
    finally:
        store.close()


class _FixedEmbedder:
    """Test double that always returns the same vector, for exact distance control."""

    def __init__(self, vector):
        self._vector = vector
        self.dimension = len(vector)

    def embed(self, texts):
        return [self._vector for _ in texts]
