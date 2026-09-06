"""Incremental indexer: ties the chunker, the embedder, and the store together.

Per RETRIEVAL-DESIGN.md §2.3, this is the load-bearing piece: re-chunking a
file always produces its full current chunk set, but only chunks whose
content hash is genuinely new get sent to the embedder -- everything else
reuses its already-stored vector. A one-line edit costs one embedding call,
not a whole-file re-embed.

This module knows nothing about *when* to run (that's the file-watcher hook
in frontmatter_index.py, extended rather than duplicated -- see its
`add_change_listener`) -- it only knows how to bring one file, or the whole
vault, up to date given the current content on disk.
"""

import logging
from pathlib import Path

from .. import config
from .chunker import chunk_markdown
from .embeddings import Embedder
from .store import RetrievalStore

logger = logging.getLogger(__name__)


class RetrievalIndexer:
    def __init__(self, store: RetrievalStore, embedder: Embedder, vault_root: Path):
        self._store = store
        self._embedder = embedder
        self._vault_root = Path(vault_root)

    def index_file(self, relative_path: str) -> None:
        """Bring one file's stored chunks up to date with its current content.

        Safe to call unconditionally on every file-watcher event (create,
        modify) -- if nothing actually changed, this makes zero embedding
        calls (§2.3).
        """
        abs_path = self._vault_root / relative_path
        if not abs_path.is_file():
            self.delete_file(relative_path)
            return

        try:
            content = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            logger.warning("retrieval indexer: could not read %s: %s", relative_path, e)
            return

        chunks = chunk_markdown(relative_path, content)
        to_embed, _reused = self._store.diff(relative_path, chunks)

        # Two chunks can land on identical embedded text within one file
        # (a genuinely repeated line, found live against Jett's vault) --
        # dedupe by hash before calling the embedder so identical text is
        # never sent twice in the same batch (store.apply() separately
        # guards against the resulting duplicate-hash row on the storage side).
        unique_to_embed: dict[str, "Chunk"] = {}
        for c in to_embed:
            unique_to_embed.setdefault(c.content_hash, c)

        new_embeddings: dict[str, list[float]] = {}
        if unique_to_embed:
            hashes = list(unique_to_embed.keys())
            vectors = self._embedder.embed([unique_to_embed[h].text for h in hashes])
            new_embeddings = dict(zip(hashes, vectors))

        self._store.apply(relative_path, chunks, new_embeddings)

    def delete_file(self, relative_path: str) -> None:
        """Remove a file's chunks entirely -- it was deleted from the vault."""
        self._store.delete_file(relative_path)

    def full_reindex(self) -> None:
        """Regenerate the index for every Markdown file in the vault.

        Per §2.3's closing note: this path must exist and be explicitly
        invokable (a model change, or recovery if the index and vault ever
        visibly drift apart), but it is never what runs automatically on
        every save -- `index_file` is.
        """
        count = 0
        for md_path in self._vault_root.rglob("*.md"):
            if any(part in config.EXCLUDED_DIRS for part in md_path.relative_to(self._vault_root).parts):
                continue
            rel = str(md_path.relative_to(self._vault_root))
            self.index_file(rel)
            count += 1
        logger.info("Retrieval full re-index complete: %d files", count)
