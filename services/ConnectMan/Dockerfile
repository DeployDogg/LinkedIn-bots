FROM python:3.11-slim-bookworm

ARG SUPERCRONIC_VERSION=v0.2.34
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl tzdata bash procps \
       libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 libasound2 libxss1 libx11-xcb1 \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) sc_arch="amd64" ;; \
        arm64) sc_arch="arm64" ;; \
        *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /usr/local/bin/supercronic "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${sc_arch}" \
    && chmod +x /usr/local/bin/supercronic \
    && pip install --no-cache-dir playwright==1.55.0 \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Argentina/Buenos_Aires \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PYTHONPATH=/app/scripts:/Users/deploydog-ai/LinkedIn/shared/legacy_state:/Users/deploydog-ai/LinkedIn/shared/message_state

WORKDIR /app
COPY scripts/ /app/scripts/
COPY crontab /app/crontab
COPY crontab.test /app/crontab.test
COPY crontab.central /app/crontab.central
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/scripts/*.sh 2>/dev/null || true
ENTRYPOINT ["/app/entrypoint.sh"]
