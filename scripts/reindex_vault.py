#!/usr/bin/env python3
"""Explicitly (re)build the semantic retrieval index for the whole vault.

Per RETRIEVAL-DESIGN.md §2.3: a full "regenerate everything" path must exist
and be explicitly invokable (first-time bootstrap, a model change, or
recovery if the index and vault ever visibly drift apart) -- it must NEVER
be what runs automatically on every save. That incremental path is
`RetrievalIndexer.index_file`, wired into the frontmatter watcher in
server.py's main(); this script is the one place `full_reindex()` is called.

Usage (run from the repo root):
    uv run python scripts/reindex_vault.py
    VAULT_PATH=~/other-vault uv run python scripts/reindex_vault.py

Reads the same env vars as the server: VAULT_PATH, RETRIEVAL_EMBEDDING_BACKEND
(default "ollama" -- local, no key needed; "voyage" needs VOYAGE_API_KEY),
RETRIEVAL_DB_PATH, RETRIEVAL_EMBED_MODEL, RETRIEVAL_EMBED_DIM. On the local
Ollama backend this costs time, not money -- everything runs on this
machine. On the Voyage backend it costs real API calls proportional to the
corpus's chunk count on a first run (an already-populated store only
re-embeds what changed since last run, same as the live indexer would).
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reindex_vault")


def _build_embedder(config):
    if config.RETRIEVAL_EMBEDDING_BACKEND == "voyage":
        from obsidian_vault_mcp.retrieval.embeddings import VoyageEmbedder
        if not config.VOYAGE_API_KEY:
            logger.error("RETRIEVAL_EMBEDDING_BACKEND=voyage but VOYAGE_API_KEY is not set.")
            sys.exit(1)
        return VoyageEmbedder(
            api_key=config.VOYAGE_API_KEY, model=config.RETRIEVAL_EMBED_MODEL, dimension=config.RETRIEVAL_EMBED_DIM,
        )
    from obsidian_vault_mcp.retrieval.embeddings import OllamaEmbedder
    return OllamaEmbedder(model=config.RETRIEVAL_EMBED_MODEL, dimension=config.RETRIEVAL_EMBED_DIM, host=config.OLLAMA_HOST)


def main() -> None:
    from obsidian_vault_mcp import config
    from obsidian_vault_mcp.retrieval.indexer import RetrievalIndexer
    from obsidian_vault_mcp.retrieval.store import RetrievalStore

    if not config.VAULT_PATH.is_dir():
        logger.error(f"VAULT_PATH does not exist: {config.VAULT_PATH}")
        sys.exit(1)

    config.RETRIEVAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Vault:      {config.VAULT_PATH}")
    logger.info(f"Index db:   {config.RETRIEVAL_DB_PATH}")
    logger.info(f"Backend:    {config.RETRIEVAL_EMBEDDING_BACKEND}")
    logger.info(f"Model:      {config.RETRIEVAL_EMBED_MODEL} (dim={config.RETRIEVAL_EMBED_DIM})")

    store = RetrievalStore(config.RETRIEVAL_DB_PATH, embedding_dim=config.RETRIEVAL_EMBED_DIM)
    embedder = _build_embedder(config)
    indexer = RetrievalIndexer(store=store, embedder=embedder, vault_root=config.VAULT_PATH)

    t0 = time.monotonic()
    indexer.full_reindex()
    elapsed = time.monotonic() - t0

    total_chunks = store._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    total_files = store._conn.execute("SELECT COUNT(DISTINCT file_path) FROM chunks").fetchone()[0]
    logger.info(f"Done in {elapsed:.1f}s -- {total_files} files, {total_chunks} chunks indexed.")

    store.close()


if __name__ == "__main__":
    main()
