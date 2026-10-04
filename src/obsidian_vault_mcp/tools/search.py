"""Search tools for the Obsidian vault MCP server."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import frontmatter

from .. import config
from ..markdown import json_safe
from ..vault import resolve_vault_path, resolve_vault_read_path

logger = logging.getLogger(__name__)


def _search_ripgrep(
    query: str,
    search_path: Path,
    file_pattern: str,
    max_results: int,
    context_lines: int,
) -> list[dict]:
    """Search using ripgrep for performance."""
    cmd = [
        "rg",
        "--json",
        f"--max-count={max_results}",
        f"--glob={file_pattern}",
        "-i",
        f"--context={context_lines}",
    ]

    for excluded in config.EXCLUDED_DIRS:
        cmd.append(f"--glob=!{excluded}/")

    # EMERGENCY FIX 2026-08-18: `query` used to be appended as a bare
    # positional argv element. A query beginning with "-" was therefore
    # parsed by ripgrep as an OPTION, not a search pattern -- in particular
    # vault_search(query="--pre=/bin/sh") sets ripgrep's --pre preprocessor,
    # which runs an arbitrary program against every searched file: remote
    # code execution reachable from the search tool's query argument by any
    # authenticated caller (or via adversarial vault content, if a query is
    # ever derived from it). Root-caused while comparing against
    # jimprosser/obsidian-web-mcp's independent fix for the identical bug
    # (their commit a4cf931). `-e` forces ripgrep to treat what follows as
    # the search pattern regardless of a leading "-", closing the injection.
    cmd += ["-e", query, str(search_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    matches = []

    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("type") == "match":
            match_data = data["data"]
            file_path = match_data["path"]["text"]
            try:
                rel_path = str(Path(file_path).relative_to(config.effective_vault_path()))
                # rg has already read the bytes. A refusal must discard its match,
                # not fall back to the line in the JSON event.
                resolve_vault_read_path(rel_path)
            except (ValueError, OSError):
                continue

            line_number = match_data["line_number"]
            line_text = match_data["lines"]["text"].rstrip("\n")

            matches.append({
                "path": rel_path,
                "line_number": line_number,
                "match_context": line_text,
            })

            if len(matches) >= max_results:
                break

    return matches


def _search_python(
    query: str,
    search_path: Path,
    file_pattern: str,
    max_results: int,
    context_lines: int,
) -> list[dict]:
    """Fallback Python-based search."""
    import fnmatch

    query_lower = query.lower()
    matches = []

    for file_path in search_path.rglob("*"):
        if not file_path.is_file():
            continue

        if any(part in config.EXCLUDED_DIRS for part in file_path.parts):
            continue

        if not fnmatch.fnmatch(file_path.name, file_pattern):
            continue

        try:
            rel_path = str(file_path.relative_to(config.effective_vault_path()))
            safe_path = resolve_vault_read_path(rel_path)
            content = safe_path.read_text(encoding="utf-8")
        except (ValueError, OSError):
            continue

        lines = content.splitlines()
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                context = "\n".join(lines[start:end])

                matches.append({
                    "path": rel_path,
                    "line_number": i + 1,
                    "match_context": context,
                })

                if len(matches) >= max_results:
                    return matches

    return matches


def _get_frontmatter_excerpt(file_path: Path, max_keys: int = 3) -> dict | None:
    """Read frontmatter from a file, returning first N key-value pairs."""
    try:
        rel_path = str(file_path.relative_to(config.effective_vault_path()))
        safe_path = resolve_vault_read_path(rel_path)
        content = safe_path.read_text(encoding="utf-8")
        post = frontmatter.loads(content)
        if not post.metadata:
            return None
        keys = list(post.metadata.keys())[:max_keys]
        return json_safe({k: post.metadata[k] for k in keys})
    except Exception:
        return None


def vault_search(
    query: str,
    path_prefix: str | None = None,
    file_pattern: str = "*.md",
    max_results: int = 20,
    context_lines: int = 2,
) -> str:
    """Search for text across vault files."""
    try:
        # Defense-in-depth: the pydantic model already caps max_results, but
        # this function is also called directly (see server.py's readOnlyHint
        # annotations vs a hypothetical second caller) -- clamp here too,
        # same pattern as list_directory's depth clamp in vault.py.
        max_results = min(max_results, config.MAX_SEARCH_RESULTS)

        if path_prefix:
            search_path = resolve_vault_path(path_prefix)
        else:
            search_path = config.effective_vault_path()

        if not search_path.is_dir():
            return json.dumps({"error": f"Search path is not a directory: {path_prefix}"})

        if shutil.which("rg"):
            matches = _search_ripgrep(query, search_path, file_pattern, max_results, context_lines)
        else:
            matches = _search_python(query, search_path, file_pattern, max_results, context_lines)

        for match in matches:
            file_full_path = config.effective_vault_path() / match["path"]
            match["frontmatter_excerpt"] = _get_frontmatter_excerpt(file_full_path)

        truncated = len(matches) >= max_results

        return json.dumps({
            "results": matches,
            "total_matches": len(matches),
            "truncated": truncated,
        })
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"vault_search error: {e}")
        return json.dumps({"error": str(e)})


def vault_search_frontmatter(
    field: str,
    value: str = "",
    match_type: str = "exact",
    path_prefix: str | None = None,
    max_results: int = 20,
) -> str:
    """Search vault files by frontmatter field values using the in-memory index."""
    from ..server import frontmatter_index

    try:
        max_results = min(max_results, config.MAX_SEARCH_RESULTS)

        results = frontmatter_index.search_by_field(
            field=field,
            value=value,
            match_type=match_type,
            path_prefix=path_prefix,
        )

        formatted = []
        for item in results[:max_results]:
            path = item["path"]
            fm = item["frontmatter"]
            title = fm.get("title", Path(path).stem)
            formatted.append({
                "path": path,
                "frontmatter": json_safe(fm),
                "title": json_safe(title),
            })

        truncated = len(results) > max_results

        return json.dumps({
            "results": formatted,
            "total": len(formatted),
            "truncated": truncated,
        })
    except Exception as e:
        logger.error(f"vault_search_frontmatter error: {e}")
        return json.dumps({"error": str(e)})


def vault_search_semantic(
    query: str,
    path_prefix: str | None = None,
    max_results: int = 10,
    frontmatter_field: str | None = None,
    frontmatter_value: str | None = None,
) -> str:
    """Meaning-based search over vault content.

    This server is single-tenant (no per-request identity/scope like
    prouds-mcp's multi-user model): the process-wide VAULT_SCOPE_ROOT is the
    only folder-scoping in play, same as vault_search and
    vault_search_frontmatter above -- so, like them, this passes it straight
    through into the store's own query-time filter (RETRIEVAL-DESIGN.md
    §3.1) rather than implementing a second, parallel access check.
    """
    from ..retrieval.query import semantic_search
    from .. import server

    if server.retrieval_store is None or server.retrieval_embedder is None:
        return json.dumps({"error": "Semantic search is not configured on this server (RETRIEVAL_ENABLED not set)."})

    scope_root = config.VAULT_SCOPE_ROOT
    effective_prefix = path_prefix
    if scope_root:
        effective_prefix = f"{scope_root}/{path_prefix.lstrip('/')}" if path_prefix else scope_root

    try:
        response = semantic_search(
            query=query,
            embedder=server.retrieval_embedder,
            store=server.retrieval_store,
            user_scope=effective_prefix or "",
            top_k=max_results,
            max_distance=config.RETRIEVAL_MAX_DISTANCE,
            frontmatter_field=frontmatter_field,
            frontmatter_value=frontmatter_value,
        )

        results = []
        for r in response.results:
            p = r.file_path
            if scope_root:
                if not (p == scope_root or p.startswith(scope_root + "/")):
                    # Should be unreachable given store-level scoping -- skip
                    # defensively rather than ever leak a path outside scope.
                    continue
                display_path = p[len(scope_root) + 1:] if p != scope_root else p
            else:
                display_path = p
            results.append({
                "path": display_path,
                "text": r.text,
                "heading_path": list(r.heading_path),
                "line": r.line_start,
                "distance": r.distance,
            })

        out = {
            "results": results,
            "total": len(results),
            "no_good_answer": response.no_good_answer,
            "searched_summary": response.searched_summary,
        }
        if response.no_good_answer:
            out["note"] = (
                "I didn't find anything in the vault covering this -- this answer "
                "isn't backed by a note."
            )
        return json.dumps(out)
    except Exception as e:
        logger.error(f"vault_search_semantic error: {e}")
        return json.dumps({"error": str(e)})
