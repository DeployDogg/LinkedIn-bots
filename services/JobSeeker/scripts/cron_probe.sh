#!/usr/bin/env bash
set -euo pipefail
SERVICE="${SERVICE_NAME:-service}"
mkdir -p /shared/logs
echo "$(date -Is) ${SERVICE} cron probe fired" | tee -a "/shared/logs/${SERVICE}.cron.probe.log"
date -Is > "/shared/logs/${SERVICE}.cron.last"
