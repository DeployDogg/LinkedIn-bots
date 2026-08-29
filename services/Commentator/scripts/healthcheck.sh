#!/usr/bin/env bash
set -euo pipefail

SERVICE="${SERVICE_NAME:-service}"
STAMP="/shared/logs/${SERVICE}.cron.last"
SUPERCRONIC_PATTERN="${SUPERCRONIC_PATTERN:-[s]upercronic}"

if ! command -v pgrep >/dev/null 2>&1; then
  echo "pgrep is not available; cannot verify supercronic process" >&2
  exit 1
fi

if ! pgrep -f "$SUPERCRONIC_PATTERN" >/dev/null 2>&1; then
  echo "supercronic process is not running for ${SERVICE}" >&2
  exit 1
fi

if [ -e "$STAMP" ]; then
  python3 - "$STAMP" "${LINKEDIN_COMMENTATOR_HEALTH_MAX_STAMP_AGE_SECONDS:-7500}" <<'PY'
import os
import sys
import time
path = sys.argv[1]
max_age = int(sys.argv[2])
age = time.time() - os.path.getmtime(path)
print(f"cron stamp age={age:.1f}s path={path}")
if age < -300:
    print(f"cron stamp is more than 300s in the future: {path}", file=sys.stderr)
    raise SystemExit(1)
if age > max_age:
    print(f"cron stamp is stale: age={age:.1f}s max={max_age}s", file=sys.stderr)
    raise SystemExit(1)
PY
else
  echo "cron stamp not present yet: $STAMP (ok before first scheduled run)"
fi

crontab_path="/app/crontab"
if [ "${CENTRAL_SCHEDULER_MODE:-0}" = "1" ]; then
  crontab_path="/app/crontab.central"
elif [ "${CRON_TEST_MODE:-0}" = "1" ]; then
  crontab_path="/app/crontab.test"
fi

if [ ! -r "$crontab_path" ]; then
  echo "crontab is not readable: $crontab_path" >&2
  exit 1
fi

if [ "${CENTRAL_SCHEDULER_MODE:-0}" = "1" ] && grep -q '/app/scripts/run.sh\|run_unlocked.sh' "$crontab_path"; then
  echo "central crontab must not start worker scripts: $crontab_path" >&2
  exit 1
fi

python3 - "$crontab_path" "/Users/deploydog-ai/LinkedIn/shared/comment_state/linkedin_commentator_status.json" \
  "${LINKEDIN_COMMENTATOR_HEALTH_MAX_STATUS_AGE_SECONDS:-7500}" "${CENTRAL_SCHEDULER_MODE:-0}" <<'PY'
import json
import os
import re
import sys
import time

crontab_path, status_path, max_age_raw, central_mode = sys.argv[1:]
cron = open(crontab_path, encoding="utf-8").read()
if central_mode == "1":
    if re.search(r"/app/scripts/run\.sh\b|run_unlocked\.sh\b", cron):
        print("central idle crontab must not start Commentator worker", file=sys.stderr)
        raise SystemExit(1)
    print("central idle mode: status freshness is not required")
    raise SystemExit(0)
if not re.search(r"(?m)^17\s+10\s+\*\s+\*\s+\*\s+/app/scripts/run\.sh\b", cron):
    print("expected daily 10:17 Commentator cron line is missing", file=sys.stderr)
    raise SystemExit(1)
if os.path.exists(status_path):
    age = time.time() - os.path.getmtime(status_path)
    with open(status_path, encoding="utf-8") as handle:
        status = json.load(handle)
    if not isinstance(status, dict) or not status.get("started_at"):
        print("Commentator status JSON has invalid schema", file=sys.stderr)
        raise SystemExit(1)
    max_age = int(max_age_raw)
    if age < -300 or age > max_age:
        print(f"Commentator status is stale: age={age:.1f}s max={max_age}s", file=sys.stderr)
        raise SystemExit(1)
    print(f"status age={age:.1f}s stop_reason={status.get('stop_reason')}")
else:
    print(f"status not present yet: {status_path} (ok before first scheduled run)")
PY

echo "${SERVICE} healthcheck ok: supercronic running; crontab readable: ${crontab_path}"
