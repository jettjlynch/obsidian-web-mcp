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
| OAuth 2.0 PKCE with PIN gate + multi-audience tokens | Claude connector auth; mints vault-scoped and bridge-scoped tokens (used by app-bridge). Since 2026-10-03: PKCE S256 required, 7-day tokens with issued_at max-age, 5-strike 15-min PIN lockout (persisted, audit-logged). Branch merge/upstream-2026-10-04 adds: client_id+redirect_uri bound at exchange, /oauth/register 20/h brake, atomic 0600 state, RFC 9728 401 challenge | OAuth flow -> tokens | `oauth.py`, `token_store.py`, `config.OAUTH_ACCESS_TOKEN_TTL_SECONDS` | prod |
| Read/write token-scope split (C-2) | Read-only tokens cannot call write tools; enforced at the audit wrapper choke point | token -> scope | `token_scope.py`, `audit.py` | prod |
| Audit trail of every tool call | Single choke point wrapper logs every call | calls -> log | `audit.py` | prod |
| Rate limiting | RATE_LIMIT_READ/WRITE enforced | calls -> 429 | `rate_limit.py` | prod |
| Folder scoping | `VAULT_SCOPE_ROOT` makes a sub-folder the effective root, refuses escapes | env -> scoped instance | `config.py` `effective_vault_path()` | tested |
| Frontmatter index | In-memory YAML frontmatter index across the vault | md files -> query results | `frontmatter_index.py` | prod |
| Watchdog + Cloudflare tunnel ops | Health watchdog, tunnel setup, launchd plists | n/a | `mcp_watchdog.sh`, `scripts/setup-tunnel.sh`, `scripts/launchd/*` | prod |

---

## 2026-10-04 — Upstream merge (security + bugfix ports), branch merge/upstream-2026-10-04 — BUILT, NOT DEPLOYED

**What:** 12 commits on `merge/upstream-2026-10-04` (off main `d362627`), built in worktree `~/code/obsidian-web-mcp-merge`. Upstream jimprosser/obsidian-web-mcp had 50 commits not in main. Taken by cherry-pick or hand-port, not `git merge` (a real merge would bring in upstream's username/password login, which replaces the PIN gate, plus the rewritten audit log):
- From `security/upstream-2026-10-03`: #67 atomic writes keep umask/mode (`06ce447`), #79 refuse hardlinked files on reads (`c0e8615`), #81 test only (`c9275fa`), test fix-up (`5502ee4`).
- `9b7ca8e` = the date-JSON fix `a96e571` that is live but not yet committed on main, so this branch does not undo it.
- `51a2097` pin `mcp[cli]<2` (upstream 1667004). Without a uv.lock, a fresh install pulls mcp 2.x and fails to import FastMCP (reproduced).
- `b32e2b4` frontmatter index handles MOVED events (f7a7bd9). Atomic writes were never seen by the index or the retrieval listeners until a restart.
- `030e429` 401s carry an RFC 9728 `WWW-Authenticate` challenge (60d7a13), hand-ported into the pure-ASGI middleware via `config.advertised_base_url`.
- `73cea6e` OAuth: token exchange binds `client_id` (form or HTTP Basic) and requires `redirect_uri` (c385d41/e4924f0); `/oauth/register` limited to 20 an hour across all clients (register half of 5bdba5b); `_save_json` atomic and 0600 from creation (e975be2/4fa12c1 pattern).
- `37f2d34` `vault_write`/`vault_append` content written byte for byte (a4aa3b2).
- `57bf00e` ruamel frontmatter merge keeps formatting, and malformed YAML now aborts instead of dropping keys (2e2fc9e). **New dependency `ruamel.yaml`.**
**Skipped, with reasons:** already fixed locally: e4924f0 (99e542a/dc90244), a4cf931 (eecc122), 669775a (a50fb91), 13e147f (#28 by 798f354, #5 by a96e571), d04a5b4 (code already used `scope["path"]`). Conflicts with the PIN gate: upstream's username/password login and its login brake (the PIN lockout from 8df742d already covers that). Not applicable (the code they patch is not on this fork): bf947fd, 34b1e67, 47e428f, 3f794c4, 6baeec3, 4fa12c1 as written, 48fe780, 6362981, dfbe137, 1f6a46e, a29af70. Docs/CI/release: 9c8f5e6, 6ee879a, 1d60b04, e4ba873, 6aae450, d630c04, fb34a41. Features left out because they add public tools or settings, or touch search/read where the uncommitted data-layer work lives (Jett's call later): 0d3c396 serve at /, b3b3b9a allowed_hosts env, 19ea557 vault_edit, e975be2 (the persistence itself already exists here), 94cf229 UTF-8 output, a2541fc canvas, eb0bf8d daily, dd6af28 VAULT_MCP_PATH, 8046c34 heartbeat, b1da366/81b6140/23d8c7e extension seams, f72da0c upstream audit log (would replace audit.py), ff035c2 binary write, cd88fdf analytics, d86f22b signed upload, c74e395 filename search, 6e4c25d Cloudflare Access mode.
**Why:** close the upstream divergence. Fixes only; PIN gate, VAULT_SCOPE_ROOT, retrieval, audit.py and rate limits kept as they are.
**Files touched:** `pyproject.toml`, `src/obsidian_vault_mcp/{auth,oauth,models,frontmatter_index,frontmatter_io(new),vault,markdown}.py`, `tools/{search,read,write}.py`, `README.md`, tests (new: test_auth_www_authenticate, test_oauth_upstream_ports_20261004, test_write_verbatim, test_frontmatter_index_on_moved, test_frontmatter_io, test_frontmatter_preservation, test_frontmatter_dates, test_hardlink_reads, test_auth_encoded_path; edited: conftest, 3 OAuth tests now send client_id the way real clients do).
**Deploy (Jett, one line, after the data-layer owner has committed or stashed their work in the live checkout):** `cd ~/code/obsidian-web-mcp && test -z "$(git status --porcelain --untracked-files=no)" && git tag -f pre-upstream-merge-20261004 && git merge --no-edit merge/upstream-2026-10-04 && uv run --extra dev pytest -q && launchctl kickstart -k gui/$(id -u)/com.jettlynch.vault-mcp && sleep 6 && curl -fsS localhost:8420/health && ~/work/Scripts/cron-agent/security-watch.sh accept`
**Rollback:** `cd ~/code/obsidian-web-mcp && git reset --keep pre-upstream-merge-20261004 && launchctl kickstart -k gui/$(id -u)/com.jettlynch.vault-mcp && sleep 6 && curl -fsS localhost:8420/health`
**Verified:** worktree suite **364 passed** (main baseline 273; 304 after the folded-in commits). Red/green: the new on_moved, OAuth and verbatim tests fail on the old code (3, 9 and 5 failures) and pass on the new. Smoke run of the branch server on spare port 8466 with a temp vault and temp OAuth state, 12/12 PASS: health; unauthenticated /mcp gives 401 with the challenge; register; PIN+PKCE authorize; token without client_id gives 400; token with it gives 200; both state files 0600; MCP initialize; vault_write byte for byte; merge keeps quote style and yes/no; vault_read; vault_search; 21st registration in the hour gets 429. No tracebacks, port released, live 8420 still 200. jarvis-app checked: expo-auth-session sends client_id and redirect_uri at exchange.
**NOT verified:** a real claude.ai or jarvis-app re-auth against the new token checks (claude.ai should send client_id under client_secret_post; existing tokens are unaffected); the merge into the live checkout (it may conflict in BUILD_LOG.md/conftest.py/search.py/read.py once the data-layer work is committed; the date-fix hunks are identical on both sides); the first `uv run` after deploy downloading ruamel.yaml (needs network once); nothing pushed.

---

## 2026-10-03 — OAuth hardening: 7-day tokens, required PKCE S256, PIN lockout

**What:** Commit `8df742d` (deployed live). (1) Access-token TTL 30d -> **7 days**, one constant `config.OAUTH_ACCESS_TOKEN_TTL_SECONDS`; new tokens store `issued_at`, and both `oauth.py` and `token_store.py` (app-bridge's read path) reject tokens older than the TTL even if `expires_at` is later. (2) **PKCE required, S256 only**: `/oauth/authorize` (GET and POST) rejects a missing / non-S256 / absent-method / malformed `code_challenge` with an OAuth 2.1 error redirect (`error=invalid_request`, state echoed) before the PIN form is ever shown; `/oauth/token` always requires a well-formed, matching `code_verifier` (non-ASCII verifier now 400, not 500). The `code_challenge_method` default changed from "S256" to empty. (3) **PIN lockout**: 5 consecutive wrong PINs -> 15-minute lockout; while locked, every PIN POST (correct or not) gets 429 "Unable to verify right now. Try again later." Persisted 0600 in `oauth_pin_lockout.json` (gitignored), clamped on load to at most 15 min ahead (never permanent), corrupt file = clean start, reset on success. Each failure (`oauth_pin_failure`) and lockout (`oauth_pin_lockout`) is logged on the `obsidian_vault_mcp.audit` logger, no PIN values. PIN compare now on UTF-8 bytes (a non-ASCII PIN used to raise TypeError).
**Why:** Jett approved hardening 2026-10-03, following the uncommitted 30-day TTL found in the audit (entry below). 30-day bearer tokens with no refresh, optional PKCE, and an unlimited PIN guess rate on a public CT-logged hostname. The old 24h->30d diff is superseded by this commit.
**PKCE evidence before enforcing:** `~/Library/Logs/vault-mcp.log`, every `GET /oauth/authorize` with a query string, field presence only: 47 real requests (claude.ai DCR clients, 37; jarvis-app, 10) all sent `code_challenge` + `code_challenge_method=S256`. The only one without was an earlier localhost test (`example.invalid`). 3 query-less 400s are probes.
**Existing tokens:** the store had 2 live entries (vault + bridge, both write, about 14.5 days left, issued around 09-18 at 30d) with **no `issued_at`**, so they are NOT cut to 7 days. They expire naturally around **2026-10-18**, after which claude.ai / jarvis-app re-auth with the PIN and get 7-day tokens. Token values were not read.
**Files touched:** `src/obsidian_vault_mcp/{oauth.py,config.py,token_store.py}`, `.gitignore`, `tests/test_oauth_hardening.py` (new, 22 tests), `tests/conftest.py` (isolates the lockout file and counter), `tests/test_oauth_s1.py` + `tests/test_token_scope.py` (8 dummy `"x"` challenges replaced with a valid S256 challenge plus method). Backups of the exact pre-change live files (51c2808 + the 30d edit): `~/code/obsidian-web-mcp-backups/2026-10-03-pre-oauth-hardening/{oauth.py,config.py,token_store.py}`.
**Rollback:** `cd ~/code/obsidian-web-mcp && git revert --no-edit 8df742d && launchctl kickstart -k gui/$(id -u)/com.jettlynch.vault-mcp` (gives 24h tokens). For the exact old live state (30d), use instead: `cp ~/code/obsidian-web-mcp-backups/2026-10-03-pre-oauth-hardening/*.py src/obsidian_vault_mcp/` then the same kickstart. Then check `curl -s localhost:8420/health` returns 200. Delete `oauth_pin_lockout.json` if present.
**Verified:** suite before 249 passed, after **271 passed** (TDD: 22 new tests red first, then green). Live restart PID 1045 -> 60596, `/health` 200 within about 2s. Locally AND via `https://vault-mcp.wzdmai.com`: `/health` 200; `/.well-known/oauth-authorization-server` 200 (`code_challenge_methods_supported: ["S256"]`, correct issuer); `/.well-known/oauth-protected-resource/mcp` 200; `/oauth/authorize` with no PKCE -> 302 `error=invalid_request`; `method=plain` -> 302 `error=invalid_request`; S256 -> 200 PIN form. Unauthenticated `/mcp` 401. claude.ai's existing token kept working after restart (`POST /mcp` 200s from 160.79.106.x in the access log). The tests never wrote the real lockout file.
**NOT verified:** a real end-to-end login (no PIN entered, by design); the lockout live (unit tests only, so no real attempts were burned); app-bridge was not restarted, so its `token_store.py` max-age check takes effect on its next restart (it is stricter only for new tokens). The SQLite `ProgrammingError` tracebacks in `vault-mcp-error.log` predate this change (8,303 occurrences historically) and were not investigated.

---

## 2026-10-03 — Found uncommitted oauth.py change (unlogged)

**Update (same day):** resolved. Superseded by the 7-day hardening above (commit `8df742d`).


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
