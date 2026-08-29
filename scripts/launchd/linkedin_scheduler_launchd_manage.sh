#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/deploydog-ai/LinkedIn"
LABEL="ai.linkedin.scheduler"
PLIST_SRC="$ROOT/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER="$ROOT/scripts/launchd/linkedin_scheduler_launchd.sh"
LOG_DIR="$ROOT/shared/logs/LinkedInScheduler"
STATE_DIR="$ROOT/shared/state/LinkedInScheduler"
DOMAIN="gui/$(id -u)"

verify() {
  test -f "$PLIST_SRC"
  test -x "$WRAPPER"
  mkdir -p "$LOG_DIR" "$STATE_DIR"
  plutil -lint "$PLIST_SRC" >/dev/null
  bash -n "$WRAPPER"
  python3 "$ROOT/scripts/linkedin_scheduler.py" status --state "$STATE_DIR/state.json" --logs "$LOG_DIR" --lock "$STATE_DIR/scheduler.lock" >/dev/null
  echo "verify ok: $PLIST_SRC"
}

install_plist() {
  verify >/dev/null
  mkdir -p "$HOME/Library/LaunchAgents"
  if [ ! -f "$PLIST_DST" ] || ! cmp -s "$PLIST_SRC" "$PLIST_DST"; then
    /usr/bin/install -m 644 "$PLIST_SRC" "$PLIST_DST"
  fi
  echo "installed: $PLIST_DST"
}

load_job() {
  install_plist >/dev/null
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl enable "$DOMAIN/$LABEL"
    echo "loaded: $LABEL"
    return 0
  fi
  launchctl enable "$DOMAIN/$LABEL"
  launchctl bootstrap "$DOMAIN" "$PLIST_DST"
  echo "loaded: $LABEL"
}

unload_job() {
  launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
  echo "unloaded: $LABEL"
}

status_job() {
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null || {
    echo "not loaded: $LABEL"
    return 3
  }
}

logs_job() {
  mkdir -p "$LOG_DIR"
  for f in "$LOG_DIR/launchd.out.log" "$LOG_DIR/launchd.err.log" "$LOG_DIR/daily/$(date '+%Y-%m-%d').log"; do
    echo "== $f =="
    if [ -f "$f" ]; then tail -n 80 "$f"; else echo "missing"; fi
  done
}

case "${1:-}" in
  verify)
    verify
    ;;
  install)
    install_plist
    ;;
  load)
    load_job
    ;;
  unload)
    unload_job
    ;;
  reload)
    unload_job >/dev/null || true
    load_job
    ;;
  status)
    status_job
    ;;
  logs)
    logs_job
    ;;
  uninstall)
    unload_job >/dev/null || true
    rm -f "$PLIST_DST"
    echo "uninstalled: $PLIST_DST"
    ;;
  kickstart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    ;;
  *)
    echo "usage: $0 {verify|install|load|unload|reload|status|logs|uninstall|kickstart}" >&2
    exit 2
    ;;
esac
