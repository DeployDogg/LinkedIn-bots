#!/usr/bin/env bash
set -euo pipefail

pgrep -f 'chromium.*--remote-debugging-port=9222' >/dev/null
curl --fail --silent --show-error --max-time 3 http://127.0.0.1:9222/json/version >/dev/null
container_ip="$(hostname -i | awk '{print $1}')"
test -n "$container_ip"
curl --fail --silent --show-error --max-time 3 \
  -H 'Host: linkedin-browser:9222' \
  "http://${container_ip}:9222/json/version" >/dev/null
