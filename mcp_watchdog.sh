#!/bin/bash
#
# mcp_watchdog.sh — active health probe for the vault-mcp server.
#
# WHY THIS EXISTS:
#   The launchd job (com.jettlynch.vault-mcp) has KeepAlive=true, but KeepAlive
#   only restarts on process EXIT. The known failure mode (anyio.ClosedResourceError
#   in the stateless streamable-HTTP transport) leaves the process ALIVE while every
#   authenticated POST /mcp returns HTTP 500. KeepAlive never sees it. This watchdog
#   probes /health and kickstarts the service when it stops returning 200.
#
# 2026-09-05: switched from an authenticated MCP `initialize` round trip to the
# real /health route (server.py's `health` handler -- previously listed as
# auth-exempt but never actually implemented, a separate gap closed the same
# day). Found while auditing C-2 (the read/write token-scope split): this
# watchdog was a hidden, automated consumer of the legacy static
# VAULT_MCP_TOKEN -- removing that token's acceptance to close C-2 for real
# would have made every probe 401 and, worse, made THIS SCRIPT immediately
# kickstart the service on the very first failed probe (see the restart logic
# below), restart-thrashing for ~15-20 minutes before giving up into a
# permanent false "CRITICAL, not resolved by restart" state. /health needs no
# credential at all, so this watchdog no longer depends on that token existing,
# and this file has nothing left to fix when the static token is finally
# retired for good (H-2/S2).
#
# Portable: all paths derive from $HOME. No hardcoded user.

set -uo pipefail

PROJECT_DIR="$HOME/code/obsidian-web-mcp"
ENV_FILE="$PROJECT_DIR/.env"
SERVICE_LABEL="com.jettlynch.vault-mcp"
LOG="$HOME/Library/Logs/vault-mcp-watchdog.log"
STATE="$HOME/Library/Logs/.vault-mcp-watchdog.state"   # last known state: up|down
FAILCOUNT="$HOME/Library/Logs/.vault-mcp-watchdog.failcount"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*" >>"$LOG"; }

# --- load config (port only -- no token needed for /health) ---
if [[ ! -f "$ENV_FILE" ]]; then
  log "CRITICAL: .env not found at $ENV_FILE — cannot probe"
  exit 1
fi
set -a; . "$ENV_FILE"; set +a
PORT="${VAULT_MCP_PORT:-8420}"
URL="http://127.0.0.1:${PORT}/health"

# --- one unauthenticated liveness probe; echoes only the HTTP status code ---
probe() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL" 2>/dev/null
}

CODE="$(probe)"
PREV="$(cat "$STATE" 2>/dev/null || echo up)"

if [[ "$CODE" == "200" ]]; then
  echo up >"$STATE"
  echo 0 >"$FAILCOUNT"
  # only log on recovery, to keep the log quiet during normal operation
  [[ "$PREV" == "down" ]] && log "RECOVERED: /health -> 200 (was down)"
  exit 0
fi

# --- unhealthy ---
FAILS="$(cat "$FAILCOUNT" 2>/dev/null || echo 0)"
FAILS=$((FAILS + 1))
echo "$FAILS" >"$FAILCOUNT"
echo down >"$STATE"

LASTERR="$(tail -1 "$HOME/Library/Logs/vault-mcp-error.log" 2>/dev/null)"
log "DOWN: /health -> HTTP ${CODE:-<none>} (consecutive failures: $FAILS). last error-log line: ${LASTERR:0:120}"

# Escalate instead of thrashing if restarts aren't helping.
if (( FAILS >= 4 )); then
  log "CRITICAL: $FAILS consecutive failed probes despite restarts — root cause not resolved by restart. NOT restarting again this cycle. Investigate mcp lib / ClosedResourceError."
  exit 1
fi

log "ACTION: kickstart -k gui/$(id -u)/${SERVICE_LABEL}"
if launchctl kickstart -k "gui/$(id -u)/${SERVICE_LABEL}" 2>>"$LOG"; then
  log "restart issued; waiting for recovery..."
else
  log "ERROR: kickstart command failed"
  exit 1
fi

# bounded wait for recovery (~16s max)
for _ in 1 2 3 4 5 6 7 8; do
  sleep 2
  if [[ "$(probe)" == "200" ]]; then
    echo up >"$STATE"; echo 0 >"$FAILCOUNT"
    log "RESTART_OK: server healthy again (/health -> 200)"
    exit 0
  fi
done

log "RESTART_FAILED: still not 200 after restart + ~16s. Will retry next cycle."
exit 1
