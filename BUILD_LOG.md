# BUILD_LOG: obsidian-web-mcp (Jett's personal vault-mcp)

Jett's own remote MCP server over the JJ Second Brain vault (live as `com.jettlynch.vault-mcp`, reached by claude.ai via cloudflared -> vault-mcp.wzdmai.com). Forked from jimprosser/obsidian-web-mcp. Newest entry first. Each entry: What / Why / Files touched / Rollback / Verified.

First-hand operational history already lives in `OPERATIONS.md` (outages, tunnel migration, security work, to 2026-09-07). This file adds the build-level view and the reusable-capabilities section. Capability index: `~/work/WZDM/BUILD_INDEX.md`. For new client deployments prefer `~/code/wzdm-vault-template`.

---

## Reusable capabilities

Derived from module docstrings under `src/obsidian_vault_mcp/` and `scripts/` (2026-10-03).

| Capability | What it does | Inputs / outputs | Entry point | Maturity |
|---|---|---|---|---|
| Remote vault MCP (read/write/edit/search/graph tools) | Streamable-HTTP MCP over a markdown+frontmatter vault; read-before-write surgical edits; dry_run diffs | MCP tool calls | `server.py`, `tools/{read,write,edit,search,graph,manage}.py`, `vault.py` | prod (live) |
| Semantic retrieval (hybrid) | Heading-aware chunker, sqlite-vec store, pluggable embeddings (Ollama nomic-embed-text default, Voyage optional), incremental content-hash reindex, confidence cutoff + diversification | md files -> chunks/vectors; query -> ranked chunks | `retrieval/{chunker,store,embeddings,indexer,query}.py`, tool `vault_search_semantic`, `scripts/reindex_vault.py`, `scripts/verify_retrieval_quality.py` | tested (off unless `RETRIEVAL_ENABLED`; live status: check OPERATIONS.md) |
| OAuth 2.0 PKCE with PIN gate + multi-audience tokens | Claude connector auth; mints vault-scoped and bridge-scoped tokens (used by app-bridge) | OAuth flow -> tokens | `oauth.py`, `token_store.py` | prod |
| Read/write token-scope split (C-2) | Read-only tokens cannot call write tools; enforced at the audit wrapper choke point | token -> scope | `token_scope.py`, `audit.py` | prod |
| Audit trail of every tool call | Single choke point wrapper logs every call | calls -> log | `audit.py` | prod |
| Rate limiting | RATE_LIMIT_READ/WRITE enforced | calls -> 429 | `rate_limit.py` | prod |
| Folder scoping | `VAULT_SCOPE_ROOT` makes a sub-folder the effective root, refuses escapes | env -> scoped instance | `config.py` `effective_vault_path()` | tested |
| Frontmatter index | In-memory YAML frontmatter index across the vault | md files -> query results | `frontmatter_index.py` | prod |
| Watchdog + Cloudflare tunnel ops | Health watchdog, tunnel setup, launchd plists | n/a | `mcp_watchdog.sh`, `scripts/setup-tunnel.sh`, `scripts/launchd/*` | prod |

---

## 2026-10-03 — Found uncommitted oauth.py change (unlogged)

**What:** `src/obsidian_vault_mcp/oauth.py` has one uncommitted change (file mtime 2026-09-18 09:13): `_TOKEN_TTL_SECONDS` raised from `86400` (24h) to `30 * 86400` (30 days) for access tokens issued by the authorization_code grant, with the comment rewritten to say "Extended 2026-09-18 (Jett's explicit call)". Nothing else in the file changed (11+/10-, all in that comment + constant). Last commit touching the file is `5010273` (2026-09-07, H-2).
**Why:** Unknown first-hand — found in the 2026-10-03 audit. The code comment says Jett chose it because there is no refresh_token grant (jarvis-app `auth.ts` and the claude.ai connector only do authorization_code), so 24h expiry meant re-entering the PIN every ~24-36h. The 2026-09-18 security audit (vault `Daily/2026-09-18.md`) already listed "live uncommitted TTL 24h→30d" as a finding, so it was known but never logged here or committed.
**Is it live?** YES. `com.jettlynch.vault-mcp` runs `run-vault-mcp.sh` -> `uv run --project ~/code/obsidian-web-mcp vault-mcp`; the venv has an editable install (`_editable_impl_obsidian_web_mcp.pth` -> `~/code/obsidian-web-mcp/src`), and the running process (PID 1060) started 2026-10-03 10:13, after the edit. So the deployed server issues 30-day tokens from this uncommitted code. app-bridge also imports `obsidian_vault_mcp` from this same `src/` (token lookup).
**Security assessment: CONCERN (deliberate loosening, not a regression bug).** It weakens auth in one way: any stolen/leaked bearer token (vault and bridge-scoped) now stays valid up to 30 days instead of 24h, with no refresh-token rotation. It does NOT touch PKCE, PIN check, scope enforcement, the unscoped-token purge, or H-2 reuse detection. Still open from the 09-18 audit and made more relevant by the longer TTL: PKCE optional, no PIN lockout (brute force), no user-facing token revocation. Options for Jett: (a) accept and commit it as a documented decision; (b) add a refresh_token grant and go back to a short TTL; (c) middle ground 7 days.
**Files touched:** this log entry only. Code NOT committed, NOT reverted.
**Rollback:** `git checkout -- src/obsidian_vault_mcp/oauth.py` would discard the change and go back to 24h (then `launchctl kickstart -k gui/$UID/com.jettlynch.vault-mcp`) — NOT done; it is Jett's call. Removing this entry: delete this section.
**Verified:** git diff read in full; plist, launcher script, venv .pth and process start time read (read-only). **NOT verified:** that already-issued tokens carry a 30-day `expires_at` in the token store (store not read, it holds secrets); whether Jett still wants 30 days.

---

## 2026-10-03: BUILD_LOG.md created (RECONSTRUCTED from git history 2026-10-03, Why/Verified may be incomplete)

**What:** File did not exist (OPERATIONS.md served as the ops log). Created by the all-builds log audit (`~/work/Scripts/BUILD_LOG.md`). Entries below grouped from 20 commits (2026-03-17 to 2026-09-07).
**Why:** RULE #0 + capability index.
**Files touched:** `BUILD_LOG.md` (new). Not committed: working tree has uncommitted `src/obsidian_vault_mcp/oauth.py` and untracked `.env.bak`, `install_watchdog.sh`, `verify_mcp.sh`. Left alone (did not read `.env.bak`).
**Rollback:** `rm ~/code/obsidian-web-mcp/BUILD_LOG.md`.
**Verified:** git history only. The uncommitted `oauth.py` change is UNLOGGED and unreviewed; whoever made it should log it here.

## 2026-09-05 to 2026-09-07: C-2 scope split, semantic retrieval, OAuth discovery fixes, H-2 bridge token (RECONSTRUCTED)

**What:** `97a36b3` + `7980f0b` split read/write token scope (C-2) and close the static-token gap; `dad7fde` C-2 confirmed live. `2bb3970` manual port of semantic retrieval from prouds-mcp `1793ce5`, adapted to this single-tenant scope model. `a50fb91` fix X-Forwarded-* origin spoofing in OAuth discovery; `7a7caea`, `88d9c66` exempt `/.well-known/oauth-protected-resource[/mcp]` from bearer auth. `798f354` lifespan re-entry fix (init moved to `main()`). `5010273` mint bridge-scoped OAuth token alongside vault token (H-2). `eef0c57` docs: app-bridge moved to ~/code.
**Why:** jarvis-app security audit findings (C-2, H-2) and semantic search build.
**Files touched:** `oauth.py`, `auth.py`, `audit.py`, `token_scope.py`, `token_store.py`, `server.py`, `retrieval/*`, tests.
**Rollback:** `git revert` per commit.
**Verified:** Per commit bodies and OPERATIONS.md (C-2 confirmed live with a real write-scope token). Not re-run here.

## 2026-08-17 to 2026-08-30: vault-mcp audit backlog + two emergencies (RECONSTRUCTED)

**What:** `f363fbf` PIN gate on `/oauth/authorize`; `9c5fec4` remove unauthenticated fallback on app-build failure; `f0823f2` vault_move safety gate + audit-trail logging; `9991b11` enforce rate limits; `609559f` LOW items #4-#7; `99e542a` EMERGENCY `/oauth/register` client_secret leak + PIN-gate bypass; `eecc122` EMERGENCY ripgrep argv-injection RCE in vault_search; `dc90244` close redirect_uri open redirect, stop issuing the static token.
**Why:** 2026-08-17 audit backlog; upstream-divergence review found the two emergencies.
**Files touched:** `oauth.py`, `auth.py`, `tools/search.py`, `tools/manage.py`, `rate_limit.py`, `audit.py`, tests.
**Rollback:** do not revert (security fixes).
**Verified:** Regression tests added per commit. Not re-run here.

## 2026-03-17 to 2026-06-19: fork + folder scoping (RECONSTRUCTED)

**What:** `5eb1e88` upstream initial release (Jim Prosser). `6e3d1df` VAULT_SCOPE_ROOT folder-scoping + previously uncommitted tooling (vault_append, dry_run, edit/graph/markdown modules, vault_read_section).
**Why:** Avanti MCP folder-scoping A/B test (fixtures in `~/work/AvantiVault-TEST`) and Jett's own edit tooling.
**Files touched:** 20 files.
**Rollback:** n/a.
**Verified:** `tests/test_scope.py` added; not re-run.
