#!/usr/bin/env bash
set -uo pipefail
LOG_DIR="/shared/logs/Commentator"
mkdir -p "$LOG_DIR" /Users/deploydog-ai/LinkedIn/shared/comment_state
cd /app/scripts || exit 1
source /app/scripts/logging.sh
linkedin_log_init
set +e
STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
touch /shared/logs/${SERVICE_NAME:-Commentator}.cron.last

if [ "${SAFE_MODE:-0}" = "1" ]; then
  echo "[$STAMP] Commentator SAFE_MODE: py_compile only; no LinkedIn browsing/actions" | tee -a "$LOG_DIR/run.log"
  python3 -m py_compile linkedin_commentator.py 2>&1 | tee -a "$LOG_DIR/run.log"
  exit ${PIPESTATUS[0]}
fi

ARGS=(
  --max-items "${LINKEDIN_COMMENTATOR_MAX_ITEMS:-${LINKEDIN_COMMENT_MAX_ITEMS:-20}}"
  --max-posts "${LINKEDIN_COMMENTATOR_MAX_POSTS:-100}"
)
if [ "${LINKEDIN_COMMENTATOR_DRY_RUN:-${LINKEDIN_COMMENT_DRY_RUN:-1}}" = "1" ]; then ARGS+=(--dry-run); fi
if [ "${LINKEDIN_COMMENTATOR_SEND_TELEGRAM:-0}" = "1" ]; then ARGS+=(--send-telegram); fi
if [ "${LINKEDIN_COMMENTATOR_PUBLISH_APPROVED:-0}" = "1" ]; then ARGS+=(--publish-approved); fi
if [ "${LINKEDIN_COMMENTATOR_NO_DELAY:-0}" = "1" ]; then ARGS+=(--no-delay); fi

echo "[$STAMP] Commentator run: ${ARGS[*]}" | tee -a "$LOG_DIR/run.log"
python3 linkedin_commentator.py "${ARGS[@]}" 2>&1 | tee -a "$LOG_DIR/run.log"
code=${PIPESTATUS[0]}
if [ "$code" -eq 12 ]; then
  echo "[BLOCK] Commentator LinkedIn block/security/rate-limit detected; exiting 12" | tee -a "$LOG_DIR/run.log"
  exit 12
fi
if [ "$code" -ne 0 ]; then
  echo "[ERROR] Commentator run failed code=${code}" | tee -a "$LOG_DIR/run.log"
  exit "$code"
fi
echo "[DOD] Commentator finished" | tee -a "$LOG_DIR/run.log"
exit 0
