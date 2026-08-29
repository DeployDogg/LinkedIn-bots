#!/usr/bin/env bash
set -uo pipefail
LOG_DIR="/shared/logs/PostLiker"
mkdir -p "$LOG_DIR"
cd /app/scripts || exit 1
source /app/scripts/logging.sh
linkedin_log_init
# logging.sh enables `set -e`; this wrapper intentionally handles non-zero
# action exits so DOD/block logic below can run.
set +e
if [ "${SAFE_MODE:-0}" = "1" ]; then
  STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "[$STAMP] PostLiker SAFE_MODE: no LinkedIn actions" | tee -a "$LOG_DIR/run.log"
  python3 -m py_compile linkedin_like_posts.py | tee -a "$LOG_DIR/run.log"
  exit ${PIPESTATUS[0]}
fi
TARGET="${LINKEDIN_LIKE_MAX:-5}"
MAX_ATTEMPTS="${LINKEDIN_LIKE_MAX_ATTEMPTS:-0}"  # 0 = keep trying until target or platform block
ATTEMPT_WAIT_SECONDS="${LINKEDIN_LIKE_ATTEMPT_WAIT_SECONDS:-30}"

verified_today() {
  python3 - <<'PY'
import json, os
from datetime import datetime, timezone
from pathlib import Path
base = Path(os.environ.get('LINKEDIN_LIKE_OUT_DIR', '/Users/deploydog-ai/LinkedIn/shared/legacy_state/liked_posts'))
today = datetime.now(timezone.utc).strftime('%Y%m%d')
count = 0
if base.exists():
    for run_dir in base.glob(f'{today}_*'):
        if not run_dir.is_dir():
            continue
        status_path = run_dir / 'status.json'
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text())
                count += sum(1 for item in (data.get('verification') or []) if item.get('verified'))
                continue
            except Exception:
                pass
        # Backward compatibility for old runs before per-run status.json existed.
        count += len(list(run_dir.glob('verify_*_liked.png')))
print(count)
PY
}

attempt=0
while :; do
  current="$(verified_today)"
  remaining=$(( TARGET - current ))
  if [ "$remaining" -le 0 ]; then
    echo "[DOD] PostLiker target reached: verified_today=${current}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 0
  fi
  attempt=$(( attempt + 1 ))
  if [ "$MAX_ATTEMPTS" -gt 0 ] && [ "$attempt" -gt "$MAX_ATTEMPTS" ]; then
    echo "[ERROR] PostLiker max attempts exceeded: verified_today=${current}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 14
  fi
  echo "[RUN] PostLiker remaining=${remaining} verified_today=${current}/${TARGET} attempt=${attempt}" | tee -a "$LOG_DIR/run.log"
  LINKEDIN_LIKE_HEADLESS="${LINKEDIN_LIKE_HEADLESS:-1}" python3 linkedin_like_posts.py --max-likes "$remaining" 2>&1 | tee -a "$LOG_DIR/run.log"
  code=${PIPESTATUS[0]}
  after="$(verified_today)"
  if [ "$after" -ge "$TARGET" ]; then
    echo "[DOD] PostLiker target reached after run: verified_today=${after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 0
  fi
  if [ "$code" -eq 12 ]; then
    echo "[BLOCK] PostLiker LinkedIn block detected; verified_today=${after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit 12
  fi
  if [ "$code" -ne 0 ]; then
    echo "[ERROR] PostLiker run failed code=${code}; verified_today=${after}/${TARGET}" | tee -a "$LOG_DIR/run.log"
    exit "$code"
  fi
  echo "[CONTINUE] PostLiker target not reached yet: verified_today=${after}/${TARGET}; retrying in ${ATTEMPT_WAIT_SECONDS}s" | tee -a "$LOG_DIR/run.log"
  sleep "$ATTEMPT_WAIT_SECONDS"
done
