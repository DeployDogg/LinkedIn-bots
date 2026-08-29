#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export TZ="America/Argentina/Buenos_Aires"

ROOT="/Users/deploydog-ai/LinkedIn"
PERIOD="daily"
LOG_DIR="$ROOT/shared/logs/HermesStats/launchd"
STATE_DIR="$ROOT/shared/state/HermesStats"
OUT_LOG="$LOG_DIR/${PERIOD}.out.log"
ERR_LOG="$LOG_DIR/${PERIOD}.err.log"
STATUS_FILE="$STATE_DIR/last_${PERIOD}.json"
LOCK_DIR="$STATE_DIR/${PERIOD}.lock"
TIMEOUT_SECONDS="${HERMES_STATS_TIMEOUT_SECONDS:-600}"

ba_now() {
  TZ="America/Argentina/Buenos_Aires" date '+%Y-%m-%dT%H:%M:%S%z'
}

json_escape() {
  local s="${1-}"
  s=${s//\\/\\\\}
  s=${s//"/\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

write_status() {
  local period="$1"
  local started_at="$2"
  local finished_at="$3"
  local exit_code="$4"
  local status="$5"
  local command="$6"
  local log_file="$7"
  local tmp_file="${STATUS_FILE}.$$"

  {
    printf '{\n'
    printf '  "period": "%s",\n' "$(json_escape "$period")"
    printf '  "started_at": "%s",\n' "$(json_escape "$started_at")"
    printf '  "finished_at": "%s",\n' "$(json_escape "$finished_at")"
    printf '  "exit_code": %s,\n' "$exit_code"
    printf '  "status": "%s",\n' "$(json_escape "$status")"
    printf '  "command": "%s",\n' "$(json_escape "$command")"
    printf '  "log_file": "%s"\n' "$(json_escape "$log_file")"
    printf '}\n'
  } > "$tmp_file"
  mv "$tmp_file" "$STATUS_FILE"
}

cleanup() {
  if [[ -d "$LOCK_DIR" ]] && [[ -f "$LOCK_DIR/pid" ]] && [[ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$LOCK_DIR"
  fi
}
trap cleanup EXIT INT TERM

mkdir -p "$LOG_DIR" "$STATE_DIR"
cd "$ROOT"

started_at="$(ba_now)"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  existing_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    finished_at="$(ba_now)"
    locked_command="/usr/bin/python3 scripts/hermes_container_status_bot.py --period $PERIOD"
    if [[ "${HERMES_STATS_DRY_RUN:-0}" != "1" ]]; then
      locked_command="$locked_command --send"
    fi
    echo "$finished_at [$PERIOD] another launchd wrapper is already running pid=$existing_pid" >> "$ERR_LOG"
    write_status "$PERIOD" "$started_at" "$finished_at" 75 "locked" "$locked_command" "$OUT_LOG"
    exit 75
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
printf '%s\n' "$started_at" > "$LOCK_DIR/started_at"

cmd=(/usr/bin/python3 scripts/hermes_container_status_bot.py --period "$PERIOD")
command_string="/usr/bin/python3 scripts/hermes_container_status_bot.py --period $PERIOD"
if [[ "${HERMES_STATS_DRY_RUN:-0}" != "1" ]]; then
  cmd+=(--send)
  command_string="$command_string --send"
fi

echo "$started_at [$PERIOD] start: $command_string" >> "$OUT_LOG"

set +e
"${cmd[@]}" >> "$OUT_LOG" 2>> "$ERR_LOG" &
child_pid=$!

exit_code=0
elapsed=0
while kill -0 "$child_pid" 2>/dev/null; do
  if (( elapsed >= TIMEOUT_SECONDS )); then
    echo "$(ba_now) [$PERIOD] timeout after ${TIMEOUT_SECONDS}s; terminating pid=$child_pid" >> "$ERR_LOG"
    kill -TERM "$child_pid" 2>/dev/null || true
    sleep 2
    if kill -0 "$child_pid" 2>/dev/null; then
      kill -KILL "$child_pid" 2>/dev/null || true
    fi
    wait "$child_pid" 2>/dev/null
    exit_code=124
    break
  fi
  sleep 1
  elapsed=$((elapsed + 1))
done

if (( exit_code == 0 )); then
  wait "$child_pid"
  exit_code=$?
fi
set -e

finished_at="$(ba_now)"
if (( exit_code == 0 )); then
  run_status="ok"
else
  run_status="error"
fi

echo "$finished_at [$PERIOD] finished exit_code=$exit_code status=$run_status" >> "$OUT_LOG"
write_status "$PERIOD" "$started_at" "$finished_at" "$exit_code" "$run_status" "$command_string" "$OUT_LOG"
exit "$exit_code"
