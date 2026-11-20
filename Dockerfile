# uEye - self-hosted XMEye replacement (live + recordings + config)
# Single container, single web port (8080). Build for your NAS arch:
#   docker build -t ueye .
FROM python:3.12-slim

ARG GO2RTC_VERSION=1.9.9
ARG CADDY_VERSION=2.9.1
ARG TARGETARCH=amd64
# Checksums of the downloaded binaries; the build stops if one doesn't match.
# Bump them together with the versions.
# go2rtc publishes no checksums: these are sha256 of the v1.9.9 release assets.
ARG GO2RTC_SHA256_AMD64=aca941066c816cd69f4d689bf556f924057f5087449c2fdd7d5854d9aaec8ea9
ARG GO2RTC_SHA256_ARM64=d13d4d692833d9606ac0d0e25675bba58e804f3f401e4f70761c867f38d2777a
# caddy: sha512 from caddy_2.9.1_checksums.txt (signed) in the release.
ARG CADDY_SHA512_AMD64=0412057ba591c33bdb66034d7f32209619ce635147dac3084c79abcbc118a761145e695199f57c6798d88fab077f2b0d9cb999f52ded904c6d464a4eba202932
ARG CADDY_SHA512_ARM64=80b917c2bd8aa0892f1386ff97af9e776067dd8cdc4793238869aca7e13f7280f89d1799a1fef08b49af92dbd86684c3ba7a38b9d44afc55ef2eeb33b49e1cbd

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# go2rtc (live video engine with native dvrip:// support)
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) G=linux_amd64; S="$GO2RTC_SHA256_AMD64" ;; \
      arm64) G=linux_arm64; S="$GO2RTC_SHA256_ARM64" ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_${G}"; \
    echo "$S  /usr/local/bin/go2rtc" | sha256sum -c -; \
    chmod +x /usr/local/bin/go2rtc

# caddy (single front door / reverse proxy)
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) C=amd64; S="$CADDY_SHA512_AMD64" ;; \
      arm64) C=arm64; S="$CADDY_SHA512_ARM64" ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/caddy.tar.gz \
      "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${C}.tar.gz"; \
    echo "$S  /tmp/caddy.tar.gz" | sha512sum -c -; \
    tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy; \
    rm -f /tmp/caddy.tar.gz; \
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
# sha256 from sha256sums.txt in the tailwindcss v3.4.16 release.
ARG TAILWIND_SHA256_AMD64=33f254b54c8754f16efbe2be1de38ca25192630dc36f164595a770d4bbf4d893
ARG TAILWIND_SHA256_ARM64=1e6746bba6f3d34d7550889a1a009ab90ee3794a5ebce60ed10688ad10680a87
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) T=linux-x64; S="$TAILWIND_SHA256_AMD64" ;; \
      arm64) T=linux-arm64; S="$TAILWIND_SHA256_ARM64" ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${T}"; \
    echo "$S  /usr/local/bin/tailwindcss" | sha256sum -c -; \
    chmod +x /usr/local/bin/tailwindcss; \
    tailwindcss -c tailwind.config.js -i web/src/input.css -o web/tailwind.css --minify; \
    rm -f /usr/local/bin/tailwindcss

EXPOSE 8080
VOLUME ["/config"]
ENV NVR_CONFIG=/config/config.yaml
CMD ["./run.sh"]
