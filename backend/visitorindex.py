"""Visitor store (SQLite): which motion events had a person in them.

For every scanned event we remember whether a person was seen; for those that
had one we keep a crop of the best shot (highest detector score). The UI groups
events that follow each other within a few minutes into one "visit", so the
mailman walking past two cameras is one card, not two.

Lives next to the face store under /config/faces, so it survives updates.
Rows older than the NVR's footage are pruned with the thumbnails.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(os.environ.get("FACE_DB", "/config/faces/faces.db")).parent / "visitors.db"
CROP_DIR = DB_PATH.parent / "visitors"

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CROP_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS scanned (
                channel INTEGER NOT NULL, ts TEXT NOT NULL, persons INTEGER NOT NULL,
                PRIMARY KEY (channel, ts));
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel INTEGER NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL,
                persons INTEGER NOT NULL, score REAL NOT NULL, crop TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_visits_day ON visits(day);
        """)


def event_done(channel: int, ts: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM scanned WHERE channel=? AND ts=?", (channel, ts)).fetchone() is not None


def record(channel: int, ts: str, persons: int, score: float = 0.0, crop: bytes | None = None) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO scanned(channel, ts, persons) VALUES (?,?,?)", (channel, ts, persons))
        if persons and crop:
            name = f"{channel}_{ts.replace(':', '').replace(' ', '_')}.jpg"
            (CROP_DIR / name).write_bytes(crop)
            c.execute("DELETE FROM visits WHERE channel=? AND ts=?", (channel, ts))
            c.execute("INSERT INTO visits(channel, ts, day, persons, score, crop) VALUES (?,?,?,?,?,?)",
                      (channel, ts, ts[:10], persons, score, name))


def day(d: str) -> dict:
    with _conn() as c:
        rows = c.execute("SELECT id, channel, ts, persons, score FROM visits WHERE day=? ORDER BY ts", (d,)).fetchall()
        n = c.execute("SELECT COUNT(*) n FROM scanned WHERE substr(ts,1,10)=?", (d,)).fetchone()["n"]
    return {"visits": [dict(r) for r in rows], "scanned": n}


def crop_path(vid: int) -> Path | None:
    with _conn() as c:
        r = c.execute("SELECT crop FROM visits WHERE id=?", (vid,)).fetchone()
    return (CROP_DIR / r["crop"]) if r else None


def prune(before_day: str) -> None:
    """Drop everything from days before `before_day` ('YYYY-MM-DD')."""
    with _lock, _conn() as c:
        for r in c.execute("SELECT crop FROM visits WHERE day < ?", (before_day,)).fetchall():
            (CROP_DIR / r["crop"]).unlink(missing_ok=True)
        c.execute("DELETE FROM visits WHERE day < ?", (before_day,))
        c.execute("DELETE FROM scanned WHERE substr(ts,1,10) < ?", (before_day,))
