#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/deploydog-ai/LinkedIn"
export TZ="America/Argentina/Buenos_Aires"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$ROOT/shared/logs/LinkedInScheduler" "$ROOT/shared/state/LinkedInScheduler"
cd "$ROOT"

while true; do
  python3 "$ROOT/scripts/linkedin_scheduler.py" run-due \
    --state "$ROOT/shared/state/LinkedInScheduler/state.json" \
    --logs "$ROOT/shared/logs/LinkedInScheduler" \
    --lock "$ROOT/shared/state/LinkedInScheduler/scheduler.lock" || rc=$?
  rc="${rc:-0}"
  if [ "$rc" = "11" ] || [ "$rc" = "12" ]; then
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] safety stop exit=$rc; keeping launchd wrapper alive" >&2
  elif [ "$rc" != "0" ] && [ "$rc" != "75" ]; then
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] scheduler run-due exit=$rc" >&2
  fi
  unset rc
  sleep 60
done
