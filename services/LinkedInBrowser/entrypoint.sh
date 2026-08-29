#!/usr/bin/env bash
set -euo pipefail

PROFILE_DIR="${CHROME_PROFILE_DIR:-/profile}"
DISPLAY_ID="${DISPLAY:-:99}"
SCREEN_SPEC="${XVFB_SCREEN:-1920x1080x24}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
START_URL="${LINKEDIN_BROWSER_START_URL:-https://www.linkedin.com/feed/}"

mkdir -p "$PROFILE_DIR" /tmp/browser-runtime
chmod 700 "$PROFILE_DIR" /tmp/browser-runtime

# Clear stale Chromium/Xvfb lock files inside this container/volume before
# startup. The legacy source profile is never mounted here, only the destination
# /profile volume is touched.
find "$PROFILE_DIR" -maxdepth 2 \( \
  -name SingletonLock -o \
  -name SingletonCookie -o \
  -name SingletonSocket -o \
  -name DevToolsActivePort \
\) -delete
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb "$DISPLAY_ID" -screen 0 "$SCREEN_SPEC" -nolisten tcp &
x11vnc -display "$DISPLAY_ID" -forever -shared -rfbport "$VNC_PORT" -listen 127.0.0.1 -nopw -quiet &
websockify --web=/usr/share/novnc/ "0.0.0.0:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" &

# Current Debian Chromium can bind DevTools to 127.0.0.1 and rejects non-local
# Host headers. Keep Chromium itself on --remote-debugging-port=9222, and expose
# that same port to Compose peers via an address-specific HTTP/WebSocket proxy.
BROWSER_CONTAINER_IP="$(hostname -i | awk '{print $1}')"
if [ -n "$BROWSER_CONTAINER_IP" ]; then
  CDP_PROXY_LISTEN_HOST="$BROWSER_CONTAINER_IP" \
  CDP_PROXY_LISTEN_PORT=9222 \
  CDP_PROXY_UPSTREAM_HOST=127.0.0.1 \
  CDP_PROXY_UPSTREAM_PORT=9222 \
  python3 /app/scripts/cdp_proxy.py &
fi

CHROME_BIN="/usr/bin/chromium"
exec "$CHROME_BIN" \
  --user-data-dir=/profile \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --no-first-run \
  --no-default-browser-check \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-background-timer-throttling \
  --disable-renderer-backgrounding \
  --window-size=1920,1080 \
  --start-maximized \
  "$START_URL"
