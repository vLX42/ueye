"""Access log: who used uEye, from where, and what they did.

One SQLite file next to config.yaml (/config/access.db), kept for 90 days.
Rows are written by a middleware in main.py (server-side actions: playback,
export, talk, config changes, logins, blocked outside requests) and by the UI
reporting which page it opened (live video goes straight to go2rtc, so the API
never sees it otherwise).

Noisy actions are collapsed: the same IP doing the same thing again within a
few minutes (e.g. playback fetching the next 20 s chunk) is not logged twice.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

KEEP_DAYS = 90
VIEWS = {"live": "Opened Live", "recordings": "Opened Recordings",
         "people": "Opened People", "settings": "Opened Settings"}

router = APIRouter()
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_recent: dict[tuple, float] = {}   # dedupe key -> last logged
_last_prune = 0.0


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(os.environ.get("NVR_CONFIG", "/config/config.yaml")).parent / "access.db"
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("""CREATE TABLE IF NOT EXISTS access (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, ip TEXT, home INTEGER,
            action TEXT, detail TEXT, device TEXT, session TEXT, ok INTEGER)""")
        _conn.execute("CREATE INDEX IF NOT EXISTS access_ts ON access(ts)")
    return _conn


def device_of(ua: str) -> str:
    """'iPhone · Safari' style label; enough to tell devices apart."""
    ua = ua or ""
    os_ = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
           else "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua
           else "")
    br = ("Edge" if "Edg/" in ua else "Firefox" if "Firefox/" in ua else "Chrome" if ("Chrome/" in ua or "CriOS/" in ua)
          else "Safari" if "Safari/" in ua else "")
    if not os_ and not br:
        return (ua[:40] or "unknown")
    return " · ".join(x for x in (os_, br) if x)


def client(req) -> dict:
    """ip / home / device / session for a Request or WebSocket (headers set by Caddy)."""
    h = req.headers
    tok = req.cookies.get("ueye_session") or ""
    return {
        "ip": h.get("x-client-ip") or (req.client.host if req.client else "?"),
        "home": h.get("x-ueye-home") == "1",
        "device": device_of(h.get("user-agent", "")),
        # short fingerprint of the login cookie: tells logged-in devices apart
        # without storing the cookie itself
        "session": hashlib.sha256(tok.encode()).hexdigest()[:6] if tok else "",
    }


def record(c: dict, action: str, detail: str = "", ok: bool = True,
           dedupe_s: float = 0, dedupe_key: str | None = None) -> None:
    global _last_prune
    now = time.time()
    try:
        with _lock:
            if dedupe_s:
                k = (c["ip"], action, dedupe_key if dedupe_key is not None else detail)
                if now - _recent.get(k, 0) < dedupe_s:
                    return
                _recent[k] = now
                if len(_recent) > 5000:
                    for kk in [kk for kk, t in _recent.items() if now - t > 3600]:
                        del _recent[kk]
            db = _db()
            db.execute("INSERT INTO access(ts, ip, home, action, detail, device, session, ok) VALUES (?,?,?,?,?,?,?,?)",
                       (now, c["ip"], int(c["home"]), action, detail[:200], c["device"], c["session"], int(ok)))
            if now - _last_prune > 3600:
                db.execute("DELETE FROM access WHERE ts < ?", (now - KEEP_DAYS * 86400,))
                _last_prune = now
    except sqlite3.Error as e:   # never break a request because logging failed
        print(f"[accesslog] {e}", flush=True)


@router.get("/api/access-log")
def list_log(scope: str = "all", before: float | None = None, limit: int = 200):
    q = "SELECT ts, ip, home, action, detail, device, session, ok FROM access WHERE 1=1"
    args: list = []
    if scope == "outside":
        q += " AND home = 0"
    elif scope == "home":
        q += " AND home = 1"
    if before:
        q += " AND ts < ?"
        args.append(before)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    with _lock:
        rows = _db().execute(q, args).fetchall()
    keys = ("ts", "ip", "home", "action", "detail", "device", "session", "ok")
    return {"keep_days": KEEP_DAYS,
            "rows": [dict(zip(keys, r)) | {"home": bool(r[2]), "ok": bool(r[7])} for r in rows]}


class ViewBody(BaseModel):
    view: str


@router.post("/api/access-log/view")
def log_view(body: ViewBody, request: Request):
    if body.view not in VIEWS:
        return JSONResponse({"detail": "unknown view"}, status_code=400)
    record(client(request), VIEWS[body.view], dedupe_s=300)
    return {"ok": True}
