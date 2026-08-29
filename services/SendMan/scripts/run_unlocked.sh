#!/usr/bin/env bash
set -uo pipefail
LOG_DIR="/shared/logs/SendMan"
mkdir -p "$LOG_DIR"
cd /app/scripts || exit 1
source /app/scripts/logging.sh
linkedin_log_init
# logging.sh enables `set -e`; this wrapper intentionally handles non-zero
# worker exits (notably 12 = LinkedIn safeguard) and decides whether to retry.
set +e
if [ "${SAFE_MODE:-0}" = "1" ]; then
  STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "[$STAMP] SendMan SAFE_MODE self-test: no LinkedIn actions" | tee -a "$LOG_DIR/run.log"
  python3 linkedin_message_outreach.py --self-test --no-delay 2>&1 | tee -a "$LOG_DIR/run.log"
  exit ${PIPESTATUS[0]}
fi
TARGET="${LINKEDIN_MESSAGE_MAX_MESSAGES:-60}"
MAX_PER_JOB="${LINKEDIN_MESSAGE_MAX_PER_JOB:-20}"
MAX_PAGES="${LINKEDIN_MESSAGE_MAX_PAGES:-0}"
JOB="${LINKEDIN_MESSAGE_JOB:-all}"
RETRY_WAIT_SECONDS="${LINKEDIN_MESSAGE_BLOCK_RETRY_SECONDS:-720}"
MAX_BLOCK_RETRIES="${LINKEDIN_MESSAGE_MAX_BLOCK_RETRIES:-0}"  # 0 = retry until target or hard DOD

sent_today() {
  python3 - <<'PY'
import json, os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
state_dir = Path(os.environ.get('LINKEDIN_MESSAGE_STATE_DIR', '/Users/deploydog-ai/LinkedIn/shared/message_state'))
now = datetime.now(ZoneInfo(os.environ.get('TZ', 'America/Argentina/Buenos_Aires')))
log = state_dir / 'logs' / f'linkedin_message_outreach_actions_{now.isocalendar().year}-W{now.isocalendar().week:02d}.jsonl'
count = 0
if log.exists():
    for line in log.read_text(errors='ignore').splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get('event') == 'sent' and str(rec.get('at', '')).startswith(now.strftime('%Y-%m-%d')):
            count += 1
print(count)
PY
}

status_stop_reason() {
  python3 - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ.get('LINKEDIN_MESSAGE_STATUS_PATH', '/Users/deploydog-ai/LinkedIn/shared/message_state/linkedin_message_outreach_status.json'))
try:
    print(json.loads(p.read_text()).get('stop_reason') or '')
except Exception:
    print('')
PY
}

block_retries=0
while :; do
  already_sent="$(sent_today)"
  remaining=$(( TARGET - already_sent ))
  if [ "$remaining" -le 0 ]; then
    echo "[DOD] SendMan target reached: sent_today=${already_sent}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 0
  fi

  ARGS=(--max-messages "$remaining" --max-per-job "$MAX_PER_JOB" --max-pages "$MAX_PAGES" --job "$JOB")
  if [ "${LINKEDIN_MESSAGE_DRY_RUN:-0}" = "1" ]; then ARGS+=(--dry-run); fi
  echo "[RUN] SendMan remaining=${remaining} sent_today=${already_sent}/${TARGET} job=${JOB}" | tee -a "$LOG_DIR/run.log"
  python3 linkedin_message_outreach.py "${ARGS[@]}" 2>&1 | tee -a "$LOG_DIR/run.log"
  code=${PIPESTATUS[0]}

  already_sent_after="$(sent_today)"
  if [ "$already_sent_after" -ge "$TARGET" ]; then
    echo "[DOD] SendMan target reached after run: sent_today=${already_sent_after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 0
  fi

  reason="$(status_stop_reason)"
  if [ "$reason" = "inmail_credits_exhausted" ]; then
    echo "[DOD] SendMan hard platform limit: ${reason}; sent_today=${already_sent_after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 0
  fi

  if [ "$code" -eq 12 ]; then
    block_retries=$(( block_retries + 1 ))
    if [ "$MAX_BLOCK_RETRIES" -gt 0 ] && [ "$block_retries" -gt "$MAX_BLOCK_RETRIES" ]; then
      echo "[BLOCK] SendMan max block retries exceeded: reason=${reason} sent_today=${already_sent_after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
      exit 12
    fi
    echo "[BLOCK] SendMan LinkedIn block/safeguard: reason=${reason:-unknown}; waiting ${RETRY_WAIT_SECONDS}s before retry ${block_retries}" | tee -a "$LOG_DIR/run.log"
    sleep "$RETRY_WAIT_SECONDS"
    continue
  fi

  if [ "$code" -ne 0 ]; then
    echo "[ERROR] SendMan run failed code=${code}; sent_today=${already_sent_after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit "$code"
  fi

  # Normal completion before target means search/candidate exhaustion for this run.
  # Retry once after a short pause because LinkedIn search/feed can be virtualized/stale.
  if [ "$already_sent_after" -eq "$already_sent" ]; then
    echo "[DOD] SendMan no additional sendable candidates found; sent_today=${already_sent_after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 0
  fi
  echo "[CONTINUE] SendMan made progress but target not reached; retrying remaining messages" | tee -a "$LOG_DIR/run.log"
  sleep 30
done
