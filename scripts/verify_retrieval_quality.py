#!/usr/bin/env python3
"""Compare the OLD grep/frontmatter search against the NEW semantic search
on 20 real queries phrased the way Jett would actually ask them (not the
literal keywords his notes use) -- the whole point of adding semantic
retrieval (RETRIEVAL-DESIGN.md's opening rationale).

Usage:
    VOYAGE_API_KEY=... uv run python scripts/verify_retrieval_quality.py
    # Grep-only baseline, no API key needed / no cost:
    uv run python scripts/verify_retrieval_quality.py --grep-only

This does NOT auto-score "which is better" -- that's a real judgment call
for a human reading real vault content, not something to fabricate a number
for. It prints both result sets per query so a human can compare them
directly. It DOES report one hard automatic signal: queries where grep
found literally nothing (a phrasing miss) but semantic search found
something above the confidence cutoff -- the case semantic search exists to
fix.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
logging.basicConfig(level=logging.WARNING)

# 20 real queries about Jett's actual vault content (Avanti/Kyle/Debbie work,
# HTS/Seth Lloyd pipeline, TWR podcast, business admin, personal Insights/
# Daily reflection notes), deliberately phrased in different words than the
# notes themselves use -- exact-keyword grep is expected to miss several of
# these; that gap is exactly what semantic search is for.
QUERIES = [
    "what did we agree with Kyle about Phoenix 3",
    "how is the Avanti internship going",
    "what's the status of Seth Lloyd as a prospect",
    "any notes on pricing the high ticket offer",
    "what guests are lined up for the podcast",
    "anything about UK company compliance deadlines",
    "did I ever write about feeling insecure or black and white thinking",
    "what happened in therapy recently",
    "notes on the Japan trip planning",
    "what's Debbie's role at Avanti",
    "anything about the vault-mcp security audit findings",
    "did I decide anything about the archive UID bug",
    "what's the plan for the Hawk workstream access model",
    "notes about stoicism or philosophy I've been reading",
    "what did I say about trusting the process",
    "anything about property development in Whitstable",
    "what's blocking the offer engine sprint",
    "notes on the iMessage bot humanize work",
    "what did the weekly review say about this month",
    "anything about tax and how I get paid",
]

assert len(QUERIES) == 20, f"expected exactly 20 queries, got {len(QUERIES)}"


def run_grep(query: str, vault_root: Path, max_results: int = 5) -> list[dict]:
    """Mirror tools/search.py's vault_search: naive substring match per
    significant word, since ripgrep needs a single literal/regex term and
    these are full natural-language questions."""
    import re
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", query.lower())]
    stop = {"what", "does", "with", "about", "have", "anything", "notes", "recently", "this", "that", "were", "been"}
    keywords = [w for w in words if w not in stop][:4]

    hits: list[dict] = []
    for md_path in vault_root.rglob("*.md"):
        if "graphify-out" in md_path.parts or ".obsidian" in md_path.parts:
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        score = sum(1 for k in keywords if k in text)
        if score > 0:
            hits.append({"path": str(md_path.relative_to(vault_root)), "keyword_hits": score})
        if len(hits) >= max_results * 4:
            break
    hits.sort(key=lambda h: -h["keyword_hits"])
    return hits[:max_results]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grep-only", action="store_true", help="Skip semantic search (no API key needed)")
    args = parser.parse_args()

    from obsidian_vault_mcp import config

    vault_root = config.VAULT_PATH
    if not vault_root.is_dir():
        print(f"VAULT_PATH does not exist: {vault_root}", file=sys.stderr)
        sys.exit(1)

    semantic_fn = None
    if not args.grep_only:
        from obsidian_vault_mcp.retrieval.indexer import RetrievalIndexer
        from obsidian_vault_mcp.retrieval.query import semantic_search
        from obsidian_vault_mcp.retrieval.store import RetrievalStore

        if config.RETRIEVAL_EMBEDDING_BACKEND == "voyage" and not config.VOYAGE_API_KEY:
            print(
                "RETRIEVAL_EMBEDDING_BACKEND=voyage but VOYAGE_API_KEY is not set -- "
                "run with --grep-only, or set the key, or unset the backend to use local Ollama.",
                file=sys.stderr,
            )
            sys.exit(1)

        if config.RETRIEVAL_EMBEDDING_BACKEND == "voyage":
            from obsidian_vault_mcp.retrieval.embeddings import VoyageEmbedder
            embedder = VoyageEmbedder(
                api_key=config.VOYAGE_API_KEY, model=config.RETRIEVAL_EMBED_MODEL, dimension=config.RETRIEVAL_EMBED_DIM,
            )
        else:
            from obsidian_vault_mcp.retrieval.embeddings import OllamaEmbedder
            embedder = OllamaEmbedder(
                model=config.RETRIEVAL_EMBED_MODEL, dimension=config.RETRIEVAL_EMBED_DIM, host=config.OLLAMA_HOST,
            )

        store = RetrievalStore(config.RETRIEVAL_DB_PATH, embedding_dim=config.RETRIEVAL_EMBED_DIM)
        indexer = RetrievalIndexer(store=store, embedder=embedder, vault_root=vault_root)
        print("Ensuring the index is up to date (only changed chunks get embedded)...")
        t0 = time.monotonic()
        indexer.full_reindex()
        print(f"  index ready in {time.monotonic() - t0:.1f}s")

        def semantic_fn(q):
            resp = semantic_search(query=q, embedder=embedder, store=store, user_scope="", top_k=5)
            return resp

    fixed_wins = 0
    for i, query in enumerate(QUERIES, 1):
        print(f"\n{'=' * 100}\n[{i}/20] {query!r}\n{'=' * 100}")

        grep_hits = run_grep(query, vault_root)
        print(f"\n  GREP (keyword match):")
        if grep_hits:
            for h in grep_hits:
                print(f"    - {h['path']}  ({h['keyword_hits']} keyword hits)")
        else:
            print("    (nothing found)")

        if semantic_fn:
            resp = semantic_fn(query)
            print(f"\n  SEMANTIC (meaning match):")
            if resp.results:
                for r in resp.results:
                    heading = r.heading_path[-1] if r.heading_path else ""
                    print(f"    - {r.file_path} :: {heading}  (distance={r.distance:.3f})")
            else:
                print(f"    (no_good_answer -- {resp.searched_summary})")

            if not grep_hits and resp.results:
                fixed_wins += 1
                print("    ^ grep found NOTHING here; semantic search did -- exactly the gap it's meant to close.")

    if semantic_fn:
        print(f"\n\nSummary: {fixed_wins}/20 queries where grep found nothing but semantic search found something.")
        store.close()
    else:
        print(
            "\n\nThis was the grep-only baseline. Re-run with VOYAGE_API_KEY set "
            "to see the semantic side and the real comparison."
        )


if __name__ == "__main__":
    main()
