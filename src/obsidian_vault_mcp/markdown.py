"""Markdown structure + diff helpers shared by the edit/graph tools.

All section/heading logic operates on ``text.split("\\n")`` (NOT splitlines)
so that ``"\\n".join(lines) == text`` round-trips exactly -- preserving the
file's trailing newline and blank lines. Surgical edits must never silently
reflow a file, so exactness here matters.
"""

import difflib
import re

# ATX heading: 1-6 leading '#', then whitespace or end-of-line. A bare "#tag"
# (no space after #) is therefore NOT a heading, which keeps inline tags safe.
_HEADING_RE = re.compile(r"^(#{1,6})(?:\s|$)")

# [[wikilink]] with optional |alias and optional #subheading or ^block ref.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")

# Inline #tag: not preceded by a word char or another '#' (so "##" headings and
# "a#b" are excluded). Tag must start alphanumeric/underscore; '/' allows nested.
_INLINE_TAG_RE = re.compile(r"(?:^|[^\w#])#([A-Za-z0-9_][A-Za-z0-9_/-]*)")


def unified_diff(path: str, old: str, new: str) -> str:
    """Return a unified diff of old->new, labelled a/<path> b/<path>.

    Empty string means no change.
    """
    if old == new:
        return ""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def heading_level(line: str) -> int:
    """Return the ATX heading level (1-6) of a line, or 0 if it isn't a heading."""
    m = _HEADING_RE.match(line.strip())
    return len(m.group(1)) if m else 0


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split leading YAML frontmatter from the body.

    Returns (frontmatter_block, body). ``frontmatter_block`` includes the
    delimiter lines and a trailing newline, or "" when there is no frontmatter.
    Reconstruction is exact: ``frontmatter_block + body == text`` whenever a
    frontmatter block is present.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            fm_block = "\n".join(lines[: i + 1]) + "\n"
            body = "\n".join(lines[i + 1 :])
            return fm_block, body
    # No closing delimiter -> not valid frontmatter; treat the whole thing as body.
    return "", text


def find_heading_section(lines: list[str], heading: str):
    """Locate a heading and the body beneath it.

    The section body runs from the line after the heading up to (but excluding)
    the next heading of the same or higher level (fewer/equal '#'), or EOF.

    Args:
        lines: file split on "\\n".
        heading: the heading line to match, e.g. "## Status" (compared stripped).

    Returns:
        (heading_index, body_start, body_end) where body = lines[body_start:body_end],
        or None if the heading is not found.
    """
    target = heading.strip()
    provided_level = heading_level(heading)
    for i, line in enumerate(lines):
        if line.strip() == target:
            level = heading_level(line) or provided_level
            j = i + 1
            while j < len(lines):
                lvl = heading_level(lines[j])
                if lvl != 0 and lvl <= level:
                    break
                j += 1
            return i, i + 1, j
    return None


def parse_wikilinks(text: str) -> list[dict]:
    """Extract [[wikilinks]] in document order.

    Each entry: {raw, target, alias, subpath}. ``target`` is the note name with
    any |alias and #subheading/^block stripped. Duplicates are kept (a link that
    appears twice appears twice) so callers can count occurrences.
    """
    links: list[dict] = []
    for m in _WIKILINK_RE.finditer(text):
        inner = m.group(1).strip()
        alias = None
        if "|" in inner:
            inner, alias = inner.split("|", 1)
            inner, alias = inner.strip(), alias.strip()
        subpath = None
        # Split on the first # or ^ that introduces a heading/block reference.
        for sep in ("#", "^"):
            if sep in inner:
                inner, subpath = inner.split(sep, 1)
                inner, subpath = inner.strip(), subpath.strip()
                break
        links.append({
            "raw": m.group(0),
            "target": inner,
            "alias": alias,
            "subpath": subpath,
        })
    return links


def link_target_key(target: str) -> str:
    """Normalise a wikilink target for comparison: drop a trailing .md, lowercase,
    and keep only the final path segment (Obsidian resolves links by note name).
    """
    t = target.strip()
    if t.lower().endswith(".md"):
        t = t[:-3]
    t = t.replace("\\", "/").rstrip("/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    return t.lower()


def extract_tags(metadata: dict | None, body: str) -> list[str]:
    """Collect tags from frontmatter and inline #tags. Returns sorted unique list.

    Frontmatter ``tags`` may be a list or a comma/space separated string.
    """
    found: set[str] = set()

    if metadata:
        raw = metadata.get("tags")
        if isinstance(raw, str):
            for part in re.split(r"[,\s]+", raw):
                part = part.strip().lstrip("#")
                if part:
                    found.add(part)
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                if item is None:
                    continue
                part = str(item).strip().lstrip("#")
                if part:
                    found.add(part)

    for m in _INLINE_TAG_RE.finditer(body):
        found.add(m.group(1))

    return sorted(found)


def json_safe(value):
    """Return frontmatter data with YAML-only scalars turned into JSON-native ones.

    PyYAML (python-frontmatter) parses unquoted ``2026-10-04`` as ``datetime.date``
    and ``2026-10-04 10:30`` as ``datetime.datetime``; ``json.dumps`` cannot encode
    either, which broke vault_search / vault_read / vault_batch_read for any note
    with such a date (fixed 2026-10-04). Dates become ISO strings (what Obsidian
    shows); dicts/lists/tuples recurse; every other value is returned unchanged.
    """
    import datetime as _dt

    if isinstance(value, (_dt.date, _dt.time)):  # datetime is a date subclass
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k) if isinstance(k, (_dt.date, _dt.time)) else k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value
