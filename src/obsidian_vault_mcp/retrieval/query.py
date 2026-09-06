"""Query orchestration: confidence cutoff, diversification, and quoted-phrase
filters layered on top of the store's vector similarity search.

Per RETRIEVAL-DESIGN.md:
- §3.1 -- folder-scoping is a straight pass-through of `user_scope` into the
  store's own query-time filter. This module does not re-derive or
  second-guess authorization; it reuses whatever scope string the caller
  (the MCP tool, which calls `identity.require_user()`) already resolved,
  exactly like every other tool in this codebase.
- §3.3 -- "nothing relevant found" is a first-class, visible outcome
  (`no_good_answer`), never a silent fallback.
- §3.4 -- diversify by parent entry so one long note doesn't crowd out
  everything else, backfilling with duplicates only if the result set would
  otherwise be smaller than requested.
- §4.3 -- a lightweight "searched N chunks across M files" status, separate
  from the answer itself.
"""

import re
from dataclasses import dataclass

from .embeddings import Embedder
from .store import RetrievalStore

DEFAULT_TOP_K = 10
# sqlite-vec's vec0 default distance metric is L2. This is intentionally a
# raw distance threshold, not a derived 0-1 "similarity" score (RETRIEVAL-
# DESIGN.md §3.3 calls the right threshold model-dependent and tunable, not
# a fixed universal constant) -- callers/config own the real value; this is
# only a fallback for direct use.
#
# 0.85, empirically calibrated (2026-09-06) against nomic-embed-text's real
# distance distribution on Jett's actual vault, not a generic guess: genuine
# matches for real queries ("how is the Avanti internship going" -> Avanti.md)
# landed at 0.69-0.78; deliberately absurd/unrelated queries ("boiling point
# of liquid nitrogen on Jupiter") landed at 0.87-0.94. An earlier default of
# 0.6 sat entirely below the real relevant-match range, silently forcing
# no_good_answer on every real query regardless of what was actually in the
# index -- caught by running the 20-query verification script for real
# rather than trusting the design doc's example number.
DEFAULT_MAX_DISTANCE = 0.85

# Fetch this many times top_k candidates from the store before diversifying,
# so there's actually a pool of different parent entries to diversify across.
_OVERFETCH_FACTOR = 4

_QUOTED_RE = re.compile(r'(-)?"([^"]+)"')


@dataclass(frozen=True)
class SemanticSearchResult:
    file_path: str
    text: str
    heading_path: tuple[str, ...]
    line_start: int
    parent_id: str
    distance: float


@dataclass(frozen=True)
class SemanticSearchResponse:
    results: list[SemanticSearchResult]
    query_text: str
    no_good_answer: bool
    searched_summary: str


def parse_query_phrases(query: str) -> tuple[str, str | None, str | None]:
    """Extract an optional must-contain phrase (`"exact phrase"`) and an
    optional must-not-contain phrase (`-"exact phrase"`) from a natural-
    language query (§3.2). Returns (remaining_text, must_contain, must_not).

    Only the first of each kind is honored -- this is a simple, deliberate,
    user-controlled narrowing tool, not a general query-language parser.
    """
    must_contain = None
    must_not_contain = None

    def _consume(m: re.Match) -> str:
        nonlocal must_contain, must_not_contain
        negated, phrase = m.group(1), m.group(2)
        if negated:
            if must_not_contain is None:
                must_not_contain = phrase
        else:
            if must_contain is None:
                must_contain = phrase
        return ""

    remaining = _QUOTED_RE.sub(_consume, query)
    remaining = re.sub(r"\s+", " ", remaining).strip()
    return remaining, must_contain, must_not_contain


def _diversify(candidates: list, top_k: int) -> list:
    """Keep at most one candidate per parent_id (the best-scoring one,
    since `candidates` arrives already sorted by distance), then backfill
    with next-best duplicates only if still short of `top_k`.
    """
    best_per_parent: dict[str, object] = {}
    order: list[str] = []
    for c in candidates:
        if c.parent_id not in best_per_parent:
            best_per_parent[c.parent_id] = c
            order.append(c.parent_id)

    diversified = [best_per_parent[pid] for pid in order]
    if len(diversified) >= top_k:
        return diversified[:top_k]

    seen_ids = {id(c) for c in diversified}
    for c in candidates:
        if len(diversified) >= top_k:
            break
        if id(c) in seen_ids:
            continue
        diversified.append(c)
        seen_ids.add(id(c))

    return diversified[:top_k]


def _searched_summary(results: list[SemanticSearchResult]) -> str:
    if not results:
        return "Searched the vault; found nothing above the confidence cutoff."
    files = sorted({r.file_path for r in results})
    headings = [r.heading_path[-1] for r in results if len(r.heading_path) > 0]
    return (
        f"Searched {len(results)} matching chunk(s) across {len(files)} file(s): "
        + ", ".join(headings[:5])
    )


def semantic_search(
    query: str,
    embedder: Embedder,
    store: RetrievalStore,
    user_scope: str,
    top_k: int = DEFAULT_TOP_K,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    frontmatter_field: str | None = None,
    frontmatter_value: str | None = None,
) -> SemanticSearchResponse:
    remaining_text, must_contain, must_not_contain = parse_query_phrases(query)
    embed_text = remaining_text or query

    [query_vector] = embedder.embed([embed_text])

    candidates = store.query(
        query_vector,
        path_prefix=user_scope,
        top_k=top_k * _OVERFETCH_FACTOR,
        max_distance=max_distance,
        must_contain=must_contain,
        must_not_contain=must_not_contain,
        frontmatter_field=frontmatter_field,
        frontmatter_value=frontmatter_value,
    )

    diversified = _diversify(candidates, top_k)

    results = [
        SemanticSearchResult(
            file_path=r.file_path,
            text=r.text,
            heading_path=r.heading_path,
            line_start=r.line_start,
            parent_id=r.parent_id,
            distance=r.distance,
        )
        for r in diversified
    ]

    return SemanticSearchResponse(
        results=results,
        query_text=query,
        no_good_answer=(len(results) == 0),
        searched_summary=_searched_summary(results),
    )
