"""Fresh, uncached reads of the OAuth issued-token store.

Why this exists rather than just importing oauth.py's own lookup functions
(2026-09-07): oauth.py's `_issued_tokens` is a module-level dict loaded ONCE
at import time and mutated in-process thereafter -- correct for
obsidian-web-mcp's own single long-lived server process, but wrong for a
SECOND, independent process reading the same file. app-bridge (separate
repo, separate `python server.py` process, port 8421) needs to validate
bridge-audience tokens minted by *this* server's `/oauth/token` -- if it
imported oauth.py directly, it would get its own private copy of
`_issued_tokens`, frozen at whatever `oauth_tokens.json` contained the
moment app-bridge itself last started. A token minted by a phone OAuth flow
five minutes ago would be invisible to app-bridge until app-bridge's own
process restarts, since Python module globals never share across process
boundaries. That's a real bug, caught by tracing the actual process
topology before writing the "obvious" fix (import oauth.py, call its
function) -- not by testing and getting lucky.

This module is the actual "shared verification approach" PLAN.md's S2 spec
asked for: one file format (oauth_tokens.json), one read path, re-read from
disk on every single call rather than trusted from a process-local cache
that could be hours or days stale relative to what another process most
recently wrote. No module-level caching. No side effects -- unlike
importing oauth.py, which would also trigger its module-level
_purge_unscoped_tokens() and the jarvis-app client pre-registration write,
neither of which app-bridge has any business doing. Read-only: nothing here
ever writes oauth_tokens.json -- minting stays oauth.py's job, the only
process that should ever create a token.
"""

import json
import time

from . import config

_TOKENS_FILE = config.OAUTH_STATE_DIR / "oauth_tokens.json"


def lookup_issued_token(token: str) -> dict | None:
    """The record for `token` -- {client_id, expires_at, scope, audience} --
    read fresh from disk, or None if it's unknown, expired, or the store is
    unreadable/corrupt. Fails closed on a bad file (same as oauth.py's own
    _load_json treats a corrupt file as empty, never as "auth not required").
    Callers check `audience` themselves -- this function doesn't filter by
    it, since it has no opinion on who's asking (that's oauth.py's own
    get_issued_token_scope for the vault side, and app-bridge's own
    _require_auth for the bridge side).
    """
    try:
        data = json.loads(_TOKENS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(token)
    if not entry or entry.get("expires_at", 0) < time.time():
        return None
    return entry
