#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINKEDIN_RUNTIME_LOCK_PATH="${LINKEDIN_RUNTIME_LOCK_PATH:-/shared/runtime/linkedin-workers.lock}"
LINKEDIN_RUNTIME_LOCK_WAIT_TIMEOUT="${LINKEDIN_RUNTIME_LOCK_WAIT_TIMEOUT:-0}"
export LINKEDIN_RUNTIME_LOCK_PATH LINKEDIN_RUNTIME_LOCK_WAIT_TIMEOUT
exec python3 /app/scripts/linkedin_runtime_lock.py \
  --lock-path "$LINKEDIN_RUNTIME_LOCK_PATH" \
  --wait-timeout "$LINKEDIN_RUNTIME_LOCK_WAIT_TIMEOUT" \
  -- "$SCRIPT_DIR/run_unlocked.sh" "$@"
