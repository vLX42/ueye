"""Load app config from config.yaml (path via NVR_CONFIG env, default /config/config.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Channel:
    id: int
    name: str = ""
    subtype: int = 1  # go2rtc substream index for the grid (0=main, 1=sub)


@dataclass
class AppConfig:
    host: str
    port: int = 34567
    username: str = "admin"
    password: str = ""
    channels: list[Channel] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | None = None) -> "AppConfig":
        p = Path(path or os.environ.get("NVR_CONFIG", "/config/config.yaml"))
        raw = yaml.safe_load(p.read_text()) or {}
        nvr = raw.get("nvr", {})
        chans = [
            Channel(id=c["id"], name=c.get("name", f"Camera {c['id']}"),
                    subtype=int(c.get("subtype", 1)))
            for c in raw.get("channels", [])
        ]
        return cls(
            host=nvr["host"],
            port=int(nvr.get("port", 34567)),
            username=nvr.get("username", "admin"),
            password=str(nvr.get("password", "")),
            channels=chans,
        )
