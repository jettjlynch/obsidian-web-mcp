"""Obsidian Vault MCP Server.

Exposes read/write access to an Obsidian vault over Streamable HTTP.
Designed to run behind Cloudflare Tunnel for secure remote access.
"""

import json
import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config
from .audit import audited
from .config import VAULT_MCP_PORT, VAULT_OAUTH_AUTHORIZE_PIN, VAULT_PATH, VAULT_SCOPE_ROOT, effective_vault_path
from .frontmatter_index import FrontmatterIndex

logger = logging.getLogger(__name__)

# Global frontmatter index instance
frontmatter_index = FrontmatterIndex()

# Semantic retrieval globals (RETRIEVAL-DESIGN.md / PORT-DESIGN.md). None
# until main() wires them up -- only happens when RETRIEVAL_ENABLED is set,
# see main()'s "Semantic retrieval" section. vault_search_semantic
# (tools/search.py) checks these for None and reports itself unconfigured
# rather than failing.
retrieval_store = None
retrieval_embedder = None
retrieval_indexer = None


@asynccontextmanager
async def lifespan(server):
    """Start frontmatter index on server startup, stop on shutdown."""
    logger.info(f"Starting vault MCP server. Vault: {VAULT_PATH}")

    # Semantic retrieval (RETRIEVAL-DESIGN.md / PORT-DESIGN.md): optional,
    # additive, OFF unless RETRIEVAL_ENABLED is explicitly set -- absent
    # that, the tool exists but reports itself unconfigured (see
    # tools/search.py::vault_search_semantic), and nothing here touches
    # RETRIEVAL_DB_PATH or makes any embedding calls. Registered as a
    # frontmatter_index change-listener BEFORE start() so the watcher thread
    # never has a gap where it's running without one; per §2.3 this keeps
    # re-indexing incremental on every future change, hooked into the SAME
    # watcher (extend, don't stand up a second Observer -- see
    # frontmatter_index.py's ChangeListener docstring for why that matters).
    global retrieval_store, retrieval_embedder, retrieval_indexer
    if config.RETRIEVAL_ENABLED:
        from .retrieval.indexer import RetrievalIndexer
        from .retrieval.store import RetrievalStore

        if config.RETRIEVAL_EMBEDDING_BACKEND == "voyage":
            from .retrieval.embeddings import VoyageEmbedder
            if not config.VOYAGE_API_KEY:
                logger.error("RETRIEVAL_EMBEDDING_BACKEND=voyage but VOYAGE_API_KEY is unset -- retrieval disabled")
                retrieval_embedder = None
            else:
                retrieval_embedder = VoyageEmbedder(
                    api_key=config.VOYAGE_API_KEY,
                    model=config.RETRIEVAL_EMBED_MODEL,
                    dimension=config.RETRIEVAL_EMBED_DIM,
                )
        else:
            from .retrieval.embeddings import OllamaEmbedder
            retrieval_embedder = OllamaEmbedder(
                model=config.RETRIEVAL_EMBED_MODEL,
                dimension=config.RETRIEVAL_EMBED_DIM,
                host=config.OLLAMA_HOST,
            )

        if retrieval_embedder is not None:
            config.RETRIEVAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            retrieval_store = RetrievalStore(config.RETRIEVAL_DB_PATH, embedding_dim=config.RETRIEVAL_EMBED_DIM)
            retrieval_indexer = RetrievalIndexer(
                store=retrieval_store, embedder=retrieval_embedder, vault_root=effective_vault_path()
            )
            frontmatter_index.add_change_listener(
                lambda rel_path, exists: (
                    retrieval_indexer.index_file(rel_path) if exists else retrieval_indexer.delete_file(rel_path)
                )
            )
            logger.info(
                f"Semantic retrieval enabled: backend={config.RETRIEVAL_EMBEDDING_BACKEND} "
                f"model={config.RETRIEVAL_EMBED_MODEL} db={config.RETRIEVAL_DB_PATH} "
                f"(run scripts/reindex_vault.py once to backfill)"
            )
    else:
        logger.info("Semantic retrieval disabled (RETRIEVAL_ENABLED not set) -- vault_search_semantic will report unconfigured")

    frontmatter_index.start()
    logger.info(f"Frontmatter index built: {frontmatter_index.file_count} files indexed")
    yield {"frontmatter_index": frontmatter_index}
    frontmatter_index.stop()
    if retrieval_store is not None:
        retrieval_store.close()
    logger.info("Vault MCP server shut down.")


async def health(request: Request) -> JSONResponse:
    """Genuinely unauthenticated liveness probe.

    auth.py's _AUTH_EXEMPT_PATHS has listed "/health" since the bearer
    middleware was written, but no route ever actually answered it -- any
    request here 404'd (masked as harmless since nothing depended on it
    returning real content, but misleading to anyone checking by hand, and
    the reason mcp_watchdog.sh had to do a full authed MCP `initialize`
    round trip instead of a cheap unauthenticated probe). Deliberately
    minimal: process-up + "has the frontmatter index finished its initial
    build" only -- no vault content (paths, file counts, filenames) and no
    auth state (token/PIN/scope validity). This exists so a monitoring
    script can poll it with no credential at all.
    """
    return JSONResponse({"status": "ok", "index_loaded": frontmatter_index.is_ready})


health_routes = [Route("/health", health, methods=["GET"])]


# Create the MCP server
mcp = FastMCP(
    "obsidian_web_mcp",
    stateless_http=True,
    json_response=True,
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            # Cloudflare Tunnel hostname (added 2026-07-14, replaces ngrok):
            "vault-mcp.wzdmai.com",
            "vault-mcp.wzdmai.com:*",
        ],
    ),
)


# --- Register all tools ---

from .tools.read import (
    vault_read as _vault_read,
    vault_batch_read as _vault_batch_read,
    vault_read_section as _vault_read_section,
)
from .tools.write import vault_write as _vault_write, vault_append as _vault_append, vault_batch_frontmatter_update as _vault_batch_frontmatter_update
from .tools.search import (
    vault_search as _vault_search,
    vault_search_frontmatter as _vault_search_frontmatter,
    vault_search_semantic as _vault_search_semantic,
)
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
    vault_daily as _vault_daily,
)
from .models import (
    VaultReadInput,
    VaultWriteInput,
    VaultAppendInput,
    VaultBatchReadInput,
    VaultBatchFrontmatterUpdateInput,
    VaultSearchInput,
    VaultSearchFrontmatterInput,
    VaultSearchSemanticInput,
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
    VaultDailyInput,
)


@mcp.tool(
    name="vault_read",
    description="Read a file from the Obsidian vault, returning content, metadata, and parsed YAML frontmatter.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_read", kind="read")
def vault_read(path: str) -> str:
    """Read a file from the vault."""
    inp = VaultReadInput(path=path)
    return _vault_read(inp.path)


@mcp.tool(
    name="vault_batch_read",
    description="Read multiple files from the vault in one call. Handles missing files gracefully.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_batch_read", kind="read")
def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files at once."""
    inp = VaultBatchReadInput(paths=paths, include_content=include_content)
    return _vault_batch_read(inp.paths, inp.include_content)


@mcp.tool(
    name="vault_write",
    description="Write a file to the Obsidian vault. Supports frontmatter merging with existing files. Creates parent directories by default.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_write", kind="write")
def vault_write(path: str, content: str, create_dirs: bool = True, merge_frontmatter: bool = False, dry_run: bool = False) -> str:
    """Write a file to the vault. Set dry_run=true to preview a diff without writing."""
    inp = VaultWriteInput(path=path, content=content, create_dirs=create_dirs, merge_frontmatter=merge_frontmatter, dry_run=dry_run)
    return _vault_write(inp.path, inp.content, inp.create_dirs, inp.merge_frontmatter, inp.dry_run)


@mcp.tool(
    name="vault_append",
    description="Append content to the end of a file in the vault without overwriting existing content. Creates the file if it doesn't exist. Prefer this over vault_write when adding to an existing note, to avoid clobbering its contents.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_append", kind="write")
def vault_append(path: str, content: str, create_dirs: bool = True, ensure_newline: bool = True, dry_run: bool = False) -> str:
    """Append content to a vault file. Set dry_run=true to preview a diff without writing."""
    inp = VaultAppendInput(path=path, content=content, create_dirs=create_dirs, ensure_newline=ensure_newline, dry_run=dry_run)
    return _vault_append(inp.path, inp.content, inp.create_dirs, inp.ensure_newline, inp.dry_run)


@mcp.tool(
    name="vault_batch_frontmatter_update",
    description="Update YAML frontmatter fields on multiple files without changing body content. Each update merges new fields into existing frontmatter.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_batch_frontmatter_update", kind="write")
def vault_batch_frontmatter_update(updates: list[dict], dry_run: bool = False) -> str:
    """Batch update frontmatter fields. Set dry_run=true to preview diffs without writing."""
    inp = VaultBatchFrontmatterUpdateInput(updates=updates, dry_run=dry_run)
    return _vault_batch_frontmatter_update(inp.updates, inp.dry_run)


@mcp.tool(
    name="vault_search",
    description="Search for text across vault files. Uses ripgrep if available, falls back to Python. Returns matching lines with context and frontmatter excerpts.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_search", kind="read")
def vault_search(
    query: str,
    path_prefix: str | None = None,
    file_pattern: str = "*.md",
    max_results: int = 20,
    context_lines: int = 2,
) -> str:
    """Search vault file contents."""
    inp = VaultSearchInput(query=query, path_prefix=path_prefix, file_pattern=file_pattern, max_results=max_results, context_lines=context_lines)
    return _vault_search(inp.query, inp.path_prefix, inp.file_pattern, inp.max_results, inp.context_lines)


@mcp.tool(
    name="vault_search_frontmatter",
    description="Search vault files by YAML frontmatter field values. Queries an in-memory index for fast results. Supports exact match, contains, and field-exists queries.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_search_frontmatter", kind="read")
def vault_search_frontmatter(
    field: str,
    value: str = "",
    match_type: str = "exact",
    path_prefix: str | None = None,
    max_results: int = 20,
) -> str:
    """Search by frontmatter fields."""
    inp = VaultSearchFrontmatterInput(field=field, value=value, match_type=match_type, path_prefix=path_prefix, max_results=max_results)
    return _vault_search_frontmatter(inp.field, inp.value, inp.match_type, inp.path_prefix, inp.max_results)


@mcp.tool(
    name="vault_search_semantic",
    description=(
        "Meaning-based search over vault content using embeddings -- finds relevant notes "
        "even when phrased differently than the query. Complements (does not replace) "
        "vault_search (exact text) and vault_search_frontmatter (structured fields). "
        "Wrap a phrase in \"double quotes\" to require it verbatim, or -\"phrase\" to exclude it. "
        "Returns no_good_answer=true with an explicit note when nothing in the vault clears the "
        "confidence cutoff -- never silently falls back to answering from general knowledge."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_search_semantic", kind="read")
def vault_search_semantic(
    query: str,
    path_prefix: str | None = None,
    max_results: int = 10,
    frontmatter_field: str | None = None,
    frontmatter_value: str | None = None,
) -> str:
    """Meaning-based search over vault content."""
    inp = VaultSearchSemanticInput(
        query=query, path_prefix=path_prefix, max_results=max_results,
        frontmatter_field=frontmatter_field, frontmatter_value=frontmatter_value,
    )
    return _vault_search_semantic(inp.query, inp.path_prefix, inp.max_results, inp.frontmatter_field, inp.frontmatter_value)


@mcp.tool(
    name="vault_list",
    description="List directory contents in the vault. Supports recursion depth, file/dir filtering, and glob patterns. Excludes .obsidian, .trash, .git directories.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_list", kind="read")
def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> str:
    """List vault directory contents."""
    inp = VaultListInput(path=path, depth=depth, include_files=include_files, include_dirs=include_dirs, pattern=pattern)
    return _vault_list(inp.path, inp.depth, inp.include_files, inp.include_dirs, inp.pattern)


@mcp.tool(
    name="vault_move",
    description="Move a file or directory within the vault. Requires confirm=true to execute -- irreversible, no .trash/ recovery. Set dry_run=true first to check the move would succeed without touching anything.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_move", kind="write")
def vault_move(source: str, destination: str, create_dirs: bool = True, confirm: bool = False, dry_run: bool = False) -> str:
    """Move a file or directory. Requires confirm=true; set dry_run=true to preview."""
    inp = VaultMoveInput(source=source, destination=destination, create_dirs=create_dirs, confirm=confirm, dry_run=dry_run)
    return _vault_move(inp.source, inp.destination, inp.create_dirs, inp.confirm, inp.dry_run)


@mcp.tool(
    name="vault_delete",
    description="Delete a file by moving it to .trash/ in the vault root. Requires confirm=true as a safety gate. Does NOT hard delete.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_delete", kind="write")
def vault_delete(path: str, confirm: bool = False) -> str:
    """Delete a file (move to .trash/)."""
    inp = VaultDeleteInput(path=path, confirm=confirm)
    return _vault_delete(inp.path, inp.confirm)


# --- Tier 1: surgical edits ---

@mcp.tool(
    name="vault_find_replace",
    description="Replace an exact string in a file. occurrence='all' (default), 'first', or an integer N for the Nth match. Errors if the string is absent. Set dry_run=true to preview a unified diff without writing. Use this instead of rewriting a whole file.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_find_replace", kind="write")
def vault_find_replace(path: str, find: str, replace: str, occurrence: str | int = "all", dry_run: bool = False) -> str:
    """Surgically replace an exact string."""
    inp = VaultFindReplaceInput(path=path, find=find, replace=replace, occurrence=occurrence, dry_run=dry_run)
    return _vault_find_replace(inp.path, inp.find, inp.replace, inp.occurrence, inp.dry_run)


@mcp.tool(
    name="vault_insert_at",
    description="Insert content relative to an anchor. Provide exactly one of after_heading (e.g. '## Status') or after_line (exact line). position='after' (default) or 'before' (use before + the first line to insert at the very top). Set dry_run=true to preview.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_insert_at", kind="write")
def vault_insert_at(path: str, content: str, after_heading: str | None = None, after_line: str | None = None, position: str = "after", dry_run: bool = False) -> str:
    """Insert content at a heading/line anchor."""
    inp = VaultInsertAtInput(path=path, content=content, after_heading=after_heading, after_line=after_line, position=position, dry_run=dry_run)
    return _vault_insert_at(inp.path, inp.content, inp.after_heading, inp.after_line, inp.position, inp.dry_run)


@mcp.tool(
    name="vault_replace_section",
    description="Replace everything under a markdown heading (e.g. '## Status') up to the next heading of the same or higher level. The heading line itself is preserved. Set dry_run=true to preview a unified diff without writing.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_replace_section", kind="write")
def vault_replace_section(path: str, heading: str, new_content: str, dry_run: bool = False) -> str:
    """Replace a section body, keeping the heading."""
    inp = VaultReplaceSectionInput(path=path, heading=heading, new_content=new_content, dry_run=dry_run)
    return _vault_replace_section(inp.path, inp.heading, inp.new_content, inp.dry_run)


# --- Tier 2: section-aware read/write ---

@mcp.tool(
    name="vault_append_under_heading",
    description="Append content to the END of the section under a heading (before the next same/higher heading), not the end of the whole file. Set dry_run=true to preview.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_append_under_heading", kind="write")
def vault_append_under_heading(path: str, heading: str, content: str, dry_run: bool = False) -> str:
    """Append to the end of a heading's section."""
    inp = VaultAppendUnderHeadingInput(path=path, heading=heading, content=content, dry_run=dry_run)
    return _vault_append_under_heading(inp.path, inp.heading, inp.content, inp.dry_run)


@mcp.tool(
    name="vault_read_section",
    description="Return ONLY the content under a heading (up to the next same/higher heading). Saves context on large files where you need one section, not the whole note.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_read_section", kind="read")
def vault_read_section(path: str, heading: str) -> str:
    """Read one section of a file."""
    inp = VaultReadSectionInput(path=path, heading=heading)
    return _vault_read_section(inp.path, inp.heading)


@mcp.tool(
    name="vault_prepend",
    description="Add content to the very top of a file, AFTER any YAML frontmatter block (frontmatter is preserved). Set dry_run=true to preview.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_prepend", kind="write")
def vault_prepend(path: str, content: str, dry_run: bool = False, create_dirs: bool = True) -> str:
    """Prepend content after frontmatter."""
    inp = VaultPrependInput(path=path, content=content, dry_run=dry_run, create_dirs=create_dirs)
    return _vault_prepend(inp.path, inp.content, inp.dry_run, inp.create_dirs)


# --- Tier 3: graph + convenience ---

@mcp.tool(
    name="vault_links",
    description="Return all [[wikilinks]] found in a file (outgoing links), with alias/subpath parsed and a deduped list of unique targets.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_links", kind="read")
def vault_links(path: str) -> str:
    """List a file's outgoing wikilinks."""
    inp = VaultLinksInput(path=path)
    return _vault_links(inp.path)


@mcp.tool(
    name="vault_backlinks",
    description="Return all notes in the vault that link to `target` (incoming links) -- the graph view Obsidian has natively. target may be given with or without a .md suffix or folder path.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_backlinks", kind="read")
def vault_backlinks(target: str) -> str:
    """Find notes linking to a target."""
    inp = VaultBacklinksInput(target=target)
    return _vault_backlinks(inp.target)


@mcp.tool(
    name="vault_tags",
    description="If tag is given: list all notes carrying that tag (frontmatter or inline #tag). If omitted: return all tags in the vault with counts, sorted by frequency.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
@audited("vault_tags", kind="read")
def vault_tags(tag: str | None = None) -> str:
    """List notes by tag, or all tags with counts."""
    inp = VaultTagsInput(tag=tag)
    return _vault_tags(inp.tag)


@mcp.tool(
    name="vault_daily",
    description="Resolve today's Daily/YYYY-MM-DD.md (Europe/London). Creates it with the standard daily header if missing. If content is given, appends it (under heading if provided, else end of file). If no content, returns today's daily note. Set dry_run=true to preview any create/append.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
@audited("vault_daily", kind="write")
def vault_daily(content: str | None = None, heading: str | None = None, dry_run: bool = False) -> str:
    """Resolve/create/append/read today's daily note."""
    inp = VaultDailyInput(content=content, heading=heading, dry_run=dry_run)
    return _vault_daily(inp.content, inp.heading, inp.dry_run)


def main():
    """Entry point. Run with streamable HTTP transport."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not VAULT_PATH.is_dir():
        logger.error(f"Vault path does not exist: {VAULT_PATH}")
        sys.exit(1)

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

    # VAULT_MCP_TOKEN no longer gates auth here at all (C-2 follow-up,
    # 2026-09-05 -- see oauth.get_token_scope's docstring): the old "unset =
    # auth will reject all requests" warning would now be actively wrong,
    # since a scoped per-client token authenticates regardless of this
    # value. The .env key itself is left alone -- app-bridge (a separate
    # process/repo) sources this same .env file for its own, independent
    # static-token check, untouched by this change.
    if not VAULT_OAUTH_AUTHORIZE_PIN:
        logger.warning(
            "VAULT_OAUTH_AUTHORIZE_PIN is not set -- /oauth/authorize will reject "
            "every request (fail closed), so no NEW client can complete OAuth "
            "sign-in until it's set. Already-issued bearer tokens still work."
        )

    # Build the Starlette app with auth middleware and OAuth endpoints.
    #
    # Deliberately NO try/except-with-unauthenticated-fallback here. This used
    # to catch any Exception building the app and fall back to bare
    # mcp.run(transport="streamable-http", ...) with only a logger.warning --
    # meaning any transient error (a bad import, anything) would silently
    # republish the whole vault to the internet (this server sits behind a
    # public, discoverable Cloudflare Tunnel hostname) with zero auth, and the
    # watchdog wouldn't catch it either since it only checks for a 200
    # response, not whether auth is actually enforced. Fixed 2026-08-17 as a
    # live-risk finding from the vault-mcp security audit: if this fails now,
    # the process exits and launchd's KeepAlive restarts it -- crash-looping
    # loudly beats silently serving unauthenticated.
    from .auth import BearerAuthMiddleware
    from .oauth import oauth_routes

    app = mcp.streamable_http_app()

    # Mount /health + OAuth routes (all excluded from bearer auth via the
    # middleware's _AUTH_EXEMPT_PATHS -- /health genuinely needs no token).
    for route in health_routes:
        app.routes.insert(0, route)
    for route in oauth_routes:
        app.routes.insert(0, route)

    app.add_middleware(BearerAuthMiddleware)
    logger.info(f"Starting server on port {VAULT_MCP_PORT} with bearer auth + OAuth")

    import uvicorn
    # 127.0.0.1, NOT 0.0.0.0: the only legitimate non-local caller is the
    # Cloudflare Tunnel, and cloudflared runs on this machine and dials
    # localhost:8420 (see ~/.cloudflared/config-vault-mcp.yml). A wildcard
    # bind exposed the port to the whole LAN — which on this network
    # (shared-building ISP, customer.ask4.lan) means strangers' devices.
    # allowed_hosts does NOT protect against that (it only checks the Host
    # header, which any direct caller can forge). Changed 2026-07-14.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=VAULT_MCP_PORT,
        log_level="info",
        # Honor X-Forwarded-* ONLY from the trusted loopback proxy (Cloudflare
        # Tunnel / Caddy), never from arbitrary clients. Trusting "*" let any
        # caller spoof the advertised OAuth origin via X-Forwarded-Host --
        # merged in 2026-09-06 from jimprosser/obsidian-web-mcp's independent
        # fix for the same class of bug (upstream commit 669775a); see
        # config.VAULT_MCP_FORWARDED_ALLOW_IPS's docstring.
        proxy_headers=True,
        forwarded_allow_ips=config.VAULT_MCP_FORWARDED_ALLOW_IPS,
    )


if __name__ == "__main__":
    main()
