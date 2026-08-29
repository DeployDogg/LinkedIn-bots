FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    CHROME_PROFILE_DIR=/profile \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        chromium \
        curl \
        procps \
        python3 \
        python3-pip \
        tini \
        websockify \
        novnc \
        x11vnc \
        xvfb \
    && python3 -m pip install --break-system-packages --no-cache-dir playwright==1.55.0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
COPY scripts/healthcheck.sh /app/scripts/healthcheck.sh
COPY scripts/central_auth.py /app/scripts/central_auth.py
COPY scripts/cdp_proxy.py /app/scripts/cdp_proxy.py
RUN chmod +x /app/entrypoint.sh /app/scripts/healthcheck.sh /app/scripts/central_auth.py /app/scripts/cdp_proxy.py \
    && mkdir -p /profile /session-backup \
    && chmod 700 /profile /session-backup

EXPOSE 9222 6080

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
