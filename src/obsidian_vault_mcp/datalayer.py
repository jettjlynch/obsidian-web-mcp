"""Optional data-layer P3 hook: type x usage-decay re-rank, pinned core, recall log.

The ranking library lives outside this repo, in Jett's data-layer build
(DATALAYER_DIR, default ~/work/Scripts/data-layer: rerank.py + recall.py,
stdlib only). Everything here is gated by that build's `vault_mcp` flag
(DATALAYER_DIR/flags.json; env DATALAYER_RERANK_VAULT_MCP overrides). The
flag is read on every call, so flipping it needs no restart once this code is
loaded. Flag off, DATALAYER_DIR missing, or any error inside the library:
every function returns its input unchanged / None / no-op, i.e. the server
behaves exactly as before this module existed.

Confidence: vault_search_semantic's `no_good_answer` is computed from the raw
distances inside retrieval.query before anything here runs, and each result
keeps its raw `distance`. The re-rank only reorders.

Rollback (no restart needed): python3 ~/work/Scripts/data-layer/rerank.py flag vault_mcp off
"""

import importlib
import logging
import os
import sys
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

DATALAYER_DIR = Path(os.environ.get("DATALAYER_DIR", os.path.expanduser("~/work/Scripts/data-layer")))
FLAG = "vault_mcp"
DEPTH_FACTOR = 3  # candidates fetched per requested result when re-ranking


def _lib(name: str = "rerank"):
    if not DATALAYER_DIR.is_dir():
        return None
    if str(DATALAYER_DIR) not in sys.path:
        sys.path.insert(0, str(DATALAYER_DIR))
    return importlib.import_module(name)


def enabled() -> bool:
    try:
        rr = _lib()
        return bool(rr and rr.flag_enabled(FLAG))
    except Exception as e:
        logger.warning(f"data-layer unavailable: {e}")
        return False


def rerank_semantic(results: list) -> list:
    """Reorder SemanticSearchResult-like objects (file_path, distance). Unchanged when off."""
    if not results or not enabled():
        return results
    try:
        rr = _lib()
        rows = rr.rerank(
            [{"path": r.file_path, "score": max(0.0, 1.0 - float(r.distance)), "_obj": r} for r in results],
            vault=config.VAULT_PATH,
        )
        return [row["_obj"] for row in rows]
    except Exception as e:
        logger.warning(f"data-layer semantic rerank failed, raw order kept: {e}")
        return results


def rerank_matches(matches: list) -> list:
    """Reorder ripgrep line matches by their file's type x decay weight (rg order is filesystem
    order, not relevance). Stable within a file. Unchanged when off."""
    if not matches or not enabled():
        return matches
    try:
        rr = _lib()
        root = config.effective_vault_path().resolve()
        base = config.VAULT_PATH.resolve()
        prefix = root.relative_to(base).as_posix() if root != base else ""
        rows = rr.rerank(
            [{"path": f"{prefix}/{m['path']}" if prefix else m["path"], "score": 1.0, "_obj": m} for m in matches],
            vault=config.VAULT_PATH,
        )
        return [row["_obj"] for row in rows]
    except Exception as e:
        logger.warning(f"data-layer match rerank failed, raw order kept: {e}")
        return matches


def pinned_core() -> list | None:
    """[{"path", "title", "description", "memory_type"}] for notes with pinned: true, or None when off.
    Paths are vault-relative (scope-rerooted, and out-of-scope notes dropped, when VAULT_SCOPE_ROOT is set)."""
    if not enabled():
        return None
    try:
        rr = _lib()
        items = rr.pinned_core(vault=config.VAULT_PATH)
        scope = (config.VAULT_SCOPE_ROOT or "").strip("/")
        if not scope:
            return items
        out = []
        for it in items:
            if it["path"].startswith(scope + "/"):
                out.append({**it, "path": it["path"][len(scope) + 1:]})
        return out
    except Exception as e:
        logger.warning(f"data-layer pinned core failed: {e}")
        return None


def log_recall(paths: list, source: str = "vault-mcp") -> None:
    if not paths or not enabled():
        return
    try:
        _lib("recall").append([p for p in paths if isinstance(p, str)], source=source)
    except Exception:
        pass
