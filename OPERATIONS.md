# Operations Log

Infra fixes and incidents for the live vault-mcp + ngrok deployment. Not dev docs (see README.md for those) — this is the history of what broke and what changed on the running Mac Mini services.

---

## 2026-07-13 — OAuth tunnel outage: port collision with Obsidian's own plugin

**Symptom:** claude.ai's Obsidian connector failed to reconnect with "couldn't register with sign-in service."

**Root cause:** `run-ngrok.sh` tunneled `https://gigabyte-widget-elevating.ngrok-free.dev` → `localhost:27123`. vault-mcp's `.env` had `VAULT_MCP_PORT=27123` (overriding its own code default of `8420`) — but `127.0.0.1:27123` is Obsidian's own Local REST API plugin's default HTTP port (it also holds `127.0.0.1:27124` for HTTPS), which was already bound there. Obsidian's bind is interface-specific (`127.0.0.1`); vault-mcp's was wildcard (`*:27123`). On macOS, a specific-interface bind always wins over a wildcard bind for matching traffic, so every request to `localhost:27123` — including ngrok's — landed on Obsidian's raw plugin, not vault-mcp. vault-mcp itself was never crashed; it had a stable 6-day uptime the entire time, just structurally unreachable through that port. (vault-mcp doesn't use the Obsidian plugin at all — it reads the vault straight off disk via `VAULT_PATH`; the plugin is unrelated infrastructure that happened to squat on the same port number.)

**Confirmed before fix:**
- `.well-known/oauth-authorization-server` and `.well-known/oauth-protected-resource` via the tunnel both returned Obsidian's raw plugin error (`{"message": "Authorization required. Find your API Key in the 'Local REST API' section of your Obsidian settings.", "errorCode": 40101}`), not OAuth metadata.
- `lsof -nP -iTCP:27123 -sTCP:LISTEN` showed both `Obsidian` (127.0.0.1:27123) and `Python`/vault-mcp (`*:27123`) bound simultaneously.
- `curl localhost:8420` (vault-mcp's actual code default) got no response at all — nothing was listening there.

**Fix applied:**
1. `.env`: `VAULT_MCP_PORT` changed `27123` → `8420` (its own code default; confirmed genuinely free via `lsof -nP -iTCP:8420 -sTCP:LISTEN` before use). Added a guard comment directly above the variable: 27123/27124 are permanently reserved by Obsidian's plugin on this machine and must never be reused, with the exact `lsof` check to run before picking a port in future.
2. `run-ngrok.sh`: target port `27123` → `8420`, with a comment explaining why (this incident).
3. Swept the project for other stale references to `27123`: `mcp_watchdog.sh` sources `.env` fresh every run so it was already correct in practice, but its unset-fallback default (`${VAULT_MCP_PORT:-27123}`) was updated to `8420` so a future missing-key case doesn't silently probe the wrong port. `verify_mcp.sh` (manual diagnostic script) had `27123` hardcoded despite already sourcing `.env` — fixed to `${VAULT_MCP_PORT:-8420}`, matching the watchdog's pattern.
4. Restarted in order: `launchctl kickstart -k gui/$UID/com.jettlynch.vault-mcp` first, confirmed it came up listening on `*:8420` and served real OAuth metadata locally (`curl localhost:8420/.well-known/oauth-authorization-server` → `200`, correct JSON) — *then* `launchctl kickstart -k gui/$UID/com.jettlynch.ngrok-vault`, confirmed via `localhost:4040/api/tunnels` that it now forwards to `localhost:8420`.

**Verified after fix:**
- `curl https://gigabyte-widget-elevating.ngrok-free.dev/.well-known/oauth-authorization-server` → **200**, real metadata with `issuer` correctly resolved to the public ngrok URL (not localhost) — `authorization_endpoint`/`token_endpoint`/`registration_endpoint` all present.
- `lsof -nP -iTCP:27123 -sTCP:LISTEN` now shows only Obsidian's plugin — no more second listener on that port.

**Not fixed, flagged separately (was not part of this fix's scope):** `curl .../.well-known/oauth-protected-resource` through the tunnel now returns vault-mcp's own `401 {"error": "Missing or malformed Authorization header"}` — proof the routing fix works (this is vault-mcp's auth middleware responding, not Obsidian's plugin), but this route was never actually implemented: `oauth.py` only registers `/.well-known/oauth-authorization-server`, and `auth.py`'s `_AUTH_EXEMPT_PATHS` allowlist doesn't include `/.well-known/oauth-protected-resource` either — so any request to it is blocked by the bearer-auth middleware before Starlette's router even sees it (masking what would otherwise be a 404 as a 401). Whether claude.ai's connector actually needs this endpoint for registration to succeed is unconfirmed; if the connector still fails to register after this port fix, this is the next thing to check. Left alone since it wasn't part of the diagnosed port-collision fix and is a real code change (new route + exempt-path entry), not a config change.

**Backup:** `.env.bak` (pre-edit copy) left in place alongside `.env` as a rollback artifact.

---

## 2026-07-14 — ngrok → Cloudflare Tunnel migration + security hardening

### Migration (completes the TODO open since 2026-05-22)

Jett put **wzdmai.com** onto Cloudflare's nameservers, unblocking `cloudflared tunnel login`. Then:

1. `scripts/setup-tunnel.sh` with `VAULT_MCP_HOSTNAME=vault-mcp.wzdmai.com` → tunnel `vault-mcp` (id `d4b3cd93-2b87-4fb6-ae28-bf65ce527b5d`), config at `~/.cloudflared/config-vault-mcp.yml`, DNS CNAME auto-added.
2. `vault-mcp.wzdmai.com` added to `allowed_hosts` in `server.py`; ngrok entries removed after cutover.
3. New LaunchAgent **`com.jettlynch.cloudflared-vault`** (RunAtLoad/KeepAlive/ThrottleInterval=30, logs to `~/Library/Logs/cloudflared-vault*.log`).
4. claude.ai connector re-pointed. **Gotcha that cost two failed reconnects:** the connector URL must be `https://vault-mcp.wzdmai.com/mcp` — WITH the `/mcp` path. The bare domain 404s ("no MCP server was found at the provided URL"). Root-level requests only serve the OAuth discovery endpoints.
5. ngrok retired: `com.jettlynch.ngrok-vault` booted out and plist deleted. Zero ngrok processes verified afterward. `run-ngrok.sh` left in the repo as historical reference only.

**DNS gotcha during cutover:** Tailscale's "Use Tailscale DNS" silently overrides manually-set DNS servers on macOS (`scutil --dns` showed `100.100.100.100` as global resolver #1), and it — plus the building's resolver — had negative-cached the hostname from before the record existed. Fixed with `tailscale set --accept-dns=false` on the Mac Mini (tradeoff: `.ts.net` names no longer resolve on this machine; nothing currently depends on that).

### Fix: missing RFC 9728 endpoint (the "flagged, not fixed" item from 2026-07-13)

`/.well-known/oauth-protected-resource` (and `/mcp`-suffixed variant) now have a real handler in `oauth.py` returning `{resource, authorization_servers}`, and both paths are in `auth.py`'s `_AUTH_EXEMPT_PATHS`. This endpoint turned out to be REQUIRED: claude.ai does full resource discovery when connecting to a new hostname, and the 401 it hit here was a hard connector failure. It only ever "worked" under ngrok because the long-lived connection never re-triggered discovery.

### Hardening: bind moved 0.0.0.0 → 127.0.0.1 (`server.py`)

`lsof` showed vault-mcp listening on `*:8420` — reachable from the whole LAN, and this machine sits on a shared-building ISP network (`customer.ask4.lan`). `allowed_hosts` does not mitigate direct-to-port access (it's a Host-header check; trivially forged). The only legitimate non-local caller is cloudflared, which dials `localhost:8420` from the same machine. Bound to `127.0.0.1`; tunnel verified still 200 end-to-end; watchdog unaffected (it probes 127.0.0.1 already).

### Security audit findings (same day, post-migration)

- ✅ Tunnel credentials (`~/.cloudflared/*.json` 400, `cert.pem` 600, dir 700).
- ✅ DNS-rebinding allowlist tight: localhost variants + `vault-mcp.wzdmai.com` only.
- ✅ Watchdog tunnel-agnostic (probes 127.0.0.1:8420 with real authed `initialize`).
- ✅ **CLOSED (staged: 2026-08-17, 2026-08-18, 2026-08-30) — auto-approve OAuth chain mints the real bearer token for ANY caller.** Was: `/oauth/register` handed `VAULT_OAUTH_CLIENT_SECRET` to anyone (201); `/oauth/authorize` auto-approved any client_id/redirect_uri; `/oauth/token` validated only the caller's own PKCE pair. Fixed in three passes, see "2026-08-30 — S1: OAuth lockdown" below for the full closure and current migration state.
- ⚠️ **OPEN — no HTTP→HTTPS redirect at the Cloudflare edge.** `http://vault-mcp.wzdmai.com/...` serves 200 over cleartext. All known clients use https, but enable "Always Use HTTPS" in the Cloudflare dashboard (Rules → or SSL/TLS → Edge Certificates) — dashboard action, not scriptable from here.

### Downstream impact discovered during audit

**jarvis-app's `OBSIDIAN_MCP_BASE_URL` (src/lib/config.ts) pointed at the retired ngrok domain** — the phone app's PKCE flow was hard-dead the moment ngrok stopped (the domain now 404s at ngrok's edge). Updated to `https://vault-mcp.wzdmai.com` same day. Bonus: this permanently removes the ngrok free-tier "Visit Site" interstitial that was silently breaking on-device sign-in (jarvis-app BUILD_LOG 2026-07-02 + 2026-07-14). Requires rebuild + re-sideload of the app. Correction to a claim made mid-session: the phone's OAuth was NOT on a separate tunnel — it shared this exact ngrok tunnel; only app-bridge (port 8421, Tailscale-only, no public tunnel) is separate.

### 2026-08-30 — S1: OAuth lockdown (closing the rest of C-1/M-1)

Full history of the auto-approve chain, three passes:

1. **2026-08-17** — `/oauth/authorize` gated behind `VAULT_OAUTH_AUTHORIZE_PIN`. Closed the no-consent auto-approve, but NOT redirect_uri validation or the shared static token.
2. **2026-08-18 (EMERGENCY)** — `/oauth/register` stopped returning the real `VAULT_OAUTH_CLIENT_SECRET`; now generates a per-registration secret. Closed a full bypass of the PIN gate via `grant_type=client_credentials`.
3. **2026-08-30 (S1, today)** — the two remaining gaps SECURITY.md/PLAN.md still listed as open:
   - **redirect_uri was still unvalidated** (open redirect, M-1) even behind the PIN — a PIN-holder could be steered into approving a code sent to an attacker's URI. Fixed: `/oauth/authorize` now checks `client_id` + `redirect_uri` against a real exact-match allowlist *before ever showing the PIN form* (fails fast, re-checked again after POST since hidden form fields are attacker-influenceable). Rejection is a `400` JSON error, never a redirect to the unvalidated URI.
   - **every successful flow still got the one shared static `VAULT_MCP_TOKEN`.** Fixed: the authorization_code grant now issues a fresh per-client random token (`secrets.token_urlsafe(32)`), 24h real expiry (not indefinite — see the trade-off note in `oauth.py` on why not a shorter SHOULD-tier TTL: jarvis-app has no refresh_token grant, so expiry means re-entering the PIN).

**Migration state — what's still on the static token, and why:** `auth.py`'s bearer middleware now accepts EITHER the legacy static `VAULT_MCP_TOKEN` OR a freshly-issued per-client token. The static token is not yet retired because:
- Anything already holding it from before today (jarvis-app's existing iOS Keychain entry, until it next re-authenticates) needs to keep working without an abrupt break.
- `client_credentials` grant (a separate code path, not part of C-1/M-1, not touched by S1) still hands back the static token to whoever has the real `VAULT_OAUTH_CLIENT_SECRET` out-of-band.
- The app-bridge (`server.py`, separate process, port 8421) independently compares against this same static token value — it never goes through this server's OAuth flow at all, so retiring the static token here has no effect on it either way.

Full retirement of the static token (distinct per-service tokens, rotation + reuse-detection per RFC 9700 §4.14) is S2 (H-2), not this task.

**New client registry:** `/oauth/register` now persists each dynamic registration's declared `redirect_uris` (`oauth_clients.json`, 0600, gitignored — RFC 7591-style, this is what makes claude.ai's MCP connector's own callback URL validate correctly without hardcoding it here). jarvis-app never calls `/oauth/register` (its own `auth.ts` goes straight to `/oauth/authorize` with a hardcoded `client_id`), so it's pre-registered at server startup from `VAULT_OAUTH_REDIRECT_URIS` (config.py; default `jarvisapp://oauth/callback`, verified against expo-auth-session's actual `makeRedirectUri` output for the app's `jarvisapp` scheme, not guessed).

**Not verified this pass:** an actual on-device PKCE flow from jarvis-app against the live server (would need a fresh build + re-sideload + a real approval with Jett's PIN). Verified instead via a full regression suite (`tests/test_oauth_s1.py`, 7 new tests) exercising the same request shapes jarvis-app's `auth.ts` and claude.ai's connector actually send, plus the existing 115-test suite with zero regressions. Recommend a real on-device sign-in as the first thing to try after this deploys.

**Cloudflare WAF rate-limit rule on `/oauth/*`** (PLAN.md S1 item 5): dashboard-only action, no API token available in this environment to script it — not done, flagging for Jett. Suggested rule: path `vault-mcp.wzdmai.com/oauth/*`, rate-limit e.g. 10 requests/minute per IP, action Block or Managed Challenge.

### 2026-09-05 — C-2: read/write token-scope split

S1 closed *how* a token gets minted; it did nothing about *what one grants*. Before this pass, any token that authenticated at all — static or per-client — could call every tool, including `vault_write`/`append`/`delete`/`move`/`batch_frontmatter_update`. No scope concept existed anywhere in the codebase.

**What changed:**
- Issued tokens (`oauth_tokens.json`) now carry a `"scope"`: `"read"` or `"write"`.
- `/oauth/authorize`'s consent page gained a write-access checkbox, **unchecked by default**. A client may hint `?scope=write` in the authorize URL to pre-check it (pure UX — claude.ai's connector, say, won't have to make Jett hunt for it), but only what's actually submitted on the form, approved with the correct PIN, is granted. No default silently becomes write.
- The token response (`/oauth/token`) now includes `"scope"` so clients know what they got.
- Enforcement sits at the same choke point rate-limiting already uses — audit.py's `audited()` decorator — and for the identical reason rate_limit.py documents: auth.py's bearer middleware is pure ASGI and never parses the JSON-RPC body, so it can determine a token's granted scope but not which tool is about to be called. auth.py now threads the granted scope through a contextvar (`token_scope.py`) that audit.py reads once it knows both the tool and its declared `kind`. **Deliberately did not implement this as literal logic inside auth.py's middleware** — doing so would require buffering/parsing the request body there, reintroducing the exact `anyio.ClosedResourceError` this middleware was rewritten to avoid (see auth.py's own module docstring).
- write-class tools now require exactly `"write"` scope; read-class tools accept either (write implies read). A request with no determinable scope (shouldn't happen in production — only reachable if a tool is invoked outside the normal auth.py→audit.py path, e.g. directly in a test) fails closed on both.
- Scope-denied calls log a distinct audit event (`"scope_denied": true`, `"granted_scope": ...`), not folded into the generic rate-limit/error markers.

**Migration decision — existing per-client tokens (issued by S1, 2026-08-30, through today) are NOT grandfathered into write access.** They predate the scope concept entirely (no `"scope"` key). Silently granting them write would be exactly the kind of implicit full-access grant this task exists to close. Instead, `_purge_unscoped_tokens()` runs once at server startup and deletes any token record missing a `"scope"` key outright. Practical effect: **the next request from any already-issued token 401s**, and the client transparently re-runs the existing PKCE + PIN flow to get a real, scoped token. Cost is bounded — tokens are already TTL'd at 24h, so this is at most one unscheduled re-auth, not a standing outage. The legacy **static** `VAULT_MCP_TOKEN` is unaffected by any of this — it's grandfathered to `"write"` unconditionally (see `oauth.get_token_scope`'s docstring); scoping or retiring it is H-2/S2's job, explicitly out of scope for this task.

**Flagging for Jett — real product impact, not just plumbing:** claude.ai's Obsidian connector is (as far as this session could determine) the actual way you ask Claude to edit vault notes day to day. Its connector does not send a `scope` parameter today, so under the new default it will only ever get a **read-only** token unless you tick the write checkbox on the PIN screen the next time it re-authenticates (which will happen automatically once its current token gets purged by the migration above). **If claude.ai's vault-editing stops working after this deploys, this is why — go through its next sign-in prompt and check the write box.** Separately: `jarvis-app`'s own `auth.ts` sends no `scope` param either, so its tokens also default to read-only now; checked its PLAN.md/BUILD_LOG usage and found no feature that currently calls this server's write-class tools directly with that token (its writes to `metrics.db`/goals/cooking go through app-bridge's own process, which uses the separate static-token path, untouched by this change) — but `auth.ts`'s own doc-comment claiming the token is "valid for both obsidian-web-mcp and the app-bridge (they share one static token)" was already stale before this change (S1 stopped returning the static token from this grant on 2026-08-30) and is more stale now; worth a pass over jarvis-app's auth.ts comments next time that repo is touched, not done here (out of this repo's scope).

**Verified:** full test suite green — `tests/test_token_scope.py` (22 new tests: the audit.py gate against fake tools, every real registered tool in server.py driven off its own `audit_kind` annotation rather than a hand-copied list, consent-page checkbox behavior, issuance defaults, migration purge, static-token grandfather) plus the pre-existing 122 tests, zero regressions (144 passed, 1 skipped — the pre-existing rg-gated test, `rg` still not installed here). **Not verified:** an actual on-device/claude.ai re-authentication against the live server (would need this deployed first, then a real re-auth to observe).

### 2026-09-05 — C-2 follow-up: the scope split was live-ineffective; closing it for real

**What happened:** before this pass was even deployed, a write from claude.ai's connector succeeded against the (already-restarted) new code — without the connector having gone through a fresh OAuth reconnect. Investigation (full trace, not inference) found: this server's own `oauth_tokens.json` per-client token store had never held a single entry — `_issue_token()` had never succeeded even once since S1 landed 2026-08-30 — and the last OAuth `authorization_code` flow completion in this server's entire retained log history was **2026-08-17, three weeks before S1 even existed**. Claude.ai's connector had been authenticating this whole time with the token that flow returned back then: the raw static `VAULT_MCP_TOKEN`. `get_token_scope()` grandfathered that token to unconditional `"write"` (a deliberate decision at the time, framed as H-2/S2's territory) — so every real write sailed straight past the new scope gate as `"write"`, completely independent of the consent-page checkbox. The enforcement mechanism itself was never broken or bypassed in code (checked line by line: the static-token branch fed into the exact same `token_scope` contextvar → `audit.py` gate as any other token) — the gap was that the client which actually matters was never going through the gated path to begin with. **Net effect at the time: C-2 provided zero practical protection for real traffic.**

**Consumer check, done before touching anything (same discipline as the original C-2 pass):** before removing the static token's authority against this server, checked what else authenticates against it that way. Found two real dependents beyond claude.ai:
- **`mcp_watchdog.sh`** — automated, launchd-scheduled every 300s, and on ANY non-200 probe it immediately runs `launchctl kickstart -k` to restart the live service. It was doing a full authenticated MCP `initialize` round trip using the static token specifically because `/health` — listed in `auth.py`'s `_AUTH_EXEMPT_PATHS` from the start — had never actually been implemented (a separate, real gap, closed the same pass; see below). Removing the static token blind would have made every probe 401 and, worse, made this script restart-thrash the live service for ~15-20 minutes before settling into a permanent false `CRITICAL, not resolved by restart` state.
- The OAuth **`client_credentials`** grant — a separate, never-advertised code path (`oauth_metadata()`'s `grant_types_supported` has only ever listed `authorization_code`) that handed back the same raw static token. Checked its actual usage first: the *only* trace of it in this server's entire retained log history is one **failed** attempt (2026-08-18 08:16:44), tied to that day's register-leak/PIN-gate-bypass incident — zero successful uses, ever. No legitimate client (jarvis-app's `auth.ts`, claude.ai's registered connector) uses it; both go through `authorization_code` exclusively.
- `verify_mcp.sh` (manual diagnostic, run by hand) also depends on the static token and was **not** updated this pass — lower stakes since it's not automated and whoever runs it will see the 401 immediately and understand why from this section.

**What changed, in order:**
1. **Mounted a real `/health` route** (`server.py`) — genuinely unauthenticated (already exempt in `auth.py`, just never implemented; any request there had 404'd since this server existed). Minimal liveness signal only: `{"status": "ok", "index_loaded": <bool>}` — no vault content (paths, filenames, counts), no auth state. `FrontmatterIndex` gained a public `is_ready` property for this rather than reaching into its private `_observer` attribute from outside the class.
2. **`mcp_watchdog.sh` now probes `/health`** with no `Authorization` header at all — no longer depends on `VAULT_MCP_TOKEN` existing or authenticating. Stale log-message wording ("authed initialize -> ...") updated to match.
3. **`client_credentials` grant removed outright** — `oauth_token()` now returns a clear `400 unsupported_grant_type` with an explanatory `error_description` for that grant type, rather than silently issuing a token (the static one) that would 401 on first real use once step 4 landed. `_handle_client_credentials()` deleted entirely, not left as unreachable dead code holding a live secret.
4. **`get_token_scope()` no longer recognizes the static token at all.** This is the actual fix. `auth.py`'s now-obsolete "Server misconfigured: no auth token set" 500 guard (specifically about `VAULT_MCP_TOKEN` being unset, meaningless once that value stops gating auth) was removed, and `server.py`'s matching stale startup warning was replaced with a comment explaining why. `config.VAULT_MCP_TOKEN` itself was **left in `config.py`** — the `.env` key still needs to exist for app-bridge's independent sourcing of it (see below); only this server's own use of it as a credential was removed.

**Scope of this fix, stated plainly (per the task's own framing):** this is a **narrow slice of H-2/S2**, not the full bind-fix-and-secret-split. It only revokes the static token's authority *against obsidian-web-mcp*. Verified, not assumed: **app-bridge (port 8421, separate codebase entirely, moved 2026-09-06 from `~/Documents/Scripts/app-bridge` to `~/code/app-bridge`) independently compares incoming requests against its own copy of this same secret value and never goes through this server's OAuth/auth code at all** (confirmed via `run-app-bridge.sh`'s sourcing of this repo's `.env` and this repo's own prior trace in the 2026-08-30 section above) — completely untouched by anything in this pass. The secret itself is unretired, unrotated, and still lets app-bridge authenticate its own separate incoming requests. Full retirement (distinct per-service tokens, rotation + reuse-detection per RFC 9700 §4.14, and the bridge's 0.0.0.0 bind fix) is still S2/H-2/H-1, still open.

**User-facing sequence when the live service picks this up (restart deliberately withheld again — same pattern as before, reported here first):**
1. claude.ai's connector's current session token (the static token) 401s on its very next request.
2. The connector's own OAuth client re-triggers the standard flow: browser redirect → `/oauth/authorize`.
3. Jett sees the PIN-entry consent page — same as always, now also showing the write-access checkbox (unchecked by default).
4. Jett ticks the write checkbox, enters the PIN, approves.
5. `/oauth/token` issues a fresh per-client token with `scope: "write"` — this is the first time a real `"write"`-scoped per-client token will have ever existed in `oauth_tokens.json`.
6. Vault writes from claude.ai now flow through the actual scoped path C-2 built, for real, for the first time.

**Verified:** full suite green, 147 passed / 1 pre-existing skip (144 prior + 3 new `/health` tests; two pre-existing tests updated in place to assert the new, intentional behavior — a static token being *rejected*, not accepted — see `tests/test_oauth_s1.py` and `tests/test_token_scope.py`; `tests/test_oauth_register_leak.py`'s `client_credentials`-adjacent tests updated for the grant's removal). A live background server for a genuine end-to-end `curl /health` was attempted but declined by this session's sandbox; the `/health` route is proven via three `Starlette TestClient`-level tests instead (no-auth 200, exact response shape, index-readiness reflection). Real `curl`-by-hand confirmation, and the actual claude.ai reconnect sequence above, both wait on the live restart Jett will trigger.

**Residual staleness spotted, not fixed (out of this pass's scope):** `README.md`'s env-var table still documents `VAULT_MCP_TOKEN` as "256-bit bearer token for authenticating MCP requests" — true for app-bridge's independent use of the same `.env` value, no longer true for this server's own behavior. Worth a pass next time this repo's docs are touched.

### 2026-09-06 — C-2 confirmed live end-to-end: real write-scoped token issued

The user-facing sequence predicted at the end of the prior section actually happened, in order, exactly as predicted. Verified directly (file read, not inferred from logs or assumed from the reconnect having been requested):

- `oauth_tokens.json` — which had never held a single entry since S1 (2026-08-30) — now contains one real per-client token: `client_id: vault-mcp-b521c8239d7714a1`, `scope: "write"`, `expires_at: 1788806907.725138` (2026-09-07 19:48 local).
- This is the first `"write"`-scoped per-client token to ever exist in this store. Its presence confirms the checkbox was ticked, the PIN was entered correctly, `/oauth/authorize` → `/oauth/token` completed, and the token that came back carries exactly the scope the user chose — not a silent grandfather to `"write"` (the pass-1 bug) and not a rejected/expired attempt.

This closes the gap left open at the end of the prior pass: pass 2 proved the *old* (unscoped, static-token) path was dead; this confirms the *new* (scoped, per-client) path is alive and does what it's supposed to. C-2 has no remaining open sub-question as of this entry — only H-2's broader secret-retirement work (unrelated to scoping) and the still-outstanding Cloudflare WAF rate-limit item remain in this finding's neighborhood.
