"""Generate go2rtc.yaml from AppConfig.

go2rtc has a native 'dvrip' source for Xiongmai-family recorders, so live view
needs no protocol code from us: we just point go2rtc at each channel and let it
expose MSE/WebRTC/HLS to the browser.
  dvrip://user:pass@host:port?channel=N&subtype=M
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import AppConfig  # noqa: E402


def build(cfg: AppConfig) -> dict:
    up = quote(cfg.username, safe="")
    pw = quote(cfg.password, safe="")
    streams: dict[str, str] = {}
    for ch in cfg.channels:
        auth = f"{up}:{pw}@" if (up or pw) else ""
        base = f"dvrip://{auth}{cfg.host}:{cfg.port}"
        streams[f"cam{ch.id}"] = f"{base}?channel={ch.id}&subtype=0"       # main
        streams[f"cam{ch.id}_sub"] = f"{base}?channel={ch.id}&subtype=1"   # sub
    return {
        "api": {"listen": "127.0.0.1:1984"},
        "rtsp": {"listen": "127.0.0.1:8554"},
        "webrtc": {"listen": ":8555"},
        "log": {"level": "info"},
        "streams": streams,
    }


def write(cfg: AppConfig, out: str) -> None:
    Path(out).write_text(yaml.safe_dump(build(cfg), sort_keys=False))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/run/go2rtc.yaml"
    write(AppConfig.load(), out)
    print(f"wrote {out}")
