"""Write tools for the Obsidian vault MCP server.

Every write tool supports ``dry_run=True``: it computes the would-be content,
returns a unified diff, and writes nothing. This is a hard safety requirement
given the vault's history of accidental full-file overwrites.
"""

import json
import logging

import frontmatter

from ..markdown import unified_diff
from ..vault import resolve_vault_path, read_file, write_file_atomic

logger = logging.getLogger(__name__)


def vault_write(
    path: str,
    content: str,
    create_dirs: bool = True,
    merge_frontmatter: bool = False,
    dry_run: bool = False,
) -> str:
    """Write a file to the vault, optionally merging frontmatter with existing content."""
    try:
        resolve_vault_path(path)

        try:
            old_content, _ = read_file(path)
            existed = True
        except FileNotFoundError:
            old_content = ""
            existed = False

        final_content = content
        if merge_frontmatter and existed:
            try:
                existing_post = frontmatter.loads(old_content)
                new_post = frontmatter.loads(content)

                merged_meta = dict(existing_post.metadata)
                merged_meta.update(new_post.metadata)

                new_post.metadata = merged_meta
                final_content = frontmatter.dumps(new_post)
            except Exception as e:
                logger.warning(f"Frontmatter merge failed for {path}, writing as-is: {e}")

        if dry_run:
            return json.dumps({
                "path": path,
                "dry_run": True,
                "would_change": final_content != old_content,
                "would_create": not existed,
                "diff": unified_diff(path, old_content, final_content),
            })

        is_new, size = write_file_atomic(path, final_content, create_dirs=create_dirs)

        return json.dumps({"path": path, "created": is_new, "size": size})
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_write error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_append(
    path: str,
    content: str,
    create_dirs: bool = True,
    ensure_newline: bool = True,
    dry_run: bool = False,
) -> str:
    """Append content to the end of a file, creating it if it doesn't exist."""
    try:
        resolve_vault_path(path)

        try:
            existing_content, _ = read_file(path)
            is_new = False
        except FileNotFoundError:
            existing_content = ""
            is_new = True

        base = existing_content
        if base and ensure_newline and not base.endswith("\n"):
            base += "\n"

        new_content = base + content

        if dry_run:
            return json.dumps({
                "path": path,
                "dry_run": True,
                "would_change": new_content != existing_content,
                "would_create": is_new,
                "diff": unified_diff(path, existing_content, new_content),
            })

        _, size = write_file_atomic(path, new_content, create_dirs=create_dirs)

        return json.dumps({
            "path": path,
            "created": is_new,
            "size": size,
            "appended_bytes": len(content.encode("utf-8")),
        })
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_append error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_batch_frontmatter_update(updates: list[dict], dry_run: bool = False) -> str:
    """Update frontmatter fields on multiple files without changing body content."""
    results = []

    for update in updates:
        file_path = update.get("path", "")
        fields = update.get("fields", {})

        try:
            content, _ = read_file(file_path)
            post = frontmatter.loads(content)

            for key, value in fields.items():
                post.metadata[key] = value

            new_content = frontmatter.dumps(post)

            if dry_run:
                results.append({
                    "path": file_path,
                    "dry_run": True,
                    "would_change": new_content != content,
                    "diff": unified_diff(file_path, content, new_content),
                })
            else:
                write_file_atomic(file_path, new_content, create_dirs=False)
                results.append({"path": file_path, "updated": True})
        except FileNotFoundError:
            results.append({"path": file_path, "updated": False, "error": "File not found"})
        except ValueError as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})
        except Exception as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})

    return json.dumps({"results": results, "dry_run": dry_run})
