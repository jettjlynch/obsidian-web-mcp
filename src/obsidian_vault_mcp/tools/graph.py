"""Graph + convenience tools: wikilinks, backlinks, tags, and the daily note.

These give the MCP the link/tag graph view that Obsidian has natively but the
server previously lacked.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import frontmatter

from .. import config
from ..markdown import (
    extract_tags,
    find_heading_section,
    link_target_key,
    parse_wikilinks,
    unified_diff,
)
from ..vault import read_file, resolve_vault_path, write_file_atomic

logger = logging.getLogger(__name__)


def _iter_md_paths():
    """Yield absolute Paths of every vault .md file, skipping excluded dirs."""
    root = config.effective_vault_path()
    for p in root.rglob("*.md"):
        rel_parts = set(p.relative_to(root).parts)
        if config.EXCLUDED_DIRS & rel_parts:
            continue
        yield p


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, OSError):
        return None


def vault_links(path: str) -> str:
    """Return all [[wikilinks]] in a file (outgoing links)."""
    try:
        resolve_vault_path(path)
        content, _ = read_file(path)

        links = parse_wikilinks(content)
        unique: list[str] = []
        seen: set[str] = set()
        for link in links:
            t = link["target"]
            if t and t.lower() not in seen:
                seen.add(t.lower())
                unique.append(t)

        return json.dumps({
            "path": path,
            "links": links,
            "unique_targets": unique,
            "count": len(links),
        })
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}", "path": path})
    except Exception as e:
        logger.error(f"vault_links error for {path}: {e}")
        return json.dumps({"error": str(e), "path": path})


def vault_backlinks(target: str) -> str:
    """Return all notes that link to `target` (incoming links)."""
    try:
        key = link_target_key(target)
        if not key:
            return json.dumps({"error": "`target` must be a non-empty note name", "target": target})

        results = []
        for p in _iter_md_paths():
            text = _read_text(p)
            if not text or "[[" not in text:
                continue
            matched = [l["raw"] for l in parse_wikilinks(text) if link_target_key(l["target"]) == key]
            if matched:
                rel = str(p.relative_to(config.effective_vault_path()))
                results.append({"path": rel, "links": matched, "count": len(matched)})

        results.sort(key=lambda r: r["path"])
        return json.dumps({"target": target, "backlinks": results, "total": len(results)})
    except Exception as e:
        logger.error(f"vault_backlinks error for {target}: {e}")
        return json.dumps({"error": str(e), "target": target})


def vault_tags(tag: str | None = None) -> str:
    """List notes carrying `tag`, or all tags with counts when `tag` is None."""
    try:
        if tag:
            want = tag.strip().lstrip("#").lower()
            notes = []
            for p in _iter_md_paths():
                text = _read_text(p)
                if text is None:
                    continue
                try:
                    post = frontmatter.loads(text)
                    tags = extract_tags(post.metadata, post.content)
                except Exception:
                    logger.warning("Failed to parse frontmatter for tags: %s", p)
                    continue
                if any(t.lower() == want for t in tags):
                    notes.append(str(p.relative_to(config.effective_vault_path())))
            notes.sort()
            return json.dumps({"tag": want, "notes": notes, "total": len(notes)})

        counts: dict[str, int] = {}
        for p in _iter_md_paths():
            text = _read_text(p)
            if text is None:
                continue
            try:
                post = frontmatter.loads(text)
                tags = extract_tags(post.metadata, post.content)
            except Exception:
                logger.warning("Failed to parse frontmatter for tags: %s", p)
                continue
            for t in tags:
                counts[t] = counts.get(t, 0) + 1

        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return json.dumps({
            "tags": [{"tag": k, "count": v} for k, v in ordered],
            "total_unique": len(ordered),
        })
    except Exception as e:
        logger.error(f"vault_tags error: {e}")
        return json.dumps({"error": str(e)})


def _london_today() -> str:
    """Today's date (YYYY-MM-DD) in Europe/London, falling back to local time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")
    except Exception:
        # macOS host runs on London time; local now() is the correct fallback.
        return datetime.now().astimezone().strftime("%Y-%m-%d")


def _daily_header(date_str: str) -> str:
    return (
        "---\n"
        f"title: Daily Log — {date_str}\n"
        "type: daily\n"
        "tags: [daily, living-brain]\n"
        f"permalink: jj-second-brain/daily/{date_str}\n"
        "---\n\n"
        f"# {date_str}\n\n"
        "Entries append throughout the day from all Claude interactions.\n"
    )


def _append_block(base: str, content: str, heading: str | None) -> str:
    """Append `content` to `base`, under `heading` if given (else at the end)."""
    if heading:
        lines = base.split("\n")
        section = find_heading_section(lines, heading)
        if section is not None:
            _, start, end = section
            insert_at = end
            while insert_at > start and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            block = ([""] if insert_at > start else []) + content.split("\n")
            return "\n".join(lines[:insert_at] + block + lines[insert_at:])
        # Heading absent: start a new section at the end of the file.
        tail = base if (not base or base.endswith("\n")) else base + "\n"
        return f"{tail}\n{heading}\n{content}\n"

    sep = "\n" if (base and not base.endswith("\n")) else ""
    return base + sep + content


def vault_daily(content: str | None = None, heading: str | None = None, dry_run: bool = False) -> str:
    """Resolve today's Daily/YYYY-MM-DD.md; create it, append to it, or read it.

    dry_run (added per the global write-safety rule) previews any create/append
    as a unified diff and writes nothing.
    """
    date_str = _london_today()
    path = f"Daily/{date_str}.md"
    try:
        resolve_vault_path(path)

        try:
            existing, _ = read_file(path)
            existed = True
        except FileNotFoundError:
            existing = ""
            existed = False

        # Pure read: file already exists and no content to add.
        if existed and content is None:
            return json.dumps({
                "path": path, "date": date_str, "created": False,
                "appended": False, "content": existing,
            })

        base = existing if existed else _daily_header(date_str)
        new_content = _append_block(base, content, heading) if content else base
        appended = content is not None

        if dry_run:
            return json.dumps({
                "path": path, "date": date_str, "dry_run": True,
                "would_create": not existed, "appended": appended,
                "diff": unified_diff(path, existing, new_content),
            })

        write_file_atomic(path, new_content, create_dirs=True)
        return json.dumps({
            "path": path, "date": date_str, "created": not existed,
            "appended": appended, "content": new_content,
        })
    except ValueError as e:
        return json.dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_daily error: {e}")
        return json.dumps({"error": str(e), "path": path})
