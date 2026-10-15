#!/usr/bin/env bash
# Cron entrypoint for stock-screener alerts (deploy to ~/bin/stock-screener-alert.sh).
#
# Intended crontab (America/Chicago):
#   CRON_TZ=America/Chicago
#   */30 6-18 * * 1-5  /home/asharma/bin/stock-screener-alert.sh
#   0 19 * * 1-5       /home/asharma/bin/stock-screener-alert.sh
#
# Behavior:
#   06:00 CT (Mon–Fri)     → every mode on the full universe (S&P 500 + extras)
#   other */30 06–18 CT    → athdip again on the full universe (S&P 500 + extras)
#   19:00 CT               → same full screen again (EOD)
#
# Env (optional):
#   SCREENER_ROOT          repo path (default: auto-detect)
#   SCREENER_FORCE_FULL=1  force full screen this run
#   SCREENER_FORCE_SCHWAB=1  force a full-universe athdip scan this run

set -euo pipefail

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*"; }

detect_root() {
  if [[ -n "${SCREENER_ROOT:-}" && -d "$SCREENER_ROOT" ]]; then
    echo "$SCREENER_ROOT"
    return
  fi
  for cand in \
    "$HOME/dev/stock_screener" \
    "$HOME/dev/stock-screener" \
    "$HOME/workspace/stock-screener" \
    "$HOME/workspace/stock_screener" \
    "$HOME/Projects/stock-screener" \
    "$HOME/stock-screener"
  do
    if [[ -d "$cand" && -f "$cand/scripts/schwab_athdip_watch.py" ]]; then
      echo "$cand"
      return
    fi
  done
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$here/schwab_athdip_watch.py" ]]; then
    echo "$(cd "$here/.." && pwd)"
    return
  fi
  if [[ -f "$here/../scripts/schwab_athdip_watch.py" ]]; then
    echo "$(cd "$here/.." && pwd)"
    return
  fi
  echo ""
}

ROOT="$(detect_root)"
if [[ -z "$ROOT" ]]; then
  log "ERROR: stock-screener repo not found. Set SCREENER_ROOT."
  exit 1
fi

cd "$ROOT"
log "ROOT=$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.venv/bin/python3" ]]; then
  PY="$ROOT/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

export PYTHONUNBUFFERED=1

# Load KEY=value from .env without `source` (unquoted values with spaces break source).
load_dotenv() {
  local f="$1" line key val
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%"${line##*[![:space:]]}"}"
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # strip one layer of matching quotes
    if [[ "$val" == \"*\" && "$val" == *\" ]]; then
      val="${val:1:${#val}-2}"
    elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
      val="${val:1:${#val}-2}"
    fi
    export "${key}=${val}"
  done < "$f"
}
load_dotenv "$ROOT/.env"

mkdir -p "$ROOT/output" "$ROOT/logs"
LOG="$ROOT/logs/alert-$(date +%Y%m%d).log"
# A full-universe scan can still be running when the next */30 slot fires.
exec 9>"$ROOT/logs/alert.lock"
if ! flock -n 9; then
  log "Another scan is still running; skipping this slot"
  exit 0
fi

HOUR="$(TZ=America/Chicago date +%-H 2>/dev/null || TZ=America/Chicago date +%H | sed 's/^0//')"
MINUTE="$(TZ=America/Chicago date +%-M 2>/dev/null || TZ=America/Chicago date +%M | sed 's/^0//')"
FORCE_FULL="${SCREENER_FORCE_FULL:-0}"
FORCE_SCHWAB="${SCREENER_FORCE_SCHWAB:-0}"

run_full_screen() {
  log "MODE=full screen (Schwab prices, Yahoo backup) + dashboard"
  if [[ -x "$ROOT/.venv/bin/stock-screener" ]]; then
    SCREENER_BIN="$ROOT/.venv/bin/stock-screener"
  elif command -v stock-screener >/dev/null 2>&1; then
    SCREENER_BIN="stock-screener"
  else
    SCREENER_BIN="$PY -m stock_screener"
  fi

  # No --limit: support, catalyst, and athdip all see the S&P 500 plus extras.
  $SCREENER_BIN --mode all --universe sp500 --top 25 \
    --allow-below-200 --no-reddit --no-grok --no-trump-x \
    --price-source schwab \
    --output output/screen.csv \
    2>&1 | tee -a "$LOG" || log "WARN: screener exited $?"
  $PY scripts/build_screen_dashboard.py 2>&1 | tee -a "$LOG" || log "WARN: dashboard exited $?"
  log "Full screen done"
}

run_wide_athdip() {
  log "MODE=athdip full universe (S&P 500 + AI, chips, energy, nuclear, ETFs)"
  if [[ -x "$ROOT/.venv/bin/stock-screener" ]]; then
    SCREENER_BIN="$ROOT/.venv/bin/stock-screener"
  elif command -v stock-screener >/dev/null 2>&1; then
    SCREENER_BIN="stock-screener"
  else
    SCREENER_BIN="$PY -m stock_screener"
  fi
  # No --limit: the whole S&P 500, plus theme extras in the code.
  $SCREENER_BIN --mode athdip --universe sp500 --top 50 \
    --price-source schwab --no-news \
    --output output/screen_athdip.csv \
    2>&1 | tee -a "$LOG" || log "WARN: wide athdip exited $?"
}

run_schwab_watch() {
  # Every half hour rescans the full athdip universe, then alerts only names
  # that were not already in the snapshot. The setup watcher is not the scan.
  run_wide_athdip
  $PY scripts/alert_new_signals.py --no-desktop \
    2>&1 | tee -a "$LOG" || log "WARN: new-signal alert exited $?"
  log "Full-universe athdip done"
}

# Decide mode
MODE="schwab"
if [[ "$FORCE_SCHWAB" == "1" ]]; then
  MODE="schwab"
elif [[ "$FORCE_FULL" == "1" ]]; then
  MODE="full"
elif [[ "$HOUR" -eq 19 ]]; then
  MODE="full"
elif [[ "$HOUR" -eq 6 && "$MINUTE" -lt 15 ]]; then
  # First cron slot of the day (~06:00): refresh Yahoo universe / focus
  MODE="full"
else
  MODE="schwab"
fi

{
  log "==== stock-screener-alert start (hour=$HOUR minute=$MINUTE mode=$MODE) ===="
  if [[ "$MODE" == "full" ]]; then
    run_full_screen
  else
    run_schwab_watch
  fi
  log "==== stock-screener-alert end ===="
} 2>&1 | tee -a "$LOG"
