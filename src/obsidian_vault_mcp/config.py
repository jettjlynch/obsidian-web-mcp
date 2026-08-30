import os
from pathlib import Path

# Vault configuration
VAULT_PATH = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/Obsidian/MyVault")))
VAULT_MCP_TOKEN = os.environ.get("VAULT_MCP_TOKEN", "")
VAULT_MCP_PORT = int(os.environ.get("VAULT_MCP_PORT", "8420"))

# Optional folder-scoping. When VAULT_SCOPE_ROOT names a sub-folder of the vault,
# this server instance is locked to that sub-folder: it becomes the effective vault
# root and every tool refuses any path resolving outside it. Unset = full vault.
VAULT_SCOPE_ROOT = os.environ.get("VAULT_SCOPE_ROOT", "").strip().strip("/")


def effective_vault_path() -> "Path":
    """Root that every tool treats as 'the vault'.

    Returns VAULT_PATH unchanged in full-access mode. When VAULT_SCOPE_ROOT is set,
    returns VAULT_PATH/VAULT_SCOPE_ROOT and that sub-path becomes the root used for
    resolution, listing, search, and the frontmatter index. The scope itself must
    name a folder inside the vault (no absolute path, no '..' escape) or this raises.
    """
    if not VAULT_SCOPE_ROOT:
        return VAULT_PATH

    base = VAULT_PATH.resolve()
    scoped = (base / VAULT_SCOPE_ROOT).resolve()
    if scoped != base and not str(scoped).startswith(str(base) + os.sep):
        raise ValueError(
            f"VAULT_SCOPE_ROOT {VAULT_SCOPE_ROOT!r} resolves outside the vault root"
        )
    return scoped

# OAuth 2.0 client credentials (for Claude app integration)
VAULT_OAUTH_CLIENT_ID = os.environ.get("VAULT_OAUTH_CLIENT_ID", "vault-mcp-client")
VAULT_OAUTH_CLIENT_SECRET = os.environ.get("VAULT_OAUTH_CLIENT_SECRET", "")

# PIN required to approve a new OAuth client at /oauth/authorize. Added
# 2026-08-17 after auditing prouds-mcp's SECURITY.md: without this, /oauth/authorize
# auto-approved ANY caller (no login, no consent page), so anyone who knew this
# server's hostname could complete register -> authorize -> token and obtain the
# real VAULT_MCP_TOKEN in two unauthenticated requests -- PKCE alone doesn't stop
# this since an attacker controls both ends of it. Unset = authorize always
# rejects (fail closed), not fail open.
VAULT_OAUTH_AUTHORIZE_PIN = os.environ.get("VAULT_OAUTH_AUTHORIZE_PIN", "")

# S1 (2026-08-30, closing the rest of C-1/M-1): the PIN gate above stops an
# unauthenticated caller from ever reaching an approval, but it alone did NOT
# validate redirect_uri -- a caller who supplied (or tricked Jett into
# approving) an arbitrary redirect_uri still got the code sent there
# (open redirect). This is the static client's exact-match allowlist,
# consulted by oauth.py alongside the per-client redirect_uris that dynamic
# registrants (e.g. claude.ai's MCP connector) declare via /oauth/register.
# Comma-separated. Default is jarvis-app's real, verified value: its own
# config.ts hardcodes clientId='jarvis-app' and calls
# `AuthSession.makeRedirectUri({ path: 'oauth/callback' })` with no `native`
# override, which (per expo-auth-session's makeRedirectUri -> expo-linking's
# createURL with isTripleSlashed defaulting false) resolves to exactly
# `jarvisapp://oauth/callback` for a standalone build using the app's
# app.json `scheme: "jarvisapp"`. Not a secret -- it's a public callback URL.
VAULT_OAUTH_REDIRECT_URIS = [
    uri.strip()
    for uri in os.environ.get("VAULT_OAUTH_REDIRECT_URIS", "jarvisapp://oauth/callback").split(",")
    if uri.strip()
]

# The client_id jarvis-app's own auth.ts actually sends (hardcoded there as
# OAUTH_CLIENT_ID, independent of VAULT_OAUTH_CLIENT_ID above which is for
# the unrelated client_credentials grant). Pre-registered at import time in
# oauth.py against VAULT_OAUTH_REDIRECT_URIS, since jarvis-app never calls
# /oauth/register -- it goes straight to /oauth/authorize.
VAULT_OAUTH_STATIC_CLIENT_ID = os.environ.get("VAULT_OAUTH_STATIC_CLIENT_ID", "jarvis-app")

# Where the OAuth client registry (client_id -> redirect_uris) and the
# per-client issued-token map persist across restarts -- small JSON beside
# the server, 0600 (see oauth.py). Both files are gitignored; they hold live
# session state, not source.
OAUTH_STATE_DIR = Path(os.environ.get("VAULT_OAUTH_STATE_DIR", str(Path(__file__).resolve().parent.parent.parent)))

# Safety limits
MAX_CONTENT_SIZE = 1_000_000  # 1MB max write size
MAX_BATCH_SIZE = 20           # Max files per batch operation
MAX_SEARCH_RESULTS = 50       # Max results per search
DEFAULT_SEARCH_RESULTS = 20
MAX_LIST_DEPTH = 5            # Max directory recursion depth
CONTEXT_LINES = 2             # Default lines of context in search results

# Directories to never expose or modify
EXCLUDED_DIRS = {".obsidian", ".trash", ".git", ".DS_Store"}

# Frontmatter index refresh interval (seconds)
FRONTMATTER_INDEX_DEBOUNCE = 5.0

# Rate limiting (requests per minute) -- track in-memory, enforce per-token
RATE_LIMIT_READ = 100
RATE_LIMIT_WRITE = 30
