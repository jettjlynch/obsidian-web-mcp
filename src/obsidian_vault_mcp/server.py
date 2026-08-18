"""Obsidian Vault MCP Server.

Exposes read/write access to an Obsidian vault over Streamable HTTP.
Designed to run behind Cloudflare Tunnel for secure remote access.
"""

import atexit
import json
import logging
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import (
    VAULT_AUDIT_LOG_INCLUDE_READS,
    VAULT_MCP_ALLOWED_HOSTS,
    VAULT_MCP_FORWARDED_ALLOW_IPS,
    VAULT_MCP_HEARTBEAT_URL,
    VAULT_MCP_HOST,
    VAULT_MCP_PATH,
    VAULT_MCP_PORT,
    VAULT_MCP_TOKEN,
    VAULT_PATH,
    VAULT_SCOPE_ROOT,
    effective_vault_path,
)
from .frontmatter_index import FrontmatterIndex
from .rate_limit import rate_limited
from .audit import (
    BATCH_OPERATIONS,
    MUTATION_OPERATIONS,
    audit_enabled,
    audit_log_path,
    audit_path_inside_vault,
    audit_path_writable,
    before_target_path,
    build_audit_record,
    infer_target_path,
    should_audit_operation,
    snapshot_path,
    write_audit_record,
)

logger = logging.getLogger(__name__)

# Global frontmatter index instance
frontmatter_index = FrontmatterIndex()


# Liveness pings don't need the response body; read just enough to complete the
# request without pulling a large/hostile body into memory.
_HEARTBEAT_MAX_BYTES = 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects on the heartbeat GET.

    The configured URL is operator-trusted, but a redirect is not -- following one
    would let a compromised/typo'd monitor bounce the ping to an arbitrary target
    (incl. another scheme). Returning None makes urllib raise instead of follow.
    """

    def redirect_request(self, *args, **kwargs):
        return None


_heartbeat_opener = urllib.request.build_opener(_NoRedirect)


def _heartbeat_ping(url: str) -> None:
    """Send a single liveness GET. Split out from the loop so it is unit-testable.

    Does not follow redirects and reads at most _HEARTBEAT_MAX_BYTES of the body.
    """
    with _heartbeat_opener.open(url, timeout=10) as resp:
        resp.read(_HEARTBEAT_MAX_BYTES)


def _heartbeat_forever(url: str, interval: int) -> None:
    """Ping ``url`` every ``interval`` seconds for the process lifetime.

    Runs in a daemon thread started from main() -- NOT the per-request MCP lifespan,
    which fires on every request and would spawn a heartbeat per session. Failures
    are logged and swallowed so a flaky monitor can never take the server down.
    """
    # The heartbeat URL is a capability URL (the secret is in the path), so log only
    # the host + exception type on failure, never the full URL or exception string.
    host = urllib.parse.urlsplit(url).hostname or "?"
    while True:
        try:
            _heartbeat_ping(url)
        except Exception as e:
            logger.warning("Heartbeat ping to %s failed: %s", host, type(e).__name__)
        time.sleep(interval)


@asynccontextmanager
async def lifespan(server):
    """Per-request MCP lifespan.

    With stateless_http=True this runs on EVERY HTTP request, so it must NOT build
    or tear down the index -- doing so rebuilt the whole index per request and timed
    out large vaults (#28). The index is built once in main() before serving; here we
    only expose the already-built instance to tools.
    """
    yield {"frontmatter_index": frontmatter_index}


# Create the MCP server
mcp = FastMCP(
    "obsidian_web_mcp",
    stateless_http=True,
    json_response=True,
    # Mount path for the MCP transport. Defaults to "/" (via VAULT_MCP_PATH) so
    # connectors that POST to the root complete the handshake instead of 404ing
    # (#19); set VAULT_MCP_PATH to host under a prefix like "/mcp".
    streamable_http_path=VAULT_MCP_PATH,
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            # Operator hostnames from VAULT_MCP_ALLOWED_HOSTS are appended to the
            # loopback defaults above (set it to your tunnel/proxy hostname, e.g.
            # vault-mcp.wzdmai.com).
            *VAULT_MCP_ALLOWED_HOSTS,
        ],
    ),
)


# --- Register all tools ---

from .tools.read import (
    vault_read as _vault_read,
    vault_batch_read as _vault_batch_read,
    vault_read_section as _vault_read_section,
)
from .tools.write import (
    vault_append as _vault_append,
    vault_batch_frontmatter_update as _vault_batch_frontmatter_update,
    vault_edit as _vault_edit,
    vault_write as _vault_write,
    vault_write_binary as _vault_write_binary,
)
from .tools.search import vault_search as _vault_search, vault_search_frontmatter as _vault_search_frontmatter
from .tools.manage import vault_list as _vault_list, vault_move as _vault_move, vault_delete as _vault_delete
from .tools.edit import (
    vault_find_replace as _vault_find_replace,
    vault_insert_at as _vault_insert_at,
    vault_replace_section as _vault_replace_section,
    vault_append_under_heading as _vault_append_under_heading,
    vault_prepend as _vault_prepend,
)
from .tools.graph import (
    vault_links as _vault_links,
    vault_backlinks as _vault_backlinks,
    vault_tags as _vault_tags,
)
from .tools.canvas import (
    vault_canvas_read as _vault_canvas_read,
    vault_canvas_add_node as _vault_canvas_add_node,
    vault_canvas_add_edge as _vault_canvas_add_edge,
)
from .tools.daily import (
    _daily_note_path,
    _today,
    vault_daily_note_path as _vault_daily_note_path,
    vault_daily_note_read as _vault_daily_note_read,
    vault_daily_note_append as _vault_daily_note_append,
)
from .tools.analytics import (
    vault_analytics_summary as _vault_analytics_summary,
    vault_analytics_findings as _vault_analytics_findings,
)
from .models import (
    VaultReadInput,
    VaultWriteInput,
    VaultWriteBinaryInput,
    VaultEditInput,
    VaultAppendInput,
    VaultBatchReadInput,
    VaultBatchFrontmatterUpdateInput,
    VaultSearchInput,
    VaultSearchFrontmatterInput,
    VaultListInput,
    VaultMoveInput,
    VaultDeleteInput,
    VaultFindReplaceInput,
    VaultInsertAtInput,
    VaultReplaceSectionInput,
    VaultAppendUnderHeadingInput,
    VaultPrependInput,
    VaultReadSectionInput,
    VaultLinksInput,
    VaultBacklinksInput,
    VaultTagsInput,
    VaultCanvasReadInput,
    VaultCanvasAddNodeInput,
    VaultCanvasAddEdgeInput,
    VaultDailyNoteAppendInput,
    VaultAnalyticsSummaryInput,
    VaultAnalyticsFindingsInput,
)


def _parse_tool_result(result: str) -> dict:
    """Parse a tool's JSON result into a dict, or {} when it is not a JSON object."""
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_audited(operation: str, func, **context) -> str:
    """Run a tool and emit audit records when auditing covers this operation.

    A straight passthrough when auditing is off (no log path) or the operation is a read
    and read-audit is disabled, so there is no cost on the default path. For mutations the
    target is snapshotted (size + checksum) before and after; reads capture the target as
    it is read. Batch mutations emit one record per file (see _run_audited_batch). An
    audit-write failure is swallowed inside write_audit_record so the trail can never break
    the tool result.
    """
    if not should_audit_operation(operation):
        return func()

    if operation in BATCH_OPERATIONS:
        return _run_audited_batch(operation, func, context)

    is_mutation = operation in MUTATION_OPERATIONS
    before = snapshot_path(before_target_path(operation, context)) if is_mutation else None

    try:
        result = func()
    except Exception:
        write_audit_record(build_audit_record(
            operation=operation,
            target_path=infer_target_path(operation, context),
            before=before,
            operation_status="error",
            error="tool exception",
        ))
        raise

    parsed = _parse_tool_result(result)
    target_path = infer_target_path(operation, context, parsed)
    status = "error" if "error" in parsed else "success"
    error = parsed.get("error") if status == "error" else None
    if is_mutation:
        record = build_audit_record(
            operation=operation, target_path=target_path, before=before,
            after=snapshot_path(target_path), operation_status=status, error=error,
        )
    else:
        record = build_audit_record(
            operation=operation, target_path=target_path,
            before=snapshot_path(target_path), operation_status=status, error=error,
        )
    write_audit_record(record)
    return result


def _run_audited_batch(operation: str, func, context: dict) -> str:
    """Audit a batch mutation as one record per file with correct per-file status.

    The batch tools report per-file outcomes inside ``results`` (some files can fail while
    the call as a whole "succeeds"), so a single top-level record would both hide partial
    failures and lose per-file snapshots. Each file gets its own before/after snapshot and
    its own operation_status.
    """
    paths = [p for p in (context.get("paths") or []) if isinstance(p, str) and p]
    before_map = {p: snapshot_path(p) for p in paths}

    try:
        result = func()
    except Exception:
        for p in paths:
            write_audit_record(build_audit_record(
                operation=operation, target_path=p, before=before_map.get(p),
                operation_status="error", error="tool exception",
            ))
        raise

    parsed = _parse_tool_result(result)
    items = parsed.get("results")
    if not isinstance(items, list) or not items:
        # A tool-level failure (e.g. validation) before any per-file work ran.
        write_audit_record(build_audit_record(
            operation=operation, target_path=paths or None,
            operation_status="error" if "error" in parsed else "success",
            error=parsed.get("error"),
        ))
        return result

    for item in items:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        item_error = item.get("error")
        item_status = "error" if item_error else "success"
        before = before_map.get(path) if isinstance(path, str) else None
        after = snapshot_path(path) if (item_status == "success" and isinstance(path, str)) else None
        write_audit_record(build_audit_record(
            operation=operation, target_path=path, before=before, after=after,
            operation_status=item_status, error=item_error,
        ))
    return result


@mcp.tool(
    name="vault_read",
    description="Read a file from the Obsidian vault, returning content, metadata, and parsed YAML frontmatter.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_read(path: str) -> str:
    """Read a file from the vault."""
    inp = VaultReadInput(path=path)
    return _run_audited("vault_read", lambda: _vault_read(inp.path), path=inp.path)


@mcp.tool(
    name="vault_batch_read",
    description="Read multiple files from the vault in one call. Handles missing files gracefully.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files at once."""
    inp = VaultBatchReadInput(paths=paths, include_content=include_content)
    return _run_audited("vault_batch_read", lambda: _vault_batch_read(inp.paths, inp.include_content))


@mcp.tool(
    name="vault_write",
    description="Write a file to the Obsidian vault. Supports frontmatter merging with existing files. Creates parent directories by default. Set dry_run=true to preview a diff without writing.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_write(path: str, content: str, create_dirs: bool = True, merge_frontmatter: bool = False, dry_run: bool = False) -> str:
    """Write a file to the vault. Set dry_run=true to preview a diff without writing."""
    inp = VaultWriteInput(path=path, content=content, create_dirs=create_dirs, merge_frontmatter=merge_frontmatter, dry_run=dry_run)
    if inp.dry_run:
        # A dry run writes nothing; don't record it as a mutation.
        return _vault_write(inp.path, inp.content, inp.create_dirs, inp.merge_frontmatter, inp.dry_run)
    return _run_audited(
        "vault_write",
        lambda: _vault_write(inp.path, inp.content, inp.create_dirs, inp.merge_frontmatter, inp.dry_run),
        path=inp.path,
    )


@mcp.tool(
    name="vault_write_binary",
    description="Write an allowed binary file (image or PDF) to the Obsidian vault from base64-encoded content. Enforces a media-type/extension allowlist and a size cap; writes atomically.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_write_binary(path: str, data: str, media_type: str, overwrite: bool = False, create_dirs: bool = True) -> str:
    """Write a base64-encoded binary file to the vault."""
    inp = VaultWriteBinaryInput(path=path, data=data, media_type=media_type, overwrite=overwrite, create_dirs=create_dirs)
    return _vault_write_binary(inp.path, inp.data, inp.media_type, inp.overwrite, inp.create_dirs)


@mcp.tool(
    name="vault_edit",
    description=(
        "Patch an existing vault file with exact text replacements. Use this for token-efficient partial edits "
        "when only small fragments change; supports dry-run diff previews and avoids resending the full file."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_edit(path: str, edits: list[dict], dry_run: bool = False) -> str:
    """Patch a file with exact text replacements."""
    inp = VaultEditInput(path=path, edits=edits, dry_run=dry_run)
    if inp.dry_run:
        # A dry run writes nothing; don't record it as a mutation.
        return _vault_edit(inp.path, [edit.model_dump() for edit in inp.edits], inp.dry_run)
    return _run_audited(
        "vault_edit",
        lambda: _vault_edit(inp.path, [edit.model_dump() for edit in inp.edits], inp.dry_run),
        path=inp.path,
    )


@mcp.tool(
    name="vault_append",
    description=(
        "Append content to a vault file without sending the existing file body. Use this for token-efficient "
        "additions; creates the file when it does not exist. Set dry_run=true to preview a diff without writing."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_append(path: str, content: str, separator: str = "\n\n", create_dirs: bool = True, dry_run: bool = False) -> str:
    """Append content to a file. Set dry_run=true to preview a diff without writing."""
    inp = VaultAppendInput(path=path, content=content, separator=separator, create_dirs=create_dirs, dry_run=dry_run)
    if inp.dry_run:
        # A dry run writes nothing; don't record it as a mutation.
        return _vault_append(inp.path, inp.content, inp.separator, inp.create_dirs, inp.dry_run)
    return _run_audited(
        "vault_append",
        lambda: _vault_append(inp.path, inp.content, inp.separator, inp.create_dirs, inp.dry_run),
        path=inp.path,
    )


@mcp.tool(
    name="vault_batch_frontmatter_update",
    description="Update YAML frontmatter fields on multiple files without changing body content. Each update merges new fields into existing frontmatter. Set dry_run=true to preview diffs without writing.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_batch_frontmatter_update(updates: list[dict], dry_run: bool = False) -> str:
    """Batch update frontmatter fields. Set dry_run=true to preview diffs without writing."""
    inp = VaultBatchFrontmatterUpdateInput(updates=updates, dry_run=dry_run)
    if inp.dry_run:
        # A dry run writes nothing; don't record it as a mutation.
        return _vault_batch_frontmatter_update(inp.updates, inp.dry_run)
    return _run_audited(
        "vault_batch_frontmatter_update",
        lambda: _vault_batch_frontmatter_update(inp.updates, inp.dry_run),
        paths=[u.get("path") for u in inp.updates if isinstance(u, dict) and u.get("path")],
    )


@mcp.tool(
    name="vault_search",
    description="Search for text across vault files. Uses ripgrep if available, falls back to Python. Returns matching lines with context and frontmatter excerpts.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_search(
    query: str,
    path_prefix: str | None = None,
    file_pattern: str = "*.md",
    max_results: int = 20,
    context_lines: int = 2,
) -> str:
    """Search vault file contents."""
    inp = VaultSearchInput(query=query, path_prefix=path_prefix, file_pattern=file_pattern, max_results=max_results, context_lines=context_lines)
    return _run_audited(
        "vault_search",
        lambda: _vault_search(inp.query, inp.path_prefix, inp.file_pattern, inp.max_results, inp.context_lines),
    )


@mcp.tool(
    name="vault_search_frontmatter",
    description="Search vault files by YAML frontmatter field values. Queries an in-memory index for fast results. Supports exact match, contains, and field-exists queries.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_search_frontmatter(
    field: str,
    value: str = "",
    match_type: str = "exact",
    path_prefix: str | None = None,
    max_results: int = 20,
) -> str:
    """Search by frontmatter fields."""
    inp = VaultSearchFrontmatterInput(field=field, value=value, match_type=match_type, path_prefix=path_prefix, max_results=max_results)
    return _run_audited(
        "vault_search_frontmatter",
        lambda: _vault_search_frontmatter(inp.field, inp.value, inp.match_type, inp.path_prefix, inp.max_results),
    )


@mcp.tool(
    name="vault_list",
    description="List directory contents in the vault. Supports recursion depth, file/dir filtering, and glob patterns. Excludes .obsidian, .trash, .git directories.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> str:
    """List vault directory contents."""
    inp = VaultListInput(path=path, depth=depth, include_files=include_files, include_dirs=include_dirs, pattern=pattern)
    return _run_audited(
        "vault_list",
        lambda: _vault_list(inp.path, inp.depth, inp.include_files, inp.include_dirs, inp.pattern),
        path=inp.path,
    )


@mcp.tool(
    name="vault_move",
    description="Move a file or directory within the vault. Requires confirm=true to execute -- irreversible, no .trash/ recovery. Set dry_run=true first to check the move would succeed without touching anything.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_move(source: str, destination: str, create_dirs: bool = True, confirm: bool = False, dry_run: bool = False) -> str:
    """Move a file or directory. Requires confirm=true; set dry_run=true to preview."""
    inp = VaultMoveInput(source=source, destination=destination, create_dirs=create_dirs, confirm=confirm, dry_run=dry_run)
    if inp.dry_run:
        # A dry run touches nothing; don't record it as a mutation.
        return _vault_move(inp.source, inp.destination, inp.create_dirs, inp.confirm, inp.dry_run)
    return _run_audited(
        "vault_move",
        lambda: _vault_move(inp.source, inp.destination, inp.create_dirs, inp.confirm, inp.dry_run),
        source=inp.source,
        destination=inp.destination,
    )


@mcp.tool(
    name="vault_delete",
    description="Delete a file by moving it to .trash/ in the vault root. Requires confirm=true as a safety gate. Does NOT hard delete.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_delete(path: str, confirm: bool = False) -> str:
    """Delete a file (move to .trash/)."""
    inp = VaultDeleteInput(path=path, confirm=confirm)
    return _run_audited(
        "vault_delete",
        lambda: _vault_delete(inp.path, inp.confirm),
        path=inp.path,
    )


# --- Local-only: surgical section-aware edit tools (tools/edit.py) ---
# Upstream's vault_edit only covers exact-text find/replace; these give
# heading-relative inserts, section replace/append, and frontmatter-safe
# prepend, none of which upstream has an equivalent for. Preserved through
# the 2026-08-18 upstream merge -- see audit.py's MUTATION_OPERATIONS.

@mcp.tool(
    name="vault_find_replace",
    description="Replace an exact string in a file. occurrence='all' (default), 'first', or an integer N for the Nth match. Errors if the string is absent. Set dry_run=true to preview a unified diff without writing. Use this instead of rewriting a whole file.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_find_replace(path: str, find: str, replace: str, occurrence: str | int = "all", dry_run: bool = False) -> str:
    """Surgically replace an exact string."""
    inp = VaultFindReplaceInput(path=path, find=find, replace=replace, occurrence=occurrence, dry_run=dry_run)
    if inp.dry_run:
        return _vault_find_replace(inp.path, inp.find, inp.replace, inp.occurrence, inp.dry_run)
    return _run_audited(
        "vault_find_replace",
        lambda: _vault_find_replace(inp.path, inp.find, inp.replace, inp.occurrence, inp.dry_run),
        path=inp.path,
    )


@mcp.tool(
    name="vault_insert_at",
    description="Insert content relative to an anchor. Provide exactly one of after_heading (e.g. '## Status') or after_line (exact line). position='after' (default) or 'before' (use before + the first line to insert at the very top). Set dry_run=true to preview.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_insert_at(path: str, content: str, after_heading: str | None = None, after_line: str | None = None, position: str = "after", dry_run: bool = False) -> str:
    """Insert content at a heading/line anchor."""
    inp = VaultInsertAtInput(path=path, content=content, after_heading=after_heading, after_line=after_line, position=position, dry_run=dry_run)
    if inp.dry_run:
        return _vault_insert_at(inp.path, inp.content, inp.after_heading, inp.after_line, inp.position, inp.dry_run)
    return _run_audited(
        "vault_insert_at",
        lambda: _vault_insert_at(inp.path, inp.content, inp.after_heading, inp.after_line, inp.position, inp.dry_run),
        path=inp.path,
    )


@mcp.tool(
    name="vault_replace_section",
    description="Replace everything under a markdown heading (e.g. '## Status') up to the next heading of the same or higher level. The heading line itself is preserved. Set dry_run=true to preview a unified diff without writing.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_replace_section(path: str, heading: str, new_content: str, dry_run: bool = False) -> str:
    """Replace a section body, keeping the heading."""
    inp = VaultReplaceSectionInput(path=path, heading=heading, new_content=new_content, dry_run=dry_run)
    if inp.dry_run:
        return _vault_replace_section(inp.path, inp.heading, inp.new_content, inp.dry_run)
    return _run_audited(
        "vault_replace_section",
        lambda: _vault_replace_section(inp.path, inp.heading, inp.new_content, inp.dry_run),
        path=inp.path,
    )


@mcp.tool(
    name="vault_append_under_heading",
    description="Append content to the END of the section under a heading (before the next same/higher heading), not the end of the whole file. Set dry_run=true to preview.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_append_under_heading(path: str, heading: str, content: str, dry_run: bool = False) -> str:
    """Append to the end of a heading's section."""
    inp = VaultAppendUnderHeadingInput(path=path, heading=heading, content=content, dry_run=dry_run)
    if inp.dry_run:
        return _vault_append_under_heading(inp.path, inp.heading, inp.content, inp.dry_run)
    return _run_audited(
        "vault_append_under_heading",
        lambda: _vault_append_under_heading(inp.path, inp.heading, inp.content, inp.dry_run),
        path=inp.path,
    )


@mcp.tool(
    name="vault_read_section",
    description="Return ONLY the content under a heading (up to the next same/higher heading). Saves context on large files where you need one section, not the whole note.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_read_section(path: str, heading: str) -> str:
    """Read one section of a file."""
    inp = VaultReadSectionInput(path=path, heading=heading)
    return _run_audited("vault_read_section", lambda: _vault_read_section(inp.path, inp.heading), path=inp.path)


@mcp.tool(
    name="vault_prepend",
    description="Add content to the very top of a file, AFTER any YAML frontmatter block (frontmatter is preserved). Set dry_run=true to preview.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_prepend(path: str, content: str, dry_run: bool = False, create_dirs: bool = True) -> str:
    """Prepend content after frontmatter."""
    inp = VaultPrependInput(path=path, content=content, dry_run=dry_run, create_dirs=create_dirs)
    if inp.dry_run:
        return _vault_prepend(inp.path, inp.content, inp.dry_run, inp.create_dirs)
    return _run_audited(
        "vault_prepend",
        lambda: _vault_prepend(inp.path, inp.content, inp.dry_run, inp.create_dirs),
        path=inp.path,
    )


# --- Local-only: graph tools (tools/graph.py) -- no upstream equivalent ---

@mcp.tool(
    name="vault_links",
    description="Return all [[wikilinks]] found in a file (outgoing links), with alias/subpath parsed and a deduped list of unique targets.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_links(path: str) -> str:
    """List a file's outgoing wikilinks."""
    inp = VaultLinksInput(path=path)
    return _run_audited("vault_links", lambda: _vault_links(inp.path), path=inp.path)


@mcp.tool(
    name="vault_backlinks",
    description="Return all notes in the vault that link to `target` (incoming links) -- the graph view Obsidian has natively. target may be given with or without a .md suffix or folder path.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_backlinks(target: str) -> str:
    """Find notes linking to a target."""
    inp = VaultBacklinksInput(target=target)
    return _run_audited("vault_backlinks", lambda: _vault_backlinks(inp.target))


@mcp.tool(
    name="vault_tags",
    description="If tag is given: list all notes carrying that tag (frontmatter or inline #tag). If omitted: return all tags in the vault with counts, sorted by frequency.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_tags(tag: str | None = None) -> str:
    """List notes by tag, or all tags with counts."""
    inp = VaultTagsInput(tag=tag)
    return _run_audited("vault_tags", lambda: _vault_tags(inp.tag))


@mcp.tool(
    name="vault_canvas_read",
    description="Read an Obsidian .canvas file and return its parsed nodes and edges.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_canvas_read(path: str) -> str:
    """Read an Obsidian Canvas file."""
    inp = VaultCanvasReadInput(path=path)
    return _run_audited("vault_canvas_read", lambda: _vault_canvas_read(inp.path), path=inp.path)


@mcp.tool(
    name="vault_canvas_add_node",
    description=(
        "Append a node to an Obsidian .canvas file, creating the file if it does not exist. Requires type, x, y, "
        "width, height; an alphanumeric id is generated when omitted. Unknown node fields are preserved."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_canvas_add_node(path: str, node: dict) -> str:
    """Append a node to a Canvas file."""
    inp = VaultCanvasAddNodeInput(path=path, node=node)
    return _run_audited(
        "vault_canvas_add_node",
        lambda: _vault_canvas_add_node(inp.path, inp.node.model_dump(exclude_none=True, mode="json")),
        path=inp.path,
    )


@mcp.tool(
    name="vault_canvas_add_edge",
    description=(
        "Append an edge to an existing Obsidian .canvas file. Requires fromNode, toNode, and fromSide/toSide "
        "(top, right, bottom, left); both endpoints must reference existing node ids. An alphanumeric id is "
        "generated when omitted."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_canvas_add_edge(path: str, edge: dict) -> str:
    """Append an edge to a Canvas file."""
    inp = VaultCanvasAddEdgeInput(path=path, edge=edge)
    return _run_audited(
        "vault_canvas_add_edge",
        lambda: _vault_canvas_add_edge(inp.path, inp.edge.model_dump(exclude_none=True, mode="json")),
        path=inp.path,
    )


@mcp.tool(
    name="vault_daily_note_path",
    description="Return today's daily-note path (server local date), derived from VAULT_DAILY_NOTES_FOLDER and VAULT_DAILY_NOTES_FORMAT. Does not read or create the file.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_daily_note_path() -> str:
    """Resolve today's daily-note path."""
    return _vault_daily_note_path()


@mcp.tool(
    name="vault_daily_note_read",
    description="Read today's daily note. Returns an error payload (does not create the note) when it does not exist.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_daily_note_read() -> str:
    """Read today's daily note."""
    return _run_audited("vault_daily_note_read", _vault_daily_note_read)


@mcp.tool(
    name="vault_daily_note_append",
    description="Append content to today's daily note, creating it from VAULT_DAILY_NOTES_TEMPLATE when missing. Token-efficient daily logging without resending the note body.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@rate_limited(kind="write")
def vault_daily_note_append(content: str) -> str:
    """Append to today's daily note."""
    inp = VaultDailyNoteAppendInput(content=content)
    return _run_audited(
        "vault_daily_note_append",
        lambda: _vault_daily_note_append(inp.content),
        path=_daily_note_path(_today()),
    )


@mcp.tool(
    name="vault_analytics_summary",
    description=(
        "Return a compact analytics summary for vault hygiene, including frontmatter, link, tag, and encoding "
        "findings. Read-only; scoped to an optional folder prefix."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_analytics_summary(
    path_prefix: str | None = None,
    required_frontmatter: list[str] | None = None,
    max_examples: int = 3,
) -> str:
    """Return a compact analytics summary for vault hygiene."""
    inp = VaultAnalyticsSummaryInput(
        path_prefix=path_prefix,
        required_frontmatter=required_frontmatter,
        max_examples=max_examples,
    )
    return _vault_analytics_summary(inp.path_prefix or "", inp.required_frontmatter, inp.max_examples)


@mcp.tool(
    name="vault_analytics_findings",
    description=(
        "Return detailed findings for one vault analytics category: frontmatter_missing, "
        "required_frontmatter_missing, broken_wikilinks, suspicious_tag_variants, encoding_issues, "
        "or oversized_files. Read-only."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@rate_limited(kind="read")
def vault_analytics_findings(
    category: str,
    path_prefix: str | None = None,
    required_frontmatter: list[str] | None = None,
    max_results: int = 50,
) -> str:
    """Return detailed findings for one analytics category."""
    inp = VaultAnalyticsFindingsInput(
        category=category,
        path_prefix=path_prefix,
        required_frontmatter=required_frontmatter,
        max_results=max_results,
    )
    return _vault_analytics_findings(
        inp.category,
        inp.path_prefix or "",
        inp.required_frontmatter,
        inp.max_results,
    )


def build_app(extensions=()):
    """Assemble the authenticated Starlette app served to clients.

    MCP transport + OAuth routes + (off-root only) the unauthenticated spec probe
    at GET/HEAD / + any extension routes + the bearer-auth middleware. Extracted
    from main() so the exact composition that serves the vault can be exercised
    end-to-end in tests, rather than only the validation helper.

    extensions: optional iterable of extensions.Extension instances; each
    register_routes(app) runs before the auth middleware is attached, so extension
    routes are bearer-protected. A route that collides with an auth-exempt path is
    rejected (fail closed) so an extension cannot expose an unauthenticated endpoint.
    """
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    from .auth import BearerAuthMiddleware
    from .oauth import oauth_routes

    app = mcp.streamable_http_app()

    # MCP spec 2025-06-18 probe: GET/HEAD / answers with the protocol version.
    # Only mount it when MCP is NOT at root -- otherwise the transport owns
    # GET/HEAD / and this route would shadow it. (auth.py exempts GET/HEAD /
    # from bearer auth under the same VAULT_MCP_PATH != "/" guard.)
    if VAULT_MCP_PATH != "/":
        async def mcp_root_probe(request):
            return Response(
                status_code=200,
                headers={"MCP-Protocol-Version": "2025-06-18"},
            )

        app.routes.insert(0, Route("/", mcp_root_probe, methods=["GET", "HEAD"]))

    # Mount OAuth routes (these are excluded from bearer auth via the middleware)
    for route in oauth_routes:
        app.routes.insert(0, route)

    # Health endpoint (bearer-exempt, see auth._AUTH_EXEMPT_PATHS). Surfaces audit status
    # so an operator can confirm the log is enabled and being written.
    async def health(_request):
        # Unauthenticated and reachable over the public tunnel, so keep it to liveness:
        # report only whether auditing is on -- never the log path or write counters,
        # which would leak the host filesystem layout and a vault-activity side-channel.
        return JSONResponse({"status": "ok", "audit": {"enabled": audit_enabled()}})

    app.routes.insert(0, Route("/health", health, methods=["GET"]))

    # Extension routes (e.g. a localhost search endpoint), added before the auth
    # middleware so they are bearer-protected like the MCP transport.
    #
    # TRUST MODEL: extensions are fully-trusted, in-process code the operator passes
    # to serve(). They can do anything the server can (read VAULT_MCP_TOKEN, touch the
    # vault, mutate any route). This is NOT a sandbox and CANNOT stop a hostile
    # extension. The check below is a best-effort FOOTGUN guard for honest authors: it
    # fails closed when a newly-added route would land on an auth-exempt path (which
    # the bearer middleware skips before routing) and would thus be served
    # unauthenticated. It does not (and cannot) defend against an extension that
    # mutates an existing route in place, opens a raw socket, etc.
    from starlette.routing import Match, Mount, WebSocketRoute

    from .auth import _AUTH_EXEMPT_METHOD_PATHS, _AUTH_EXEMPT_PATHS

    extensions = tuple(extensions)
    before_ids = {id(r) for r in app.routes}
    for ext in extensions:
        ext.register_routes(app)
    ext_routes = [r for r in app.routes if id(r) not in before_ids]

    def _covers(route, method, path):
        """Match enum for route vs (method, path); NONE if the probe can't run."""
        try:
            match, _ = route.matches(
                {"type": "http", "method": method, "path": path, "headers": []}
            )
            return match
        except Exception:
            logger.warning(
                "extension route %r could not be auth-checked; allowing "
                "(trusted-extension model)", getattr(route, "path", route)
            )
            return Match.NONE

    for r in ext_routes:
        # Footguns: a Mount can shadow an exempt prefix; a WebSocketRoute isn't covered
        # by the HTTP bearer middleware at all. Reject both with a clear error.
        if isinstance(r, (Mount, WebSocketRoute)):
            raise ValueError(
                f"extension {type(r).__name__} is not allowed: it can serve an "
                "unauthenticated surface -- register plain HTTP Routes instead"
            )
        # Method-AGNOSTIC exempt paths: the whole path is unauthenticated, so ANY
        # coverage (PARTIAL = path matches even if method differs, or FULL) is unsafe.
        for p in _AUTH_EXEMPT_PATHS:
            if _covers(r, "GET", p) is not Match.NONE:
                raise ValueError(
                    f"extension route {getattr(r, 'path', r)!r} covers auth-exempt path "
                    f"{p!r}; it would be served without bearer authentication"
                )
        # Method-SPECIFIC exempt pairs (e.g. GET/HEAD / probe when off-root): only a
        # FULL match of that exact method+path is unsafe -- a POST / route is fine.
        for m, p in _AUTH_EXEMPT_METHOD_PATHS:
            if _covers(r, m, p) is Match.FULL:
                raise ValueError(
                    f"extension route {getattr(r, 'path', r)!r} covers auth-exempt "
                    f"{m} {p!r}; it would be served without bearer authentication"
                )
    app.add_middleware(BearerAuthMiddleware)
    return app


def main():
    """Console-script entry point: run the stock server with no extensions."""
    serve()


def serve(extensions=()):
    """Run the server with the streamable HTTP transport.

    extensions: optional iterable of extensions.Extension instances. A custom
    deployment calls serve([MyExtension()]) from its own entry point to add tools,
    routes, and index hooks without forking this module. With no extensions the
    behavior is identical to the stock server.
    """
    extensions = tuple(extensions)  # consumed multiple times; never a generator
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not VAULT_PATH.is_dir():
        logger.error(f"Vault path does not exist: {VAULT_PATH}")
        sys.exit(1)

    # VAULT_SCOPE_ROOT: lock this instance to a sub-folder of the vault. Validated
    # here (in addition to config.effective_vault_path() raising on an escaping
    # value) so a bad scope fails CLOSED at startup rather than surfacing as a
    # confusing "file not found" on the first tool call.
    if VAULT_SCOPE_ROOT:
        try:
            scoped = effective_vault_path()
        except ValueError as e:
            logger.error(f"Invalid VAULT_SCOPE_ROOT: {e}")
            sys.exit(1)
        if not scoped.is_dir():
            logger.error(f"VAULT_SCOPE_ROOT folder does not exist: {scoped}")
            sys.exit(1)
        logger.warning(f"SCOPED MODE: access locked to sub-folder {VAULT_SCOPE_ROOT!r} ({scoped})")
    else:
        logger.info(f"Full-access mode: vault root {VAULT_PATH}")

    # Validate operator config before serving; fail CLOSED on a bad value.
    try:
        from .config import validate_config
        validate_config()
    except ValueError as e:
        logger.error(f"Invalid configuration: {e}")
        sys.exit(1)

    if not VAULT_MCP_TOKEN:
        logger.warning("VAULT_MCP_TOKEN is not set -- auth will reject all requests")

    # Fail CLOSED on a misconfigured audit log: if auditing is requested but the log path
    # is not writable, refuse to start rather than silently dropping mutation records.
    if audit_enabled() and not audit_path_writable():
        logger.error(f"VAULT_AUDIT_LOG_PATH is not writable: {audit_log_path()}")
        sys.exit(1)
    # Fail CLOSED on an audit log that resolves inside the vault: the vault tools could
    # then overwrite or delete it, defeating the append-only integrity premise.
    if audit_enabled() and audit_path_inside_vault():
        logger.error(
            f"VAULT_AUDIT_LOG_PATH resolves inside the vault ({audit_log_path()}); "
            "the vault tools could rewrite it. Choose a path outside VAULT_PATH."
        )
        sys.exit(1)
    if audit_enabled():
        logger.info(
            "Audit log enabled: %s (reads included: %s)",
            audit_log_path(),
            VAULT_AUDIT_LOG_INCLUDE_READS,
        )

    # Extension setup: register tools BEFORE the app/tool-schema is built, and let
    # each extension prepare before the frontmatter index starts (e.g. attach a
    # change listener so no change is missed between build and listener attach).
    for ext in extensions:
        ext.register_tools(mcp)
        ext.before_indexes_start(frontmatter_index)

    # Build the frontmatter index ONCE, before serving. With stateless_http the
    # per-request MCP lifespan would otherwise rebuild it on every request (#28).
    logger.info(f"Starting vault MCP server. Vault: {VAULT_PATH}")
    frontmatter_index.start()
    atexit.register(frontmatter_index.stop)

    # After the index is built and watching: extensions can start dependent work
    # (e.g. a reconcile loop). shutdown() is registered last so atexit (LIFO) runs
    # it BEFORE frontmatter_index.stop().
    for ext in extensions:
        ext.after_indexes_start(frontmatter_index)
        atexit.register(ext.shutdown)

    # Optional liveness heartbeat. Daemon thread tied to the process (not the
    # per-request lifespan), started only when configured. Validated here so a bad
    # URL scheme or interval fails CLOSED instead of booting silently broken.
    try:
        from .config import validate_heartbeat
        heartbeat_interval = validate_heartbeat()
    except ValueError as e:
        logger.error(f"Invalid heartbeat configuration: {e}")
        sys.exit(1)
    if heartbeat_interval is not None:
        threading.Thread(
            target=_heartbeat_forever,
            args=(VAULT_MCP_HEARTBEAT_URL, heartbeat_interval),
            daemon=True,
            name="heartbeat",
        ).start()
        logger.info("Heartbeat enabled (interval: %ds)", heartbeat_interval)

    # Build the Starlette app with auth middleware and OAuth endpoints.
    #
    # Deliberately NO try/except-with-unauthenticated-fallback here beyond the
    # fail-closed sys.exit below. This used to catch any Exception building the app
    # and fall back to bare mcp.run(transport="streamable-http", ...) with only a
    # logger.warning -- meaning any transient error (a bad import, anything) would
    # silently republish the whole vault to the internet (this server sits behind a
    # public, discoverable Cloudflare Tunnel hostname) with zero auth, and the
    # watchdog wouldn't catch it either since it only checks for a 200 response, not
    # whether auth is actually enforced. Fixed 2026-08-17 as a live-risk finding
    # from the vault-mcp security audit: if this fails now, the process exits and
    # launchd's KeepAlive restarts it -- crash-looping loudly beats silently serving
    # unauthenticated.
    try:
        app = build_app(extensions)
        logger.info(f"Starting server on {VAULT_MCP_HOST}:{VAULT_MCP_PORT} with bearer auth + OAuth")
    except Exception as e:
        # Fail CLOSED: never fall back to an unauthenticated server.
        logger.error(f"Could not build the authenticated app: {e}")
        sys.exit(1)

    import uvicorn
    uvicorn.run(
        app,
        host=VAULT_MCP_HOST,
        port=VAULT_MCP_PORT,
        log_level="info",
        # Honor X-Forwarded-* ONLY from the trusted loopback proxy (Cloudflare
        # Tunnel / Caddy), never from arbitrary clients. Trusting "*" let any
        # caller spoof the advertised OAuth origin via X-Forwarded-Host.
        proxy_headers=True,
        forwarded_allow_ips=VAULT_MCP_FORWARDED_ALLOW_IPS,
    )


if __name__ == "__main__":
    main()
