"""Audit-trail logging for every vault tool call.

Every MCP tool call passes through the wrapper functions registered in
server.py -- the single choke point instrumented here, rather than scattering
logging calls across every tools/*.py file individually. Added 2026-08-17 as
backlog item #1 from the vault-mcp security audit: before this, every
`logger.*` call in tools/*.py was error-path only, so if the bearer token or
OAuth PIN ever leaked there would be zero record of what was actually read or
written. This logs every call -- success and failure alike -- at INFO level
to the same stderr stream the rest of the server already logs to, so it's
picked up by whatever log rotation/collection exists for the launchd process.

Deliberately logs only path/identifier-shaped arguments (which file, which
tag, which query), never `content`/`find`/`replace`/`new_content` bodies --
otherwise the audit log becomes a second, less-protected copy of the vault
itself, which defeats the point of an audit trail.
"""

import functools
import json
import logging
import time

from . import rate_limit

logger = logging.getLogger("obsidian_vault_mcp.audit")

# Argument names worth recording: what was touched, and gate/mode flags that
# change what an entry means. Everything else (content bodies, replacement
# text, frontmatter values) is dropped.
_LOGGED_KEYS = (
    "path", "paths", "source", "destination", "target", "field", "tag",
    "query", "path_prefix", "file_pattern", "after_heading", "after_line",
    "heading", "confirm", "dry_run", "match_type", "occurrence", "updates",
)


def _identifying_args(bound: dict) -> dict:
    """Keep only path/identifier/flag-shaped args; drop content bodies."""
    out = {}
    for k, v in bound.items():
        if k not in _LOGGED_KEYS:
            continue
        if k == "updates" and isinstance(v, list):
            # updates is a list of {"path": ..., "fields": {...}} -- keep the
            # paths, drop the field values being written.
            out[k] = [u.get("path") for u in v if isinstance(u, dict)]
        else:
            out[k] = v
    return out


def _extract_error(result) -> str | None:
    """Tool functions return a JSON string; pull out an "error" key if present."""
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed.get("error") if isinstance(parsed, dict) else None


def audited(tool_name: str, kind: str | None = None):
    """Decorator: log every call to a vault tool with args, outcome, and duration.

    Applied under @mcp.tool so it wraps the plain function (functools.wraps
    keeps __wrapped__ pointing at the original, so FastMCP's signature
    introspection for the tool schema still sees the real parameters).

    `kind` is "read" or "write" (matching each tool's readOnlyHint annotation
    in server.py) or None to skip rate limiting entirely (used for tools that
    are neither, if any are ever added). When set, enforces
    RATE_LIMIT_READ/RATE_LIMIT_WRITE (see rate_limit.py) before calling
    through -- a rejected call short-circuits fn entirely and is logged the
    same as any other error result, not raised, so it reaches the caller as
    a normal tool-error JSON body rather than an MCP-level exception.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = dict(zip(fn.__code__.co_varnames, args))
            bound.update(kwargs)
            entry = {"tool": tool_name, "args": _identifying_args(bound)}
            start = time.monotonic()

            if kind is not None:
                try:
                    rate_limit.check(kind)
                except rate_limit.RateLimitExceeded as e:
                    result = json.dumps({"error": str(e)})
                    entry["duration_ms"] = round((time.monotonic() - start) * 1000, 1)
                    entry["ok"] = False
                    entry["error"] = str(e)
                    entry["rate_limited"] = True
                    logger.info(json.dumps(entry))
                    return result

            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                entry["ok"] = False
                entry["error"] = str(e)
                entry["duration_ms"] = round((time.monotonic() - start) * 1000, 1)
                logger.info(json.dumps(entry))
                raise
            entry["duration_ms"] = round((time.monotonic() - start) * 1000, 1)
            error = _extract_error(result)
            entry["ok"] = error is None
            if error is not None:
                entry["error"] = error
            logger.info(json.dumps(entry))
            return result
        return wrapper
    return decorator
