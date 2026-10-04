"""Pydantic input models for obsidian-vault-mcp tool endpoints."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# File content is written byte for byte (port of upstream a4aa3b2, #99). The
# models below strip surrounding whitespace from their string fields, which is
# right for paths and wrong for a note: stripped, every vault_write lost the
# note's trailing newline and an indented first line lost its indent, and
# vault_append lost leading blank lines / trailing newline of the appended text.
VerbatimText = Annotated[str, StringConstraints(strip_whitespace=False)]

from .config import (
    CONTEXT_LINES,
    DEFAULT_SEARCH_RESULTS,
    MAX_BATCH_SIZE,
    MAX_CONTENT_SIZE,
    MAX_LIST_DEPTH,
    MAX_SEARCH_RESULTS,
)


class VaultReadInput(BaseModel):
    """Read a single file from the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root (e.g. 'projects/acme/notes.md')",
        min_length=1,
        max_length=500,
    )


class VaultWriteInput(BaseModel):
    """Write or overwrite a file in the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root",
        min_length=1,
        max_length=500,
    )
    content: VerbatimText = Field(
        ...,
        description="Full file content to write",
        max_length=MAX_CONTENT_SIZE,
    )
    create_dirs: bool = Field(
        default=True,
        description="Create parent directories if they don't exist",
    )
    merge_frontmatter: bool = Field(
        default=False,
        description="If true, merge YAML frontmatter with existing file's frontmatter instead of replacing",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, return a unified diff of what WOULD change and write nothing",
    )


class VaultAppendInput(BaseModel):
    """Append content to the end of a file in the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root",
        min_length=1,
        max_length=500,
    )
    content: VerbatimText = Field(
        ...,
        description="Content to append to the end of the file",
        max_length=MAX_CONTENT_SIZE,
    )
    create_dirs: bool = Field(
        default=True,
        description="Create the file (and parent directories) if it doesn't exist",
    )
    ensure_newline: bool = Field(
        default=True,
        description="If true, ensure a newline separates existing content from the appended content",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, return a unified diff of what WOULD change and write nothing",
    )


class VaultListInput(BaseModel):
    """List files and directories under a vault path."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        default="",
        description="Relative directory path from vault root; empty string for root",
        max_length=500,
    )
    depth: int = Field(
        default=1,
        ge=1,
        le=MAX_LIST_DEPTH,
        description="How many levels deep to recurse",
    )
    include_files: bool = Field(
        default=True,
        description="Include files in the listing",
    )
    include_dirs: bool = Field(
        default=True,
        description="Include directories in the listing",
    )
    pattern: str | None = Field(
        default=None,
        description="Optional glob pattern to filter results (e.g. '*.md')",
        max_length=100,
    )


class VaultMoveInput(BaseModel):
    """Move or rename a file/directory within the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    source: str = Field(
        ...,
        description="Current relative path of the file or directory",
        min_length=1,
        max_length=500,
    )
    destination: str = Field(
        ...,
        description="New relative path for the file or directory",
        min_length=1,
        max_length=500,
    )
    create_dirs: bool = Field(
        default=True,
        description="Create destination parent directories if they don't exist",
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to execute the move -- safety gate to prevent accidental relocations. Unlike vault_delete, a move has no .trash/ recovery.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, report whether the move would succeed without touching anything. Does not require confirm.",
    )


class VaultDeleteInput(BaseModel):
    """Delete a file from the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path of the file to delete",
        min_length=1,
        max_length=500,
    )
    confirm: bool = Field(
        ...,
        description="Must be true to execute deletion -- safety gate to prevent accidental deletes",
    )


class VaultSearchInput(BaseModel):
    """Full-text search across vault files."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Search string to find in file contents",
        min_length=1,
        max_length=200,
    )
    path_prefix: str | None = Field(
        default=None,
        description="Limit search to files under this directory prefix",
        max_length=500,
    )
    file_pattern: str = Field(
        default="*.md",
        description="Glob pattern for files to search (e.g. '*.md', '*.canvas')",
        max_length=50,
    )
    max_results: int = Field(
        default=DEFAULT_SEARCH_RESULTS,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description="Maximum number of matching files to return",
    )
    context_lines: int = Field(
        default=CONTEXT_LINES,
        ge=0,
        le=10,
        description="Number of lines of context to show around each match",
    )


class VaultSearchFrontmatterInput(BaseModel):
    """Search vault files by YAML frontmatter field values."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    field: str = Field(
        ...,
        description="Frontmatter field name to search (e.g. 'status', 'tags', 'publish-date')",
        min_length=1,
        max_length=100,
    )
    value: str = Field(
        default="",
        description="Value to match against; ignored when match_type is 'exists'",
        max_length=200,
    )
    match_type: Literal["exact", "contains", "exists"] = Field(
        default="exact",
        description="How to match: 'exact' for equality, 'contains' for substring, 'exists' to check field presence",
    )
    path_prefix: str | None = Field(
        default=None,
        description="Limit search to files under this directory prefix",
        max_length=500,
    )
    max_results: int = Field(
        default=DEFAULT_SEARCH_RESULTS,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description="Maximum number of matching files to return",
    )


class VaultSearchSemanticInput(BaseModel):
    """Meaning-based (embeddings) search across vault content.

    Complements, does not replace, vault_search (exact text) and
    vault_search_frontmatter (structured fields) -- per RETRIEVAL-DESIGN.md
    §3.2, callers should reach for whichever tool fits, not expect one
    "smart" tool to guess.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description=(
            'Natural-language question or topic. Wrap a substring in "double quotes" to '
            'require it verbatim, or prefix with -"phrase" to exclude it.'
        ),
        min_length=1,
        max_length=500,
    )
    path_prefix: str | None = Field(
        default=None,
        description="Limit search to files under this directory prefix",
        max_length=500,
    )
    max_results: int = Field(
        default=10,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description="Maximum number of distinct chunks to return, diversified across source notes",
    )
    frontmatter_field: str | None = Field(
        default=None,
        description="Optionally narrow to chunks whose frontmatter has this field",
        max_length=100,
    )
    frontmatter_value: str | None = Field(
        default=None,
        description="Value to match frontmatter_field against (exact match)",
        max_length=200,
    )


class VaultBatchReadInput(BaseModel):
    """Read multiple vault files in a single request."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    paths: list[str] = Field(
        ...,
        description="List of relative paths to read",
        min_length=1,
        max_length=MAX_BATCH_SIZE,
    )
    include_content: bool = Field(
        default=True,
        description="If false, return metadata only (frontmatter, size) without file body",
    )


class VaultBatchFrontmatterUpdateInput(BaseModel):
    """Update YAML frontmatter on multiple files in one request."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    updates: list[dict] = Field(
        ...,
        description="List of updates, each a dict with 'path' (str) and 'fields' (dict of key-value pairs to set)",
        min_length=1,
        max_length=MAX_BATCH_SIZE,
    )

    dry_run: bool = Field(
        default=False,
        description="If true, return a unified diff per file and write nothing",
    )

    @field_validator("updates")
    @classmethod
    def validate_updates(cls, v: list[dict]) -> list[dict]:
        for i, item in enumerate(v):
            if "path" not in item or not isinstance(item["path"], str):
                raise ValueError(f"updates[{i}] must contain a 'path' key with a string value")
            if "fields" not in item or not isinstance(item["fields"], dict):
                raise ValueError(f"updates[{i}] must contain a 'fields' key with a dict value")
        return v


# ----------------------------------------------------------------------------
# Surgical edit, section, and graph tools (added 2026-06-17)
#
# These models deliberately OMIT str_strip_whitespace: surgical edits must keep
# exact leading/trailing whitespace and newlines in find/replace targets and in
# inserted content, or the edit corrupts the file. Read-only models that only
# carry a path/heading may strip.
# ----------------------------------------------------------------------------

_DRY_RUN = Field(
    default=False,
    description="If true, return a unified diff of what WOULD change and write nothing",
)


class VaultFindReplaceInput(BaseModel):
    """Replace an exact string in a file."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    find: str = Field(..., description="Exact string to find (whitespace-sensitive)", min_length=1, max_length=MAX_CONTENT_SIZE)
    replace: str = Field(..., description="Replacement string", max_length=MAX_CONTENT_SIZE)
    occurrence: str | int = Field(
        default="all",
        description="'all', 'first', or a 1-based integer N to replace only the Nth match",
    )
    dry_run: bool = _DRY_RUN


class VaultInsertAtInput(BaseModel):
    """Insert content relative to a heading or exact-line anchor."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    content: str = Field(..., description="Content to insert", max_length=MAX_CONTENT_SIZE)
    after_heading: str | None = Field(
        default=None,
        description="Heading anchor, e.g. '## Living Brain Protocol'. Provide this OR after_line.",
        max_length=500,
    )
    after_line: str | None = Field(
        default=None,
        description="Exact line to anchor to. Provide this OR after_heading.",
        max_length=2000,
    )
    position: Literal["after", "before"] = Field(
        default="after",
        description="Insert after or before the anchor (use before + first line to insert at the very top)",
    )
    dry_run: bool = _DRY_RUN


class VaultReplaceSectionInput(BaseModel):
    """Replace the body under a heading, preserving the heading line."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    heading: str = Field(..., description="Heading whose body to replace, e.g. '## Status'", min_length=1, max_length=500)
    new_content: str = Field(..., description="New body for the section (heading line is kept)", max_length=MAX_CONTENT_SIZE)
    dry_run: bool = _DRY_RUN


class VaultAppendUnderHeadingInput(BaseModel):
    """Append content to the end of a section under a heading."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    heading: str = Field(..., description="Heading whose section to append to, e.g. '## Log'", min_length=1, max_length=500)
    content: str = Field(..., description="Content to append at the end of the section", max_length=MAX_CONTENT_SIZE)
    dry_run: bool = _DRY_RUN


class VaultPrependInput(BaseModel):
    """Insert content at the top of a file, after any YAML frontmatter."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    content: str = Field(..., description="Content to place at the top (after frontmatter)", max_length=MAX_CONTENT_SIZE)
    dry_run: bool = _DRY_RUN
    create_dirs: bool = Field(default=True, description="Create the file/dirs if missing")


class VaultReadSectionInput(BaseModel):
    """Read only the body under a heading."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    heading: str = Field(..., description="Heading whose body to return, e.g. '## Status'", min_length=1, max_length=500)


class VaultLinksInput(BaseModel):
    """List outgoing [[wikilinks]] in a file."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)


class VaultBacklinksInput(BaseModel):
    """Find all notes linking to a target."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    target: str = Field(..., description="Note name to find backlinks for (with or without .md)", min_length=1, max_length=500)


class VaultTagsInput(BaseModel):
    """List notes for a tag, or all tags with counts."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tag: str | None = Field(
        default=None,
        description="Tag to list notes for (with or without leading #). Omit for all tags with counts.",
        max_length=200,
    )


class VaultDailyInput(BaseModel):
    """Resolve, create, append to, or read today's daily note."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(
        default=None,
        description="Content to append to today's daily note. Omit to just read it.",
        max_length=MAX_CONTENT_SIZE,
    )
    heading: str | None = Field(
        default=None,
        description="If appending, the heading to append under (created if absent). Else appends at end.",
        max_length=500,
    )
    dry_run: bool = _DRY_RUN
