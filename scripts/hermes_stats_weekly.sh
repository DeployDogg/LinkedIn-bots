#!/usr/bin/env bash
set -euo pipefail
/usr/bin/python3 /Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py --period weekly --send
/usr/bin/python3 /Users/deploydog-ai/LinkedIn/scripts/hermes_zero_watchdog.py --period weekly || true
