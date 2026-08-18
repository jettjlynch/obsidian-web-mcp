"""In-memory per-minute rate limiting for read and write vault tool calls.

Backlog item #2 from the 2026-08-17 vault-mcp security audit:
RATE_LIMIT_READ/RATE_LIMIT_WRITE were declared in config.py but never
enforced anywhere -- a leaked bearer token meant unlimited scrape/write
speed against a real personal vault.

This is a single global sliding-window counter per kind ("read"/"write"),
not a per-token store, because there is exactly one valid bearer token in
this server: both OAuth grant types in oauth.py hand out the same
VAULT_MCP_TOKEN to every client. "Enforce per-token" collapses to "enforce
per-server" until that changes.

Enforced at the tool-wrapper layer (see the `rate_limited(kind=...)` decorator
below, applied to every vault_* tool in server.py) rather than in auth.py's ASGI
middleware. The middleware is deliberately
pure ASGI and never buffers the request body -- that's what makes the
streamable-HTTP transport work at all (see auth.py's module docstring for
the ClosedResourceError this avoided). Which tool is being called only
becomes known once the JSON-RPC body is parsed, downstream of the
middleware, so the tool-wrapper choke point is the right place to check.
"""

import functools
import json
import logging
import threading
import time
from collections import deque

from . import config

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60.0

_LIMIT_ATTR = {"read": "RATE_LIMIT_READ", "write": "RATE_LIMIT_WRITE"}

_lock = threading.Lock()
_calls: dict[str, deque] = {"read": deque(), "write": deque()}


class RateLimitExceeded(Exception):
    """Raised when a call would exceed the per-minute limit for its kind."""


def check(kind: str) -> None:
    """Record this call and raise RateLimitExceeded if over the limit for `kind`.

    Call once per tool invocation, before doing any real work. Reads the
    configured limit from config fresh on each call (rather than caching it
    at import time) so it can be tuned -- or overridden in tests -- without
    reloading this module.
    """
    limit = getattr(config, _LIMIT_ATTR[kind])
    if not limit:
        return  # zero/unset limit = no enforcement for this kind

    now = time.monotonic()
    with _lock:
        window = _calls[kind]
        cutoff = now - _WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            raise RateLimitExceeded(
                f"Rate limit exceeded: {limit} {kind} calls per minute. Try again shortly."
            )
        window.append(now)


def reset() -> None:
    """Clear all tracked call history. Test-only -- state is otherwise process-lifetime."""
    with _lock:
        for window in _calls.values():
            window.clear()


def rate_limited(kind: str):
    """Decorator: enforce RATE_LIMIT_READ/RATE_LIMIT_WRITE before a tool wrapper runs.

    Applied under @mcp.tool on all 20 vault_* wrappers in server.py, outermost of the two
    per-call decorators -- audit logging (see audit.py's _run_audited, called from inside
    each wrapper body) never runs for a call this rejects, matching the old pre-merge
    behaviour where rate limiting and audit logging lived in the same `audited()` decorator.
    They were split apart when audit.py was replaced wholesale by upstream's append-only
    JSONL audit log (2026-08-18 upstream merge), which audits via an explicit per-wrapper
    `_run_audited(...)` call rather than a decorator -- this decorator is what's left of the
    old `audited(tool_name, kind=...)` once its audit-log half moved into the tool bodies.

    A rejected call short-circuits the wrapped function entirely and returns a normal
    tool-error JSON body (not a raised exception), matching every other tool failure shape
    so MCP clients don't need special-case handling for rate limiting.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                check(kind)
            except RateLimitExceeded as e:
                logger.warning("Rate limit rejected %s call to %s: %s", kind, fn.__name__, e)
                return json.dumps({"error": str(e)})
            return fn(*args, **kwargs)
        return wrapper
    return decorator
