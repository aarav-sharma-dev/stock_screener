#!/usr/bin/env bash
# Read new #vli-research-notes posts and DM a short crowding note.
#
# Crontab (after CRON_TZ=America/Chicago):
#   0 8 * * *   /home/asharma/bin/vli-research-notes-check.sh
#   30 12 * * * /home/asharma/bin/vli-research-notes-check.sh
#   0 17 * * *  /home/asharma/bin/vli-research-notes-check.sh
#
# Needs SLACK_BOT_TOKEN (sourced from the first-alert env) and @skynet
# in #vli-research-notes.

set -euo pipefail

export PATH="/usr/bin:/bin:/usr/local/bin:${HOME}/bin:${HOME}/.local/bin:${PATH}"

ROOT="${SCREENER_ROOT:-$HOME/dev/stock_screener}"
ENV_FILE="${HOME}/.config/first-alert-triage/env"
STATE_DIR="${HOME}/.local/state/vli-research-notes"
LOCK="${STATE_DIR}/run.lock"
LOG="${STATE_DIR}/run.log"

mkdir -p "${STATE_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
fi

exec 9>"${LOCK}"
if ! flock -n 9; then
  echo "$(date -Is) skip: another run holds lock" >>"${LOG}"
  exit 0
fi

if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

{
  echo "===== $(date -Is) ====="
  "${PY}" "${ROOT}/scripts/vli_research_notes_check.py" "$@"
  echo
} >>"${LOG}" 2>&1
