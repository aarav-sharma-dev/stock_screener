#!/usr/bin/env bash
# Weekly Schwab refresh-token keepalive (deploy to ~/bin/schwab-refresh.sh).
#
# Intended crontab (America/Chicago), Sunday 08:15, before the Monday screen:
#   CRON_TZ=America/Chicago
#   15 8 * * 0  /home/asharma/bin/schwab-refresh.sh
#
# This calls the refresh-token grant. It cannot recover a dead login;
# that still needs scripts/schwab_auth.py on this machine.

set -euo pipefail

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*"; }

ROOT="${SCREENER_ROOT:-$HOME/dev/stock_screener}"
if [[ ! -f "$ROOT/scripts/schwab_refresh.py" ]]; then
  log "ERROR: $ROOT/scripts/schwab_refresh.py not found. Set SCREENER_ROOT."
  exit 1
fi

cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/schwab-refresh.log"

{
  log "==== schwab refresh start ===="
  "$PY" scripts/schwab_refresh.py
  log "==== schwab refresh end ===="
} 2>&1 | tee -a "$LOG"
