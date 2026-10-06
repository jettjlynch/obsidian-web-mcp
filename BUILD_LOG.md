# BUILD_LOG: obsidian-web-mcp (Jett's personal vault-mcp)

Jett's own remote MCP server over the JJ Second Brain vault (live as `com.jettlynch.vault-mcp`, reached by claude.ai via cloudflared -> vault-mcp.wzdmai.com). Forked from jimprosser/obsidian-web-mcp. Newest entry first. Each entry: What / Why / Files touched / Rollback / Verified.

First-hand operational history already lives in `OPERATIONS.md` (outages, tunnel migration, security work, to 2026-09-07). This file adds the build-level view and the reusable-capabilities section. Capability index: `~/work/WZDM/BUILD_INDEX.md`. For new client deployments prefer `~/code/wzdm-vault-template`.

---

## 2026-10-06 22:25 — vault-mcp "hang": slow read tools blocked the event loop; moved them to worker threads (agent vault-mcp-fix)

- **What:** new `off_loop` decorator in `server.py`, applied between `@mcp.tool` and `@audited` on the 10 read-only tools (vault_read, vault_batch_read, vault_search, vault_search_frontmatter, vault_search_semantic, vault_list, vault_read_section, vault_links, vault_backlinks, vault_tags). It turns each into an async tool that runs the sync body via `anyio.to_thread.run_sync`. Write tools are unchanged: they stay on the loop and stay serialised. `tests/test_token_scope.py` got a `_call()` helper that awaits coroutine results. LaunchAgent restarted.
- **Why (root cause):** FastMCP runs sync tools directly on the asyncio event loop. `vault_search_semantic` (4-7 s, Python-side search; Ollama itself answers in ~30 ms warm) and `vault_tags` (~7 s full walk) block every other request, /health and `initialize` included. Measured before the fix: /health took 2.8 s behind one semantic call. A cloud routine firing several of these at once queued them serially past claude.ai's 30 s limit, so the connector timed out. cloudflared logged bursts of "context canceled" at 21:13 and 21:17 UTC, and the server logged `ClientDisconnect` at 12:03. The Ollama embed lines were NOT the cause: about 3 calls per 15 min from the file watcher, all in its own thread, not a re-index loop. The server never actually died; the watchdog had no DOWN events today.
- **Files:** `src/obsidian_vault_mcp/server.py` (backup `server.py.bak-20261006`), `tests/test_token_scope.py` (backup `tests/test_token_scope.py.bak-20261006`). No config, index data, cloudflared or Shaa server touched.
- **Rollback:** `cp src/obsidian_vault_mcp/server.py.bak-20261006 src/obsidian_vault_mcp/server.py && cp tests/test_token_scope.py.bak-20261006 tests/test_token_scope.py && launchctl kickstart -k gui/$(id -u)/com.jettlynch.vault-mcp` (or `git revert` the commit).
- **Verified:** 287/287 pytest pass. After the restart, with 3x `vault_search_semantic` plus `vault_tags` in flight (4-5 s each): /health 0.02 s, `initialize` 0.02 s, `vault_list` 0.04 s. Before the fix, /health was 2.8 s behind ONE call. Tunnel `vault_list` 0.2-0.3 s. The claude.ai Obsidian connector (`vault_list`, `vault_search_semantic`) worked end to end with correct results. The audit log still records scope and duration, which proves contextvars reach the worker thread. No new errors in the log.
- **NOT verified:** a real cloud-routine run after the fix (the coordinator should re-run the scout). The 4-7 s semantic latency itself is not fixed, only isolated; a follow-up should profile `retrieval/query.py` and the datalayer re-rank path.

## 2026-10-04 ~10:15 — tests leaked into the LIVE data-layer recall log; fixed (session data-research)

- **What:** added autouse fixture `_isolate_datalayer_flag` in `tests/conftest.py` (sets `DATALAYER_RERANK_VAULT_MCP=0`; `test_datalayer.py` still sets its own env per test, which wins).
- **Why:** with the real `~/work/Scripts/data-layer/flags.json` at vault_mcp=true, every read in test_tools/test_vault etc. ran `datalayer.log_recall()` against the live `data-layer/state/recalls.jsonl`. 84 of its 86 lines were fixture paths, and the usage-decay signal was mostly test noise. This is the same class of bug as the 2026-08-30 oauth_clients.json leak.
- **Files:** `tests/conftest.py` (backup `tests/conftest.py.bak-20261004-recallleak`). No src change, no restart.
- **Rollback:** restore the backup (this re-opens the leak).
- **Verified:** the old conftest reproduced the leak (+14 lines in one pytest run). With the new one: +0 lines, 287/287 pass. Log cleaned on the data-layer side (see `~/work/Scripts/data-research/BUILD_LOG.md`).


## Reusable capabilities

Derived from module docstrings under `src/obsidian_vault_mcp/` and `scripts/` (2026-10-03).

| Capability | What it does | Inputs / outputs | Entry point | Maturity |
|---|---|---|---|---|
| Remote vault MCP (read/write/edit/search/graph tools) | Streamable-HTTP MCP over a markdown+frontmatter vault; read-before-write surgical edits; dry_run diffs | MCP tool calls | `server.py`, `tools/{read,write,edit,search,graph,manage}.py`, `vault.py` | prod (live) |
| Semantic retrieval (hybrid) | Heading-aware chunker, sqlite-vec store, pluggable embeddings (Ollama nomic-embed-text default, Voyage optional), incremental content-hash reindex, confidence cutoff + diversification | md files -> chunks/vectors; query -> ranked chunks | `retrieval/{chunker,store,embeddings,indexer,query}.py`, tool `vault_search_semantic`, `scripts/reindex_vault.py`, `scripts/verify_retrieval_quality.py` | tested (off unless `RETRIEVAL_ENABLED`; live status: check OPERATIONS.md) |
| OAuth 2.0 PKCE with PIN gate + multi-audience tokens | Claude connector auth; mints vault-scoped and bridge-scoped tokens (used by app-bridge). Since 2026-10-03: PKCE S256 required, 7-day tokens with issued_at max-age, 5-strike 15-min PIN lockout (persisted, audit-logged) | OAuth flow -> tokens | `oauth.py`, `token_store.py`, `config.OAUTH_ACCESS_TOKEN_TTL_SECONDS` | prod |
| Read/write token-scope split (C-2) | Read-only tokens cannot call write tools; enforced at the audit wrapper choke point | token -> scope | `token_scope.py`, `audit.py` | prod |
| Audit trail of every tool call | Single choke point wrapper logs every call | calls -> log | `audit.py` | prod |
| Rate limiting | RATE_LIMIT_READ/WRITE enforced | calls -> 429 | `rate_limit.py` | prod |
| Folder scoping | `VAULT_SCOPE_ROOT` makes a sub-folder the effective root, refuses escapes | env -> scoped instance | `config.py` `effective_vault_path()` | tested |
| Frontmatter index | In-memory YAML frontmatter index across the vault | md files -> query results | `frontmatter_index.py` | prod |
| Watchdog + Cloudflare tunnel ops | Health watchdog (RETIRED 2026-10-03, selfheal owns health restarts now), tunnel setup, launchd plists | n/a | `mcp_watchdog.sh` (kept, unloaded), `scripts/setup-tunnel.sh`, `scripts/launchd/*` | tunnel prod / watchdog retired |
| Thread-safe sqlite store pattern | One shared sqlite3 connection (`check_same_thread=False`) with every method serialized by an RLock decorator (`_locked`), safe for watcher-thread writers plus request-thread readers | n/a | `retrieval/store.py` | prod |

---

## 2026-10-03 23:40-23:58 — FIX: retrieval listener SQLite thread crash loop (commit b8cf3d2, deployed)

**What:** `RetrievalStore` now opens its connection with `check_same_thread=False` and serializes every method through a `threading.RLock` (`_locked` decorator). New regression test `tests/test_retrieval_store_threading.py` (cross-thread diff/apply/query/delete, plus 4 writers and 2 readers concurrently). Committed on `main` (repo practice: direct commits) as **b8cf3d2**, containing ONLY `retrieval/store.py` and the new test. The data-layer WIP below and this BUILD_LOG stay uncommitted (not mine to commit).
**Why (root cause):** `server.py` builds the store on the main thread at startup, but `frontmatter_index._flush_pending` runs on a `threading.Timer` thread and calls the retrieval change listener, which runs `store.diff/apply`. sqlite3's default `check_same_thread=True` raised `ProgrammingError: SQLite objects created in a thread can only be used in that same thread` on EVERY listener call: 9,044 times in total, 559 on 2026-10-03, the latest at 23:41. Impact: **no edited note had been re-indexed for semantic search** (the DB file mtime was 6 Sep 18:33, the original backfill). It also produced a 391 MB, unrotated `~/Library/Logs/vault-mcp-error.log`.
**Files touched:** `src/obsidian_vault_mcp/retrieval/store.py`, `tests/test_retrieval_store_threading.py` (new). Backups: `~/code/obsidian-web-mcp-backups/2026-10-03-sqlite-thread-fix/{store.py,BUILD_LOG.md}.bak-20261003`. Old error log: `~/Library/Logs/vault-mcp-error.log.20261003-pre-sqlite-fix.gz` (6.6 MB, kept as evidence).
**Deploy:** ONE restart, `launchctl kickstart -k gui/501/com.jettlynch.vault-mcp` at 23:47:42 (new pid 37765). This also loaded the uncommitted data-layer hook below (its flag is off, so a no-op). That closes its PENDING restart.
**Rollback:** `git revert b8cf3d2` (or `cp` the backup store.py back), then `launchctl kickstart -k gui/$(id -u)/com.jettlynch.vault-mcp`. Expect the ProgrammingError flood to return.
**Verified:** the new tests failed first with the exact live error, then passed; full suite **280 passed**. Live: `/health` 200 after 8s. Public `https://vault-mcp.wzdmai.com/health` 200, OAuth metadata 200, unauthenticated `POST /mcp` 401 (auth still enforced). Zero `ProgrammingError` since the restart. The listener really writes now: `Daily/2026-10-03.md` went from 0 to 130 chunks, plus `Memory/Claude Interaction Notes.md` and 2 transcripts. The retrieval.sqlite mtime moved to 23:48-23:50, its first write since 6 Sep.
**NOT verified:** a throwaway probe note (`zz-vault-mcp-listener-probe.md`, created then deleted) was not indexed within ~3 min. The flush was still working through the backlog of other changed files serially (Ollama embeds), and the 5s debounce resets on every vault event. This is a pre-existing latency trait, not this bug. The vault has a month of un-indexed edits: consider a one-off `scripts/reindex_vault.py` to catch up, since the incremental path only fires on new edits.

---

## 2026-10-03 — data-layer P3 re-rank hook (flag OFF; loaded live 23:47:42 by the sqlite-fix restart)

**What:** New `src/obsidian_vault_mcp/datalayer.py`, an optional hook into Jett's data-layer ranking library (`DATALAYER_DIR`, default `~/work/Scripts/data-layer`: `rerank.py` + `recall.py`, stdlib, outside this repo). It is gated by that build's `flags.json` key `vault_mcp` (currently **false**); env `DATALAYER_RERANK_VAULT_MCP` overrides. The flag is read on every call. With the flag on: `vault_search_semantic` reorders results by type x usage-decay (score = 1 - distance) and adds `pinned_core` (notes with `pinned: true`); `vault_search` reorders ripgrep matches by their file's weight (rg order is filesystem order anyway) and adds `pinned_core`; `vault_read`/`vault_batch_read` (content reads only, successful only) append to `data-layer/state/recalls.jsonl`. With the flag off, the library missing, or any error, behaviour is exactly as before.
**Confidence:** `no_good_answer` is computed in `retrieval/query.py` from raw distances before the hook runs, and each result keeps its raw `distance`. The hook only reorders (tested).
**Why:** data-layer P3 (`~/work/Scripts/data-layer/HANDOFF-2026-10-03-data-layer.md`). The full entry is in `~/work/Scripts/BUILD_LOG.md` ("data-layer: P3 re-rank layer over qmd").
**Files touched:** `src/obsidian_vault_mcp/datalayer.py` (new), `tools/search.py`, `tools/read.py`, `tests/test_datalayer.py` (new, 6 tests). Backups: `~/code/obsidian-web-mcp-backups/2026-10-03-pre-datalayer/{search.py,read.py,BUILD_LOG.md}.bak-20261003-2317-datalayer`. **Uncommitted** on top of bc06e59.
**Rollback:** behaviour only, no restart needed once the code is loaded: `/opt/homebrew/bin/python3 ~/work/Scripts/data-layer/rerank.py flag vault_mcp off`. Code: `git checkout -- src/obsidian_vault_mcp/tools/search.py src/obsidian_vault_mcp/tools/read.py && rm src/obsidian_vault_mcp/datalayer.py tests/test_datalayer.py`, then `launchctl kickstart -k gui/$(id -u)/com.jettlynch.vault-mcp` if it had been restarted onto the new code.
**DONE 23:47:42 (by the sqlite-fix session, entry above): restart happened, /health 200.** Original note: load the code with `launchctl kickstart -k gui/$(id -u)/com.jettlynch.vault-mcp`, then check `curl -s localhost:8420/health` returns 200. With the flag off this changes nothing beyond one flags.json read per search/read. Not done here because a restart interrupts claude.ai remote access. Note: the watchdog restarting the server would also load it (with the flag off, still a no-op).
**Update 2026-10-04 01:40 (dl-p3):** the data-layer eval PASSED (recall@10 median general 0.380 vs 0.329 qmd_alone, track1b 0.250 vs 0.200, 5 runs). With the flag on, `vault_search_semantic` fetches `max_results x datalayer.DEPTH_FACTOR` (3) before re-ranking, then cuts back. The live process (started 23:47:42) runs this code, since files were last modified 23:25:25. On the overnight coordinator's rule the `vault_mcp` flag stays **OFF**: it is on the Jett actions list in `~/work/Scripts/BUILD_LOG.md`. To enable: `/opt/homebrew/bin/python3 ~/work/Scripts/data-layer/rerank.py flag vault_mcp on` (read per call, no restart); rollback is the same command with `off`.
**Verified:** suite 271 -> **277 passed**. Flag off: raw order, no `pinned_core` key. Flag on: decision outranks reference, raw distances and `no_good_answer` are unchanged, an empty result stays `no_good_answer: true`, and recall is logged only on successful content reads. A missing DATALAYER_DIR means off. The tests never touch the real flags.json or recall log.
**NOT verified:** live behaviour (not restarted); performance under load (one flags.json read plus an mtime-cached frontmatter stat per result per call).

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
