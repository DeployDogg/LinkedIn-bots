#!/usr/bin/env bash
set -euo pipefail
mkdir -p /app/logs /shared/logs /Users/deploydog-ai/LinkedIn/shared/legacy_state /Users/deploydog-ai/LinkedIn/shared/message_state /Users/deploydog-ai/Downloads/RESUME
source /app/scripts/logging.sh
linkedin_log_init
if [ "${CENTRAL_SCHEDULER_MODE:-0}" = "1" ]; then
  STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "[$STAMP] ${SERVICE_NAME:-service}: starting supercronic in CENTRAL scheduler idle mode" | tee -a /shared/logs/container-start.log
  exec /usr/local/bin/supercronic /app/crontab.central
fi
if [ "${CRON_TEST_MODE:-0}" = "1" ]; then
  STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "[$STAMP] ${SERVICE_NAME:-service}: starting supercronic in TEST mode" | tee -a /shared/logs/container-start.log
  exec /usr/local/bin/supercronic /app/crontab.test
fi
STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
echo "[$STAMP] ${SERVICE_NAME:-service}: starting supercronic in PRODUCTION mode" | tee -a /shared/logs/container-start.log
exec /usr/local/bin/supercronic /app/crontab
