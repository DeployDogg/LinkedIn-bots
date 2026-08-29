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
  python3 - "$STAMP" <<'PY'
import os
import sys
import time
path = sys.argv[1]
age = time.time() - os.path.getmtime(path)
print(f"cron stamp age={age:.1f}s path={path}")
if age < -300:
    print(f"cron stamp is more than 300s in the future: {path}", file=sys.stderr)
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

echo "${SERVICE} healthcheck ok: supercronic running; crontab readable: ${crontab_path}"
