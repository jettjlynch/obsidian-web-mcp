"""Heading-aware recursive Markdown chunker.

Per RETRIEVAL-DESIGN.md §1: chunk by heading structure first, token budget
second -- never fixed-size windows alone. Carry heading ancestry into every
chunk (the breadcrumb) instead of relying on text overlap between chunks.
Compute real per-chunk line anchors, not section-start line numbers.

This module has exactly one job: turn (file_path, raw_markdown_text) into a
list of `Chunk`s ready to embed. It knows nothing about storage, embedding,
or indexing.
"""

import hashlib
import re
from dataclasses import dataclass, field

from ..markdown import heading_level, split_frontmatter

try:
    import frontmatter as _frontmatter_lib
except ImportError:  # pragma: no cover -- python-frontmatter is a hard dependency elsewhere
    _frontmatter_lib = None

# Control characters (excluding \n, \t) that occasionally end up in vault
# files from copy-paste out of other apps. Strip these before a chunk is
# ever stored or sent to an embedding API. \x00 (null) is included even
# though it would already raise ValueError in vault.resolve_vault_path for
# a *path* -- this is file *content*, a different surface.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Cap on any single whitespace-delimited "word" let into a chunk. A long URL,
# base64 blob, or stack trace line can otherwise become one giant unsplittable
# token that defeats the text splitter (§1.5). Dropped outright, not truncated,
# so we never emit a mangled fragment of it.
_MAX_WORD_LEN = 200

# Roughly 4 characters per token -- a coarse, dependency-free approximation.
# Good enough for a chunk-sizing *budget*; it does not need to match any
# specific tokenizer exactly, only to keep chunks in the right ballpark
# (spec's target: ~200-300 tokens per chunk).
_CHARS_PER_TOKEN = 4

DEFAULT_MAX_TOKENS = 250

# Trim length for a heading re-prepended onto a non-first sub-chunk born from
# splitting one oversized section (§1.2 step 5): full ancestry can be long,
# so only the nearest heading's tail is carried, not the whole breadcrumb.
_REPROMPT_HEADING_TAIL = 100


@dataclass(frozen=True)
class Chunk:
    """One embeddable unit, ready for storage.

    `text` is exactly what gets embedded AND what gets shown back to a user/
    LLM later -- re-display and the embedding input must never diverge
    (§1.4, §4.1).
    """

    text: str
    parent_id: str
    heading_path: tuple[str, ...]
    line_start: int
    content_hash: str
    frontmatter: dict = field(default_factory=dict)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _sanitize(text: str) -> str:
    text = _CONTROL_CHARS_RE.sub("", text)
    words = text.split(" ")
    kept = [w for w in words if len(w) <= _MAX_WORD_LEN]
    return " ".join(kept)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parent_id(file_path: str, heading_path: tuple[str, ...], section_start_line: int) -> str:
    key = f"{file_path}\x1f{'/'.join(heading_path)}\x1f{section_start_line}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _breadcrumb(heading_path: tuple[str, ...]) -> str:
    return "\n".join(heading_path)


@dataclass
class _Section:
    """One node of the parsed heading tree."""

    heading_text: str | None  # None for the synthetic document root
    level: int  # 0 for the root
    start_line: int  # 0-based index into the ORIGINAL file's lines, of the heading line itself
    body_start: int  # 0-based index where this section's own (non-child) body begins
    body_lines: list[str] | None  # this section's own body lines only, children excluded; None until flushed
    children: list["_Section"]


def _parse_heading_tree(lines: list[str]) -> _Section:
    """Build a heading tree from a document's lines (frontmatter already stripped).

    Each section's `body_lines` holds ONLY its own text -- the text between its
    heading and its first child heading (or the end of its span, if it has no
    children) -- never its children's headings or bodies. A section's own body
    is flushed at the earliest point it's known to be complete: the moment its
    first child heading appears (for a section that turns out to have
    children), or when the section itself closes with no children ever having
    appeared (at the next same/higher-level heading, or EOF).
    """
    root = _Section(heading_text=None, level=0, start_line=0, body_start=0, body_lines=None, children=[])
    stack: list[_Section] = [root]
    n = len(lines)

    for i, line in enumerate(lines):
        lvl = heading_level(line)
        if lvl == 0:
            continue

        # Close every open section this new heading is NOT nested under.
        while len(stack) > 1 and stack[-1].level >= lvl:
            finished = stack.pop()
            if finished.body_lines is None:
                # Never got a child -> its whole span was its own body.
                finished.body_lines = lines[finished.body_start:i]

        parent = stack[-1]
        if not parent.children and parent.body_lines is None:
            # This is about to become parent's first child: parent's own body
            # is exactly what's between its heading and this line.
            parent.body_lines = lines[parent.body_start:i]

        new_section = _Section(
            heading_text=line.strip(),
            level=lvl,
            start_line=i,
            body_start=i + 1,
            body_lines=None,
            children=[],
        )
        parent.children.append(new_section)
        stack.append(new_section)

    while len(stack) > 1:
        finished = stack.pop()
        if finished.body_lines is None:
            finished.body_lines = lines[finished.body_start:n]
    if root.body_lines is None:
        root.body_lines = lines[root.body_start:n]

    return root


def _find_line_offset(haystack_lines: list[str], needle_text: str, search_from: int) -> int:
    """Find the 0-based line index within haystack_lines where needle_text's
    first non-empty line actually starts, searching from `search_from` onward
    so repeated phrases don't collide with an earlier match (§1.4).

    Falls back to `search_from` if the text can't be located (e.g. it was
    reflowed/joined during splitting).
    """
    first_line = next((ln for ln in needle_text.split("\n") if ln.strip()), "")
    if not first_line:
        return search_from
    needle = first_line.strip()[:80]
    for idx in range(search_from, len(haystack_lines)):
        if needle and needle in haystack_lines[idx]:
            return idx
    return search_from


def _split_text_by_budget(text: str, max_tokens: int) -> list[str]:
    """Fallback splitter for a leaf section body too big to be one chunk.

    Tries, in order: blank-line paragraphs, single newlines, sentence-ending
    punctuation, whitespace -- only reaching a hard character cut as an
    absolute last resort (§1.2 step 3).
    """
    if _estimate_tokens(text) <= max_tokens:
        return [text] if text.strip() else []

    for pattern in (r"\n\s*\n", r"\n", r"(?<=[.!?])\s+", r"\s+"):
        pieces = [p for p in re.split(pattern, text) if p.strip()]
        if len(pieces) > 1:
            return _pack_pieces(pieces, max_tokens)

    # Absolute last resort: hard character cut.
    budget_chars = max_tokens * _CHARS_PER_TOKEN
    return [text[i:i + budget_chars] for i in range(0, len(text), budget_chars)]


def _pack_pieces(pieces: list[str], max_tokens: int) -> list[str]:
    """Greedily pack small pieces (paragraphs/sentences/words) into chunks
    that each fit the budget, recursing into any single piece still too big
    on its own (e.g. one giant paragraph -> fall through to sentences).
    """
    out: list[str] = []
    current: list[str] = []
    current_tokens = 0

    # Join with newline if pieces look like paragraphs (carry internal
    # newlines); join with a space if they look like sentence/word fragments.
    joiner = "\n" if any("\n" in p for p in pieces) else " "

    for piece in pieces:
        piece_tokens = _estimate_tokens(piece)
        if piece_tokens > max_tokens:
            # This single piece is still too big -- recurse with a smaller
            # separator strategy by re-running the budget splitter on it.
            if current:
                out.append(joiner.join(current))
                current, current_tokens = [], 0
            out.extend(_split_text_by_budget(piece, max_tokens))
            continue
        if current and current_tokens + piece_tokens > max_tokens:
            out.append(joiner.join(current))
            current, current_tokens = [], 0
        current.append(piece)
        current_tokens += piece_tokens

    if current:
        out.append(joiner.join(current))

    return out


def _chunk_section(
    section: _Section,
    file_path: str,
    ancestry: tuple[str, ...],
    max_tokens: int,
    original_lines: list[str],
    chunks_out: list[Chunk],
) -> None:
    own_ancestry = ancestry + ((section.heading_text,) if section.heading_text else ())
    own_text = "\n".join(section.body_lines or []).strip()
    breadcrumb = _breadcrumb(tuple(a for a in own_ancestry if a))

    # A section with no children is a leaf: its own text IS its whole subtree,
    # so if that fits the budget it becomes exactly one chunk.
    subtree_fits = not section.children and _estimate_tokens(own_text) <= max_tokens

    if subtree_fits:
        if not own_text:
            return
        sanitized = _sanitize(own_text)
        if not sanitized.strip():
            return
        embed_text = f"{breadcrumb}\n\n{sanitized}" if breadcrumb else sanitized
        anchor_search_start = section.body_start
        offset = _find_line_offset(original_lines, sanitized, anchor_search_start)
        chunks_out.append(
            Chunk(
                text=embed_text,
                parent_id=_parent_id(file_path, own_ancestry, section.start_line),
                heading_path=own_ancestry,
                line_start=offset + 1,
                content_hash=_content_hash(embed_text),
            )
        )
        return

    # Own body text (before any child headings) that doesn't fit, or that
    # exists alongside children -- treat it as its own leaf content first.
    if own_text.strip():
        sanitized_own = _sanitize(own_text)
        if sanitized_own.strip():
            pid = _parent_id(file_path, own_ancestry, section.start_line)
            pieces = _split_text_by_budget(sanitized_own, max_tokens)
            search_cursor = section.body_start
            for n, piece in enumerate(pieces):
                piece = piece.strip()
                if not piece:
                    continue
                if n == 0:
                    prefix = breadcrumb
                else:
                    tail_heading = own_ancestry[-1] if own_ancestry else ""
                    prefix = tail_heading[-_REPROMPT_HEADING_TAIL:]
                embed_text = f"{prefix}\n\n{piece}" if prefix else piece
                offset = _find_line_offset(original_lines, piece, search_cursor)
                search_cursor = offset + 1
                chunks_out.append(
                    Chunk(
                        text=embed_text,
                        parent_id=pid,
                        heading_path=own_ancestry,
                        line_start=offset + 1,
                        content_hash=_content_hash(embed_text),
                    )
                )

    for child in section.children:
        _chunk_section(child, file_path, own_ancestry, max_tokens, original_lines, chunks_out)


def chunk_markdown(
    file_path: str,
    content: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[Chunk]:
    """Chunk a Markdown file's content into embeddable `Chunk`s.

    `file_path` is the vault-relative path -- it becomes the outermost
    ancestor in every chunk's breadcrumb (§1.2 step 4).
    """
    fm_block, body = split_frontmatter(content)
    frontmatter_fields: dict = {}
    if fm_block and _frontmatter_lib is not None:
        try:
            post = _frontmatter_lib.loads(content)
            frontmatter_fields = dict(post.metadata)
        except Exception:
            frontmatter_fields = {}

    lines = body.split("\n")
    root = _parse_heading_tree(lines)

    chunks: list[Chunk] = []
    _chunk_section(root, file_path, (file_path,), max_tokens, lines, chunks)

    if frontmatter_fields:
        chunks = [
            Chunk(
                text=c.text,
                parent_id=c.parent_id,
                heading_path=c.heading_path,
                line_start=c.line_start,
                content_hash=c.content_hash,
                frontmatter=frontmatter_fields,
            )
            for c in chunks
        ]

    return chunks
