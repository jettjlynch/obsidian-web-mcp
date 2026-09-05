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

C-2 (2026-09-05): this is also where the read/write token-scope split is
enforced -- the same choke point rate-limiting already uses, and for the
same reason (see rate_limit.py's module docstring): auth.py's ASGI
middleware knows a token's granted scope but never learns which tool is
being called, since it never parses the JSON-RPC body. By the time `wrapper`
runs here, both are known.
"""

import functools
import json
import logging
import time

from . import rate_limit
from . import token_scope

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


def _scope_allows(kind: str, granted: str | None) -> bool:
    """Whether a token granted `granted` scope may call a `kind`-class tool.

    write-class tools require the token's granted scope to be exactly
    "write". read-class tools accept either "read" or "write" (write
    implies read). `granted=None` -- no scope was ever determined for this
    request, which should not happen for any real HTTP request (auth.py's
    bearer middleware sets this before a tool can be reached at all -- see
    token_scope.py) -- fails closed for both kinds, matching this server's
    fail-closed posture elsewhere (e.g. an unset OAuth PIN).
    """
    if granted not in ("read", "write"):
        return False
    return granted == "write" if kind == "write" else True


def audited(tool_name: str, kind: str | None = None):
    """Decorator: log every call to a vault tool with args, outcome, and duration.

    Applied under @mcp.tool so it wraps the plain function (functools.wraps
    keeps __wrapped__ pointing at the original, so FastMCP's signature
    introspection for the tool schema still sees the real parameters).

    `kind` is "read" or "write" (matching each tool's readOnlyHint annotation
    in server.py) or None to skip rate limiting AND scope enforcement
    entirely (used for tools that are neither, if any are ever added). When
    set, enforces two gates in order, before calling through:

    1. Token scope (C-2, 2026-09-05): the calling token must have been
       granted at least `kind`-level access (see _scope_allows and
       token_scope.py) or the call is denied outright.
    2. RATE_LIMIT_READ/RATE_LIMIT_WRITE (see rate_limit.py).

    Either rejection short-circuits fn entirely and is logged the same as
    any other error result, not raised, so it reaches the caller as a normal
    tool-error JSON body rather than an MCP-level exception.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = dict(zip(fn.__code__.co_varnames, args))
            bound.update(kwargs)
            entry = {"tool": tool_name, "args": _identifying_args(bound)}
            start = time.monotonic()

            if kind is not None:
                granted = token_scope.current_scope.get()
                if not _scope_allows(kind, granted):
                    needed = "write" if kind == "write" else "read"
                    reason = (
                        f"the presented token only has {granted!r} scope"
                        if granted is not None
                        else "no valid scope was found for this request"
                    )
                    message = f"{tool_name} requires {needed}-scope access; {reason}."
                    result = json.dumps({"error": message})
                    entry["duration_ms"] = round((time.monotonic() - start) * 1000, 1)
                    entry["ok"] = False
                    entry["error"] = message
                    entry["scope_denied"] = True
                    entry["granted_scope"] = granted
                    logger.info(json.dumps(entry))
                    return result

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
        # Exposed for tests (see test_token_scope.py) to introspect every
        # real registered tool's declared kind without hand-maintaining a
        # separate read/write tool list that can drift from server.py's own
        # annotations. `@mcp.tool()` (applied outside this decorator in
        # server.py) returns fn unchanged, so this attribute survives onto
        # the final module-level name, e.g. server.vault_write.audit_kind.
        wrapper.audit_kind = kind
        return wrapper
    return decorator
