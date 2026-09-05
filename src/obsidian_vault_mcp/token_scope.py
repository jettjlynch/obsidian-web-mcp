"""Per-request token-scope propagation from auth.py's bearer middleware to
audit.py's tool-wrapper choke point (SECURITY.md C-2: read/write scope split).

Why a contextvar and not a scope check inside auth.py's middleware directly:
the same reasoning rate_limit.py documents for its own placement applies here.
auth.py's BearerAuthMiddleware is deliberately pure ASGI and never buffers or
parses the request body (see auth.py's module docstring -- that's what keeps
the streamable-HTTP transport from hitting anyio.ClosedResourceError). Which
MCP tool a request is calling -- and therefore whether write access is
required -- isn't known until deep inside FastMCP's JSON-RPC dispatch,
downstream of the middleware. So auth.py determines and records the *scope
the presented token was granted*; audit.py's `audited()` decorator -- the
same choke point rate-limiting already uses, for the same reason -- reads it
back once it actually knows which tool (and its declared read/write `kind`)
is being invoked.
"""

import contextvars

# Set by auth.py's BearerAuthMiddleware once a token passes validation, reset
# when that request finishes. None means "no request in flight" -- or, if a
# tool is somehow called without going through the middleware at all (e.g. a
# test calling a decorated tool function directly with no context set up),
# "no scope was ever granted". audit.py treats None as ungranted and fails
# closed on every tool that declares a `kind`, matching this server's
# fail-closed posture elsewhere (see e.g. VAULT_OAUTH_AUTHORIZE_PIN unset).
current_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_scope", default=None
)
