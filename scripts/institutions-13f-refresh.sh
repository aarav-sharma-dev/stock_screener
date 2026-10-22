#!/usr/bin/env bash
# Refresh curated 13F holdings the first weekday after each filing deadline.
#
# Deadlines are Feb 14, May 15, Aug 14, and Nov 14, rolled to Monday when
# they fall on a weekend. This script no-ops on every other day.
#
# Intended crontab (America/Chicago), weekdays at 07:15:
#   CRON_TZ=America/Chicago
#   15 7 * * 1-5  /home/asharma/bin/institutions-13f-refresh.sh

set -euo pipefail

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*"; }

export TZ=America/Chicago
ROOT="${SCREENER_ROOT:-$HOME/dev/stock_screener}"
if [[ ! -f "$ROOT/src/stock_screener/data/institutions_history.py" ]]; then
  log "ERROR: 13F history module not found under $ROOT. Set SCREENER_ROOT."
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
LOG="$LOG_DIR/institutions-13f-refresh.log"

{
  log "==== 13F refresh check ===="
  "$PY" src/stock_screener/data/institutions_history.py --if-deadline 8
  log "==== 13F refresh check end ===="
} 2>&1 | tee -a "$LOG"
