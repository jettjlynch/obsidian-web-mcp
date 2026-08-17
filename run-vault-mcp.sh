#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
# launchd doesn't set $HOME, so it must be exported explicitly here -- but
# derive it from the current user rather than hardcoding a literal path.
# Audit backlog #5: a hardcoded "/Users/jettlynchmini" here contradicted the
# project's own portability goal (see install_watchdog.sh's "portable to
# Avanti's Mac Mini" comment) -- same "copied script, wrong context" bug
# class that hit prouds-mcp for real. Fixed 2026-08-17.
export HOME="/Users/$(id -un)"

PROJECT_DIR="$HOME/code/obsidian-web-mcp"
cd "$PROJECT_DIR"

set -a
. "$PROJECT_DIR/.env"
set +a

exec /opt/homebrew/bin/uv run --project "$PROJECT_DIR" vault-mcp
