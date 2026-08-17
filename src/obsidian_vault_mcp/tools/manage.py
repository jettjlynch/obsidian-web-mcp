"""Management tools for the Obsidian vault MCP server."""

import json
import logging

from ..vault import list_directory, move_path, delete_path, resolve_vault_path

logger = logging.getLogger(__name__)


def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> str:
    """List directory contents in the vault."""
    try:
        items = list_directory(
            path,
            depth=depth,
            include_files=include_files,
            include_dirs=include_dirs,
            pattern=pattern,
        )
        return json.dumps({"items": items, "total": len(items)})
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except FileNotFoundError:
        return json.dumps({"error": f"Directory not found: {path}"})
    except Exception as e:
        logger.error(f"vault_list error: {e}")
        return json.dumps({"error": str(e)})


def vault_move(
    source: str, destination: str, create_dirs: bool = True, confirm: bool = False, dry_run: bool = False
) -> str:
    """Move a file or directory within the vault.

    Requires confirm=true to actually execute -- a move is irreversible (no
    .trash/ recovery, unlike vault_delete). Set dry_run=true to check whether
    the move would succeed (source exists, destination doesn't) without
    touching anything; dry_run does not require confirm.
    """
    if dry_run:
        try:
            src = resolve_vault_path(source)
            dst = resolve_vault_path(destination)
        except ValueError as e:
            return json.dumps({"error": str(e), "source": source, "destination": destination})
        if not src.exists():
            return json.dumps({
                "error": f"Source does not exist: {source}",
                "source": source,
                "destination": destination,
            })
        if dst.exists():
            return json.dumps({
                "error": f"Destination already exists: {destination}",
                "source": source,
                "destination": destination,
            })
        return json.dumps({
            "dry_run": True,
            "would_move": {"source": source, "destination": destination},
        })

    if not confirm:
        return json.dumps({
            "error": "Set confirm=true to execute the move. This is irreversible -- there is no .trash/ recovery for moves, unlike vault_delete. Set dry_run=true first to preview.",
            "source": source,
            "destination": destination,
        })

    try:
        moved = move_path(source, destination, create_dirs=create_dirs)
        return json.dumps({"source": source, "destination": destination, "moved": moved})
    except ValueError as e:
        return json.dumps({"error": str(e), "source": source, "destination": destination})
    except Exception as e:
        logger.error(f"vault_move error: {e}")
        return json.dumps({"error": str(e), "source": source, "destination": destination})


def vault_delete(path: str, confirm: bool = False) -> str:
    """Delete a file by moving it to .trash/ in the vault."""
    if not confirm:
        return json.dumps({
            "error": "Set confirm=true to execute deletion. Files are moved to .trash/, not hard deleted.",
            "path": path,
        })

    try:
        deleted = delete_path(path)
        return json.dumps({"path": path, "deleted": deleted})
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_delete error: {e}")
        return json.dumps({"error": str(e), "path": path})
