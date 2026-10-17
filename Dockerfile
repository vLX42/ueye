# uEye - self-hosted XMEye replacement (live + recordings + config)
# Single container, single web port (8080). Build for your NAS arch:
#   docker build -t ueye .
FROM python:3.12-slim

ARG GO2RTC_VERSION=1.9.9
ARG CADDY_VERSION=2.9.1
ARG TARGETARCH=amd64

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# go2rtc (live video engine with native dvrip:// support)
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) G=linux_amd64 ;; \
      arm64) G=linux_arm64 ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_${G}"; \
    chmod +x /usr/local/bin/go2rtc

# caddy (single front door / reverse proxy)
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) C=amd64 ;; \
      arm64) C=arm64 ;; \
    esac; \
    curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${C}.tar.gz" \
      | tar -xz -C /usr/local/bin caddy; \
    chmod +x /usr/local/bin/caddy

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY web/ web/
COPY Caddyfile run.sh tailwind.config.js ./
RUN chmod +x run.sh

# Compile Tailwind to a local stylesheet (no runtime CDN; works offline).
# Standalone CLI = one self-contained binary, no Node in the image.
ARG TAILWIND_VERSION=3.4.16
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) T=linux-x64 ;; \
      arm64) T=linux-arm64 ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${T}"; \
    chmod +x /usr/local/bin/tailwindcss; \
    tailwindcss -c tailwind.config.js -i web/src/input.css -o web/tailwind.css --minify; \
    rm -f /usr/local/bin/tailwindcss

EXPOSE 8080
VOLUME ["/config"]
ENV NVR_CONFIG=/config/config.yaml
CMD ["./run.sh"]
