#!/usr/bin/env bash
set -uo pipefail
LOG_DIR="/shared/logs/ConnectMan"
mkdir -p "$LOG_DIR"
cd /app/scripts || exit 1
source /app/scripts/logging.sh
linkedin_log_init
if [ "${SAFE_MODE:-0}" = "1" ]; then
  STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "[$STAMP] ConnectMan SAFE_MODE: no LinkedIn actions" | tee -a "$LOG_DIR/run.log"
  python3 -m py_compile linkedin_outreach.py | tee -a "$LOG_DIR/run.log"
  exit ${PIPESTATUS[0]}
fi
LINKEDIN_MAX_CONNECTS="${LINKEDIN_MAX_CONNECTS:-60}" LINKEDIN_MAX_PAGES="${LINKEDIN_MAX_PAGES:-30}" python3 linkedin_outreach.py 2>&1 | tee -a "$LOG_DIR/run.log"
exit ${PIPESTATUS[0]}
