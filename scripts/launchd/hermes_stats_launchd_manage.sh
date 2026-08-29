#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT="/Users/deploydog-ai/LinkedIn"
DOMAIN="gui/$(/usr/bin/id -u)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
REPO_PLIST_DIR="$ROOT/launchd"
LOG_DIR="$ROOT/shared/logs/HermesStats/launchd"
STATE_DIR="$ROOT/shared/state/HermesStats"
TAIL_LINES="${TAIL_LINES:-80}"

DAILY_LABEL="ai.linkedin.hermes-stats.daily"
WEEKLY_LABEL="ai.linkedin.hermes-stats.weekly"
DAILY_PLIST="$DAILY_LABEL.plist"
WEEKLY_PLIST="$WEEKLY_LABEL.plist"

DRY_RUN="${DRY_RUN:-0}"

usage() {
  cat <<USAGE
Usage: $(basename "$0") [verify|healthcheck|install|unload|load|reload|kickstart-daily|kickstart-weekly|status|logs|uninstall]

Environment:
  DRY_RUN=1    Print mutating commands instead of executing them.
  TAIL_LINES=N Lines per log file for logs subcommand (default: 80).

Launchd domain: $DOMAIN
Labels:
  $DAILY_LABEL
  $WEEKLY_LABEL
USAGE
}

info() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

repo_plist_path() {
  local label="$1"
  printf '%s/%s.plist' "$REPO_PLIST_DIR" "$label"
}

target_plist_path() {
  local label="$1"
  printf '%s/%s.plist' "$LAUNCH_AGENTS_DIR" "$label"
}

all_labels() {
  printf '%s\n%s\n' "$DAILY_LABEL" "$WEEKLY_LABEL"
}

ensure_dirs() {
  run mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$STATE_DIR"
}

lint_plist() {
  local path="$1"
  [[ -f "$path" ]] || { warn "missing plist: $path"; return 1; }
  /usr/bin/plutil -lint "$path"
}

bash_lint() {
  local path="$1"
  [[ -f "$path" ]] || { warn "missing script: $path"; return 1; }
  /bin/bash -n "$path"
}

validate_repo_plists() {
  local label path
  for label in $(all_labels); do
    path="$(repo_plist_path "$label")"
    lint_plist "$path"
  done
}

validate_wrappers() {
  bash_lint "$ROOT/scripts/launchd/hermes_stats_launchd_daily.sh"
  bash_lint "$ROOT/scripts/launchd/hermes_stats_launchd_weekly.sh"
  bash_lint "$ROOT/scripts/launchd/hermes_stats_launchd_manage.sh"
}

run_healthcheck() {
  /usr/bin/python3 "$ROOT/scripts/launchd/hermes_stats_launchd_healthcheck.py"
}

copy_plists() {
  local label src dst
  ensure_dirs
  validate_repo_plists
  for label in $(all_labels); do
    src="$(repo_plist_path "$label")"
    dst="$(target_plist_path "$label")"
    run /usr/bin/install -m 644 "$src" "$dst"
  done
}

is_loaded() {
  local label="$1"
  /bin/launchctl print "$DOMAIN/$label" >/dev/null 2>&1
}

print_job_summary() {
  local label="$1"
  info ""
  info "== $label =="
  if is_loaded "$label"; then
    /bin/launchctl print "$DOMAIN/$label" 2>/dev/null | /usr/bin/sed -n '1,80p'
  else
    info "not loaded in $DOMAIN"
    local target
    target="$(target_plist_path "$label")"
    if [[ -f "$target" ]]; then
      info "target plist exists: $target"
    else
      info "target plist missing: $target"
    fi
  fi
}

load_jobs() {
  local label target
  for label in $(all_labels); do
    target="$(target_plist_path "$label")"
    if [[ ! -f "$target" && "$DRY_RUN" != "1" ]]; then
      warn "target plist missing: $target (run install first)"
      return 1
    fi
    run /bin/launchctl enable "$DOMAIN/$label"
    if is_loaded "$label"; then
      info "$label already loaded"
    else
      run /bin/launchctl bootstrap "$DOMAIN" "$target"
    fi
  done
}

unload_jobs() {
  local label target
  for label in $(all_labels); do
    target="$(target_plist_path "$label")"
    if is_loaded "$label" || [[ "$DRY_RUN" == "1" ]]; then
      run /bin/launchctl bootout "$DOMAIN" "$target"
    else
      info "$label not loaded"
    fi
  done
}

uninstall_jobs() {
  local label target
  unload_jobs || true
  for label in $(all_labels); do
    target="$(target_plist_path "$label")"
    if [[ -e "$target" || "$DRY_RUN" == "1" ]]; then
      run rm -f "$target"
    else
      info "target plist already absent: $target"
    fi
  done
}

kickstart_job() {
  local label="$1"
  if [[ "$DRY_RUN" != "1" ]] && ! is_loaded "$label"; then
    warn "$label is not loaded in $DOMAIN"
    return 1
  fi
  run /bin/launchctl kickstart -kp "$DOMAIN/$label"
}

print_status_json() {
  local period path
  info ""
  info "== last status json =="
  for period in daily weekly; do
    path="$STATE_DIR/last_${period}.json"
    info "-- $path --"
    if [[ -f "$path" ]]; then
      if command -v python3 >/dev/null 2>&1; then
        /usr/bin/python3 - <<PY "$path"
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception as exc:
    print(f"invalid json: {exc}")
    raise SystemExit(1)
for key in ("period", "status", "exit_code", "started_at", "finished_at", "command", "log_file"):
    if key in data:
        print(f"{key}: {data[key]}")
PY
      else
        /bin/cat "$path"
      fi
    else
      info "missing"
    fi
  done
}

show_status() {
  local label
  for label in $(all_labels); do
    print_job_summary "$label"
  done
  print_status_json
}

tail_one_log() {
  local path="$1"
  info ""
  info "== $path =="
  if [[ -f "$path" ]]; then
    /usr/bin/tail -n "$TAIL_LINES" "$path"
  else
    info "missing"
  fi
}

show_logs() {
  tail_one_log "$LOG_DIR/launchd-daily.out.log"
  tail_one_log "$LOG_DIR/launchd-daily.err.log"
  tail_one_log "$LOG_DIR/daily.out.log"
  tail_one_log "$LOG_DIR/daily.err.log"
  tail_one_log "$LOG_DIR/launchd-weekly.out.log"
  tail_one_log "$LOG_DIR/launchd-weekly.err.log"
  tail_one_log "$LOG_DIR/weekly.out.log"
  tail_one_log "$LOG_DIR/weekly.err.log"
}

verify() {
  local rc=0
  info "root: $ROOT"
  info "launchd domain: $DOMAIN"
  info "repo plist dir: $REPO_PLIST_DIR"
  info "target dir: $LAUNCH_AGENTS_DIR"
  info "log dir: $LOG_DIR"
  info "state dir: $STATE_DIR"
  info ""
  info "== plutil -lint repo plists =="
  validate_repo_plists || rc=1
  info ""
  info "== bash -n wrappers/manage =="
  validate_wrappers || rc=1
  info ""
  info "== target plists =="
  local label target
  for label in $(all_labels); do
    target="$(target_plist_path "$label")"
    if [[ -f "$target" ]]; then
      info "exists: $target"
      lint_plist "$target" || rc=1
    else
      info "missing: $target"
    fi
  done
  return "$rc"
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    verify)
      verify
      ;;
    healthcheck)
      run_healthcheck
      ;;
    install)
      copy_plists
      ;;
    unload)
      unload_jobs
      ;;
    load)
      ensure_dirs
      load_jobs
      ;;
    reload)
      unload_jobs || true
      copy_plists
      load_jobs
      ;;
    kickstart-daily)
      kickstart_job "$DAILY_LABEL"
      ;;
    kickstart-weekly)
      kickstart_job "$WEEKLY_LABEL"
      ;;
    status)
      show_status
      ;;
    logs)
      show_logs
      ;;
    uninstall)
      uninstall_jobs
      ;;
    -h|--help|help)
      usage
      ;;
    '')
      usage >&2
      return 2
      ;;
    *)
      usage >&2
      warn "unknown subcommand: $cmd"
      return 2
      ;;
  esac
}

main "$@"
