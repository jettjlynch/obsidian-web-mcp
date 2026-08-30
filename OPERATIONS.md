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
