"""In-memory index of YAML frontmatter across all vault .md files."""

import logging
import threading
import time
from pathlib import Path
from typing import Callable

import frontmatter
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config

logger = logging.getLogger(__name__)

# (relative_path, exists) -- exists=False means the file was deleted (or
# never existed; the debounced path is stale by the time it's flushed).
ChangeListener = Callable[[str, bool], None]


class FrontmatterIndex:
    """Thread-safe in-memory index of YAML frontmatter for fast queries."""

    def __init__(self) -> None:
        self._index: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._observer: Observer | None = None
        self._debounce_timer: threading.Timer | None = None
        self._pending_paths: set[str] = set()
        # Extension point for RetrievalIndexer (retrieval/indexer.py) and
        # anything else that needs to react to vault file changes --
        # RETRIEVAL-DESIGN.md §2.3/§5 step 3 explicitly calls for extending
        # this existing watcher layer rather than standing up a second
        # watchdog.Observer on the same vault path (that collision is the
        # documented 2026-08-03 hang; see test_frontmatter_index_lifecycle.py).
        self._change_listeners: list[ChangeListener] = []

    def start(self) -> None:
        """Walk all .md files, parse frontmatter, and start watching for changes."""
        t0 = time.monotonic()
        count = 0

        for md_path in config.effective_vault_path().rglob("*.md"):
            if self._is_excluded(md_path):
                continue
            rel = str(md_path.relative_to(config.effective_vault_path()))
            fm = self._parse_frontmatter(md_path)
            if fm is not None:
                self._index[rel] = fm
                count += 1

        elapsed = time.monotonic() - t0
        logger.info(
            "Frontmatter index built: %d files in %.2f seconds", count, elapsed
        )

        self._observer = Observer()
        handler = _VaultEventHandler(self)
        self._observer.schedule(handler, str(config.effective_vault_path()), recursive=True)
        self._observer.start()

    def stop(self) -> None:
        """Stop the filesystem observer and cancel any pending debounce."""
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
            self._debounce_timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    @property
    def file_count(self) -> int:
        with self._lock:
            return len(self._index)

    @property
    def is_ready(self) -> bool:
        """Whether start() has completed its initial build and the
        filesystem observer is actively watching. Pure liveness signal for
        /health -- exposes no vault content, just "has this finished
        booting", so a public property rather than reaching into
        _observer directly from outside the class.
        """
        return self._observer is not None

    def add_change_listener(self, listener: ChangeListener) -> None:
        """Register a callback invoked once per changed file on every
        debounced flush, as `(relative_path, exists)`. A listener that
        raises is logged and skipped -- it must never break frontmatter
        indexing for the rest of the batch or for other listeners.
        """
        self._change_listeners.append(listener)

    def search_by_field(
        self,
        field: str,
        value: str,
        match_type: str,
        path_prefix: str | None = None,
    ) -> list[dict]:
        """Search frontmatter index by field.

        Args:
            field: Frontmatter key to match against.
            value: Value to compare (ignored for match_type "exists").
            match_type: One of "exact", "contains", "exists".
            path_prefix: If set, only return files whose relative path starts with this.

        Returns:
            List of {"path": relative_path, "frontmatter": dict}.
        """
        results: list[dict] = []
        with self._lock:
            for rel_path, fm in self._index.items():
                if path_prefix and not rel_path.startswith(path_prefix):
                    continue
                if match_type == "exists":
                    if field in fm:
                        results.append({"path": rel_path, "frontmatter": fm})
                elif match_type == "exact":
                    if field in fm and str(fm[field]) == value:
                        results.append({"path": rel_path, "frontmatter": fm})
                elif match_type == "contains":
                    if field in fm and value.lower() in str(fm[field]).lower():
                        results.append({"path": rel_path, "frontmatter": fm})
        return results

    # -- Internal helpers --

    def _is_excluded(self, path: Path) -> bool:
        """Check whether any path component is in config.EXCLUDED_DIRS."""
        return bool(config.EXCLUDED_DIRS & set(path.relative_to(config.effective_vault_path()).parts))

    def _parse_frontmatter(self, path: Path) -> dict | None:
        """Parse YAML frontmatter from a markdown file. Returns None on failure."""
        try:
            post = frontmatter.load(str(path))
            return dict(post.metadata)
        except Exception:
            logger.warning("Failed to parse frontmatter: %s", path)
            return None

    def _schedule_debounce(self, abs_path: str) -> None:
        """Add a path to the pending set and reset the debounce timer."""
        with self._lock:
            self._pending_paths.add(abs_path)
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                config.FRONTMATTER_INDEX_DEBOUNCE, self._flush_pending
            )
            self._debounce_timer.start()

    def _flush_pending(self) -> None:
        """Process all pending file changes."""
        with self._lock:
            paths = self._pending_paths.copy()
            self._pending_paths.clear()
            self._debounce_timer = None

        for abs_path_str in paths:
            abs_path = Path(abs_path_str)
            rel = str(abs_path.relative_to(config.effective_vault_path()))
            exists = abs_path.exists()
            if exists:
                fm = self._parse_frontmatter(abs_path)
                with self._lock:
                    if fm is not None:
                        self._index[rel] = fm
                    else:
                        self._index.pop(rel, None)
            else:
                with self._lock:
                    self._index.pop(rel, None)

            for listener in self._change_listeners:
                try:
                    listener(rel, exists)
                except Exception:
                    logger.exception("retrieval change listener failed for %s", rel)


class _VaultEventHandler(FileSystemEventHandler):
    """Watchdog handler that feeds .md changes into the frontmatter index."""

    def __init__(self, index: FrontmatterIndex) -> None:
        super().__init__()
        self._index = index

    def _handle(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != ".md":
            return
        if self._index._is_excluded(path):
            return
        self._index._schedule_debounce(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle(event)
