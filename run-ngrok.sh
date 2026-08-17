#!/bin/zsh
# run-ngrok.sh — always-on ngrok tunnel for the vault-mcp server.
# Tunnels the Mac Mini's vault-mcp (localhost:8420) to the reserved ngrok
# domain so the claude.ai Obsidian connector reaches THIS machine's vault.
# Authtoken is read from ~/Library/Application Support/ngrok/ngrok.yml.
#
# 2026-07-13 fix: was pointed at 27123, which is Obsidian's own Local REST
# API plugin port (127.0.0.1:27123/27124) -- vault-mcp doesn't use that
# plugin at all (reads the vault straight off disk), but its .env had
# VAULT_MCP_PORT overridden to the same number, so ngrok silently tunneled
# straight into Obsidian's plugin instead of vault-mcp's OAuth layer. Moved
# vault-mcp back to its code default (8420) and pointed the tunnel there.
# See .env's VAULT_MCP_PORT comment -- never reuse 27123/27124 here.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
# launchd doesn't set $HOME, so it must be exported explicitly here -- but
# derive it from the current user rather than hardcoding a literal path.
# Audit backlog #5: a hardcoded "/Users/jettlynchmini" here contradicted the
# project's own portability goal (see install_watchdog.sh's "portable to
# Avanti's Mac Mini" comment) -- same "copied script, wrong context" bug
# class that hit prouds-mcp for real. Fixed 2026-08-17.
export HOME="/Users/$(id -un)"

exec /opt/homebrew/bin/ngrok http \
  --url=https://gigabyte-widget-elevating.ngrok-free.dev \
  8420 \
  --log=stdout --log-format=logfmt
