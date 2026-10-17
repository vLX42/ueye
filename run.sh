#!/usr/bin/env bash
# Container entrypoint: generate go2rtc config from config.yaml, then run
# go2rtc + uvicorn + caddy together. If any one dies, take the container down
# so Unraid/Docker restarts it cleanly.
set -euo pipefail

CONFIG="${NVR_CONFIG:-/config/config.yaml}"
if [[ ! -f "$CONFIG" ]]; then
	echo "ERROR: $CONFIG not found. Mount a config.yaml (see config.example.yaml)." >&2
	exit 1
fi

echo "[ipcamera] generating go2rtc config from $CONFIG"
python3 /app/backend/go2rtc_gen.py /run/go2rtc.yaml

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "[ipcamera] starting go2rtc"
go2rtc -config /run/go2rtc.yaml & pids+=($!)

echo "[ipcamera] starting api (uvicorn)"
(cd /app && uvicorn backend.main:app --host 127.0.0.1 --port 8000) & pids+=($!)

echo "[ipcamera] starting caddy on :8080"
caddy run --config /app/Caddyfile --adapter caddyfile & pids+=($!)

# Exit (and trigger cleanup + container restart) as soon as any process exits.
wait -n
echo "[ipcamera] a service exited; shutting down" >&2
