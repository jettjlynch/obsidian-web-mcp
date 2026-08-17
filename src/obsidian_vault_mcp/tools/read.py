"""Read tools for the Obsidian vault MCP server."""

import json
import logging

import frontmatter

from .. import config
from ..markdown import find_heading_section
from ..vault import resolve_vault_path, read_file

logger = logging.getLogger(__name__)


def vault_read_section(path: str, heading: str) -> str:
    """Return only the body under `heading` (up to the next same/higher heading).

    Saves context on large files where reading the whole file to see one
    section is wasteful.
    """
    try:
        resolve_vault_path(path)
        content, _ = read_file(path)

        lines = content.split("\n")
        section = find_heading_section(lines, heading)
        if section is None:
            return json.dumps({"error": f"Heading not found: {heading!r}", "path": path})

        h_idx, body_start, body_end = section
        body = "\n".join(lines[body_start:body_end])

        return json.dumps({
            "path": path,
            "heading": lines[h_idx],
            "content": body,
            "line_range": [body_start + 1, body_end],
        })
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}", "path": path})
    except Exception as e:
        logger.error(f"vault_read_section error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_read(path: str) -> str:
    """Read a file from the vault, returning content, metadata, and parsed frontmatter."""
    try:
        resolved = resolve_vault_path(path)
        content, metadata = read_file(path)

        fm_data = None
        try:
            post = frontmatter.loads(content)
            if post.metadata:
                fm_data = post.metadata
        except Exception:
            pass

        return json.dumps({
            "path": path,
            "content": content,
            "metadata": metadata,
            "frontmatter": fm_data,
        })
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}", "path": path})
    except Exception as e:
        logger.error(f"vault_read error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files from the vault in one call."""
    # Defense-in-depth: the pydantic model already caps this list length,
    # but clamp here too rather than relying solely on the model layer.
    truncated = len(paths) > config.MAX_BATCH_SIZE
    paths = paths[: config.MAX_BATCH_SIZE]

    results = []
    found = 0
    missing = 0

    for path in paths:
        try:
            content, metadata = read_file(path)

            fm_data = None
            try:
                post = frontmatter.loads(content)
                if post.metadata:
                    fm_data = post.metadata
            except Exception:
                pass

            entry = {
                "path": path,
                "metadata": metadata,
                "frontmatter": fm_data,
            }
            if include_content:
                entry["content"] = content

            results.append(entry)
            found += 1
        except (ValueError, FileNotFoundError) as e:
            results.append({"path": path, "error": str(e)})
            missing += 1
        except Exception as e:
            results.append({"path": path, "error": str(e)})
            missing += 1

    return json.dumps({"files": results, "found": found, "missing": missing, "truncated": truncated})
