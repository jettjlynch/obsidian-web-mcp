"""Surgical edit tools for the Obsidian vault MCP server.

Every tool here is read-before-write: it loads the current file, applies the
change in memory, and only then writes atomically. Every tool also supports
``dry_run=True``, which returns a unified diff of what WOULD change and writes
nothing -- a hard requirement given this vault's history of accidental
full-file overwrites.
"""

import json
import logging

from ..markdown import find_heading_section, split_frontmatter, unified_diff
from ..vault import read_file, resolve_vault_path, write_file_atomic

logger = logging.getLogger(__name__)


def _edit_result(path: str, old: str, new: str, dry_run: bool, create_dirs: bool = False, **extra) -> str:
    """Apply (or preview) an in-memory edit and return a JSON result string.

    On dry_run: returns the unified diff, writes nothing.
    Otherwise: writes atomically only if the content actually changed.
    """
    changed = new != old
    diff = unified_diff(path, old, new)

    if dry_run:
        return json.dumps({
            "path": path,
            "dry_run": True,
            "would_change": changed,
            "diff": diff,
            **extra,
        })

    size = len(old.encode("utf-8"))
    if changed:
        _, size = write_file_atomic(path, new, create_dirs=create_dirs)

    return json.dumps({
        "path": path,
        "dry_run": False,
        "changed": changed,
        "size": size,
        **extra,
    })


def _normalize_occurrence(occurrence) -> tuple[str, int | None]:
    """Normalise the occurrence selector to (mode, n)."""
    if isinstance(occurrence, bool):
        raise ValueError("occurrence must be 'all', 'first', or a positive integer")
    if isinstance(occurrence, int):
        if occurrence < 1:
            raise ValueError("occurrence integer must be >= 1")
        return "nth", occurrence
    s = str(occurrence).strip().lower()
    if s == "all":
        return "all", None
    if s == "first":
        return "first", None
    if s.isdigit() and int(s) >= 1:
        return "nth", int(s)
    raise ValueError("occurrence must be 'all', 'first', or a positive integer")


def _replace_nth(content: str, find: str, replace: str, n: int) -> str | None:
    """Replace only the n-th (1-based) occurrence of find. None if fewer than n."""
    idx = -1
    for _ in range(n):
        idx = content.find(find, idx + 1)
        if idx == -1:
            return None
    return content[:idx] + replace + content[idx + len(find):]


def vault_find_replace(path: str, find: str, replace: str, occurrence="all", dry_run: bool = False) -> str:
    """Replace exact string `find` with `replace`. Eliminates full-file rewrites."""
    try:
        resolve_vault_path(path)

        if not find:
            return json.dumps({"error": "`find` must be a non-empty string", "path": path})

        try:
            content, _ = read_file(path)
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}", "path": path})

        count = content.count(find)
        if count == 0:
            return json.dumps({
                "error": f"`find` string not present in {path}; nothing replaced",
                "path": path,
                "occurrences_found": 0,
            })

        mode, n = _normalize_occurrence(occurrence)
        if mode == "all":
            new_content = content.replace(find, replace)
            replacements = count
        elif mode == "first":
            new_content = content.replace(find, replace, 1)
            replacements = 1
        else:  # nth
            result = _replace_nth(content, find, replace, n)
            if result is None:
                return json.dumps({
                    "error": f"occurrence {n} requested but only {count} present",
                    "path": path,
                    "occurrences_found": count,
                })
            new_content = result
            replacements = 1

        return _edit_result(
            path, content, new_content, dry_run,
            occurrences_found=count, replacements=replacements,
        )
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_find_replace error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_insert_at(
    path: str,
    content: str,
    after_heading: str | None = None,
    after_line: str | None = None,
    position: str = "after",
    dry_run: bool = False,
) -> str:
    """Insert `content` before/after a heading or exact-line anchor."""
    try:
        resolve_vault_path(path)

        if (after_heading is None) == (after_line is None):
            return json.dumps({
                "error": "Provide exactly one of after_heading or after_line",
                "path": path,
            })
        if position not in ("after", "before"):
            return json.dumps({"error": "position must be 'after' or 'before'", "path": path})

        try:
            existing, _ = read_file(path)
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}", "path": path})

        lines = existing.split("\n")

        if after_heading is not None:
            section = find_heading_section(lines, after_heading)
            if section is None:
                return json.dumps({"error": f"Heading not found: {after_heading!r}", "path": path})
            anchor_idx = section[0]
            anchor_desc = f"heading {after_heading!r}"
        else:
            try:
                anchor_idx = lines.index(after_line)
            except ValueError:
                return json.dumps({"error": f"Line not found (exact match): {after_line!r}", "path": path})
            anchor_desc = f"line {after_line!r}"

        insert_idx = anchor_idx + 1 if position == "after" else anchor_idx
        new_lines = lines[:insert_idx] + content.split("\n") + lines[insert_idx:]
        new_content = "\n".join(new_lines)

        return _edit_result(
            path, existing, new_content, dry_run,
            anchor=anchor_desc, position=position,
            inserted_bytes=len(content.encode("utf-8")),
        )
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_insert_at error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_replace_section(path: str, heading: str, new_content: str, dry_run: bool = False) -> str:
    """Replace the body under `heading` (up to the next same/higher heading)."""
    try:
        resolve_vault_path(path)

        try:
            existing, _ = read_file(path)
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}", "path": path})

        lines = existing.split("\n")
        section = find_heading_section(lines, heading)
        if section is None:
            return json.dumps({"error": f"Heading not found: {heading!r}", "path": path})

        h_idx, body_start, body_end = section
        new_lines = lines[:body_start] + new_content.split("\n") + lines[body_end:]
        new_text = "\n".join(new_lines)

        return _edit_result(
            path, existing, new_text, dry_run,
            heading=heading, replaced_lines=body_end - body_start,
        )
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_replace_section error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_append_under_heading(path: str, heading: str, content: str, dry_run: bool = False) -> str:
    """Append `content` to the END of the section under `heading`."""
    try:
        resolve_vault_path(path)

        try:
            existing, _ = read_file(path)
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}", "path": path})

        lines = existing.split("\n")
        section = find_heading_section(lines, heading)
        if section is None:
            return json.dumps({"error": f"Heading not found: {heading!r}", "path": path})

        h_idx, body_start, body_end = section

        # Insert just before the next same/higher heading (or EOF). Trim trailing
        # blank lines inside the section so the appended block sits flush against
        # the section's real content, then restore one blank separator.
        insert_at = body_end
        while insert_at > body_start and lines[insert_at - 1].strip() == "":
            insert_at -= 1

        addition = content.split("\n")
        # one blank line between existing section content and the addition
        block = ([""] if insert_at > body_start else []) + addition
        new_lines = lines[:insert_at] + block + lines[insert_at:]
        new_text = "\n".join(new_lines)

        return _edit_result(
            path, existing, new_text, dry_run,
            heading=heading, appended_bytes=len(content.encode("utf-8")),
        )
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_append_under_heading error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_prepend(path: str, content: str, dry_run: bool = False, create_dirs: bool = True) -> str:
    """Insert `content` at the top of the file, AFTER any YAML frontmatter."""
    try:
        resolve_vault_path(path)

        try:
            existing, _ = read_file(path)
            is_new = False
        except FileNotFoundError:
            existing = ""
            is_new = True

        fm_block, body = split_frontmatter(existing)

        insertion = content if content.endswith("\n") else content + "\n"
        new_text = fm_block + insertion + body

        return _edit_result(
            path, existing, new_text, dry_run, create_dirs=create_dirs,
            created=is_new, after_frontmatter=bool(fm_block),
        )
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_prepend error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})
