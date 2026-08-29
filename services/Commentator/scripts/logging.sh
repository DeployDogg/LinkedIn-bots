#!/usr/bin/env bash
set -euo pipefail

linkedin_log_init() {
  local service="${SERVICE_NAME:-service}"
  local base_dir="${LINKEDIN_LOG_BASE_DIR:-/shared/logs}"
  local retention_days="${LINKEDIN_LOG_RETENTION_DAYS:-3}"
  local service_dir="${base_dir}/${service}"
  local daily_dir="${service_dir}/daily"
  local today
  today="$(date +%F)"

  mkdir -p "$service_dir" "$daily_dir"
  touch "$service_dir/all-time.log" "$daily_dir/${today}.log"
  find "$daily_dir" -maxdepth 1 -type f -name '*.log' -mtime +$((retention_days - 1)) -delete 2>/dev/null || true

  exec > >(tee -a "$daily_dir/${today}.log" "$service_dir/all-time.log") 2>&1
  local stamp
  stamp="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "[$stamp] ${service}: logging initialized daily=$daily_dir/${today}.log all_time=$service_dir/all-time.log retention_days=$retention_days"
}
