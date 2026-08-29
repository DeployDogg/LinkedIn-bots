#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
run_id="$(date +%Y%m%d_%H%M%S)_$$"
prompt_file="/tmp/commentator_codex_prompt_${run_id}.txt"
out_file="/tmp/commentator_codex_reply_${run_id}.txt"
events_log="/tmp/commentator_codex_events_${run_id}.log"
stderr_log="/tmp/commentator_codex_stderr_${run_id}.log"
latest_events="/tmp/commentator_codex_events.log"
latest_stderr="/tmp/commentator_codex_stderr.log"

redact_log() {
  # Best-effort generic redaction for accidental token-like strings in Codex diagnostics.
  sed -E 's/(sk-[A-Za-z0-9_-]+)/[redacted]/g; s/([A-Za-z0-9_=-]{32,})/[redacted]/g' "$1" >"$1.redacted" 2>/dev/null && mv "$1.redacted" "$1" || true
}

codex_home=""
cleanup() {
  rm -f "$prompt_file" "$out_file"
  if [ -n "$codex_home" ] && [ -d "$codex_home" ]; then
    rm -rf "$codex_home"
  fi
}
trap cleanup EXIT

cat >"$prompt_file"

source_home="${LINKEDIN_COMMENTATOR_CODEX_SOURCE_HOME:-/root/.codex}"
runtime_parent="${LINKEDIN_COMMENTATOR_CODEX_RUNTIME_DIR:-/tmp/commentator_codex_runtime}"
mkdir -p "$runtime_parent"
codex_home="$(mktemp -d "${runtime_parent%/}/home.XXXXXX")"
chmod 700 "$codex_home"

# The host ~/.codex is mounted read-only for safety. Codex CLI needs writable state
# even for --ephemeral exec, so copy only auth/config metadata into a per-run home.
for name in config.toml auth.json version.json models_cache.json; do
  if [ -r "$source_home/$name" ]; then
    cp "$source_home/$name" "$codex_home/$name"
    chmod 600 "$codex_home/$name" 2>/dev/null || true
  fi
done

set +e
HOME="$codex_home" CODEX_HOME="$codex_home" codex exec \
  --ephemeral \
  --skip-git-repo-check \
  --sandbox read-only \
  --ignore-rules \
  --color never \
  -C /tmp \
  --output-last-message "$out_file" \
  - <"$prompt_file" >"$events_log" 2>"$stderr_log"
code=$?
set -e

redact_log "$events_log"
redact_log "$stderr_log"
cp "$events_log" "$latest_events" 2>/dev/null || true
cp "$stderr_log" "$latest_stderr" 2>/dev/null || true

if [ "$code" -ne 0 ]; then
  exit "$code"
fi

if [ -s "$out_file" ]; then
  cat "$out_file"
fi
