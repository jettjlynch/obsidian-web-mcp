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

# --- Semantic retrieval (RETRIEVAL-DESIGN.md / PORT-DESIGN.md) --------------
# Optional feature, OFF by default: main() only wires up the store/embedder/
# indexer when this is explicitly set. Without it, vault_search_semantic
# exists on the tool surface but reports itself unconfigured, and the server
# never touches RETRIEVAL_DB_PATH or makes any embedding calls -- this
# matters in practice, not just in theory: Ollama is now installed as a real
# system service on this Mac Mini, so an "always try if reachable" default
# would make every server start (including test subprocesses) silently
# write into the real retrieval db and hit a real local model. Opt-in avoids
# that entirely; explicit is better than a happy accident of what's
# currently running on the machine.
RETRIEVAL_ENABLED = os.environ.get("RETRIEVAL_ENABLED", "").strip().lower() in ("1", "true", "yes")

# Backend defaults to LOCAL (Ollama), decided 2026-09-06 after the "voyage"
# backend hit a real 3 RPM rate limit on an un-carded account and Jett chose
# local/open-source over adding a new paid vendor relationship: zero cost,
# zero rate limit, runs on the same Mac Mini as everything else here.
# "voyage" is kept available (already built, tested, proven against the
# real API) for anyone who'd rather use a hosted model.
RETRIEVAL_EMBEDDING_BACKEND = os.environ.get("RETRIEVAL_EMBEDDING_BACKEND", "ollama")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")

# Sits outside the vault, same convention as other out-of-vault state: one
# file to back up, delete-and-rebuild, or point a health-check at. This is
# the SAME path prouds-mcp's port of this feature uses -- deliberately, so
# the real index already built there (11,293 files, 297,479 chunks) is
# picked up here as-is rather than triggering a fresh reindex.
RETRIEVAL_DB_PATH = Path(
    os.environ.get("RETRIEVAL_DB_PATH", os.path.expanduser("~/.config/prouds-mcp/retrieval.sqlite"))
)

# nomic-embed-text (Apache-2.0, retrieval-tuned, ~274MB, CPU-friendly) is the
# local default -- dimension 768, confirmed live against a real `ollama pull`
# + `/api/embed` call (2026-09-06). voyage-4-large (dim 1024) is the hosted
# alternative if RETRIEVAL_EMBEDDING_BACKEND=voyage. A model, dimension, OR
# backend change is a full-corpus regeneration event (§2.1) -- change
# deliberately, then run scripts/reindex_vault.py, never silently.
_EMBED_MODEL_DEFAULTS = {"ollama": "nomic-embed-text", "voyage": "voyage-4-large"}
_EMBED_DIM_DEFAULTS = {"ollama": 768, "voyage": 1024}
RETRIEVAL_EMBED_MODEL = os.environ.get(
    "RETRIEVAL_EMBED_MODEL", _EMBED_MODEL_DEFAULTS.get(RETRIEVAL_EMBEDDING_BACKEND, "nomic-embed-text")
)
RETRIEVAL_EMBED_DIM = int(
    os.environ.get("RETRIEVAL_EMBED_DIM", str(_EMBED_DIM_DEFAULTS.get(RETRIEVAL_EMBEDDING_BACKEND, 768)))
)

RETRIEVAL_TOP_K = int(os.environ.get("RETRIEVAL_TOP_K", "10"))
# Raw L2 distance cutoff (sqlite-vec's default vec0 metric) -- §3.3 calls the
# right threshold model-dependent and tunable, not a fixed universal
# constant, hence a config knob rather than a hardcoded value. 0.85 is
# empirically calibrated against nomic-embed-text's real distance
# distribution on Jett's actual vault (see retrieval/query.py's
# DEFAULT_MAX_DISTANCE comment for the measurements) -- changing embedding
# backend/model likely means re-measuring and updating this.
RETRIEVAL_MAX_DISTANCE = float(os.environ.get("RETRIEVAL_MAX_DISTANCE", "0.85"))
RETRIEVAL_CHUNK_MAX_TOKENS = int(os.environ.get("RETRIEVAL_CHUNK_MAX_TOKENS", "250"))
