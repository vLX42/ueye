"""Face store + online clustering (SQLite).

Every detected face becomes a row with its embedding, a cropped JPEG and the
time/channel it was seen. Faces are grouped into "people" by greedy cosine
clustering: a new face joins the person whose running-average embedding is closest
(above SIM_THRESHOLD), otherwise it starts a new person. This is O(faces × people),
cheap enough for a home NVR and needs no heavyweight clustering library.

Data lives under the persistent /config volume so it survives container restarts.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

DB_PATH = Path(os.environ.get("FACE_DB", "/config/faces/faces.db"))
CROP_DIR = DB_PATH.parent / "crops"
# SFace cosine similarity: OpenCV's reference "same identity" threshold is 0.363.
SIM_THRESHOLD = float(os.environ.get("FACE_SIM_THRESHOLD", "0.363"))

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CROP_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                centroid BLOB NOT NULL,      -- running SUM of embeddings (float32[512])
                count INTEGER NOT NULL,      -- number of faces summed
                rep_face_id INTEGER,         -- best (highest score) face, for the avatar
                rep_score REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER,
                channel INTEGER NOT NULL,
                ts TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM:SS'
                day TEXT NOT NULL,
                score REAL NOT NULL,
                embedding BLOB NOT NULL,
                crop TEXT NOT NULL,          -- crop filename under CROP_DIR
                created TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
            CREATE INDEX IF NOT EXISTS idx_faces_day ON faces(day);
            -- remember which (channel, event-time) we already processed, so re-runs
            -- are idempotent and cheap.
            CREATE TABLE IF NOT EXISTS seen_events (
                channel INTEGER NOT NULL,
                ts TEXT NOT NULL,
                PRIMARY KEY (channel, ts)
            );
            """
        )


def event_done(channel: int, ts: str) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM seen_events WHERE channel=? AND ts=?", (channel, ts)
        ).fetchone() is not None


def mark_event(channel: int, ts: str) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO seen_events(channel, ts) VALUES (?,?)", (channel, ts))


def _assign_person(c: sqlite3.Connection, emb: np.ndarray) -> tuple[int, bool]:
    """Return (person_id, created_new). emb is L2-normalised."""
    rows = c.execute("SELECT id, centroid, count FROM people").fetchall()
    best_id, best_sim = None, -1.0
    for r in rows:
        centroid = np.frombuffer(r["centroid"], dtype=np.float32)
        mean = centroid / max(r["count"], 1)
        n = np.linalg.norm(mean)
        if n == 0:
            continue
        sim = float(np.dot(emb, mean / n))
        if sim > best_sim:
            best_sim, best_id = sim, r["id"]
    if best_id is not None and best_sim >= SIM_THRESHOLD:
        return best_id, False
    cur = c.execute(
        "INSERT INTO people(label, centroid, count) VALUES (NULL, ?, 0)",
        (emb.astype(np.float32).tobytes(),),
    )
    return cur.lastrowid, True


def add_face(channel: int, ts: str, emb: np.ndarray, crop_bytes: bytes, score: float) -> int:
    """Store one face, cluster it, update the person's centroid + avatar. Returns face id."""
    emb = emb.astype(np.float32)
    with _lock, _conn() as c:
        person_id, created = _assign_person(c, emb)
        day = ts[:10]
        crop_name = f"{channel}_{ts.replace(':', '').replace(' ', '_')}_{int(score*1000)}.jpg"
        (CROP_DIR / crop_name).write_bytes(crop_bytes)
        cur = c.execute(
            "INSERT INTO faces(person_id, channel, ts, day, score, embedding, crop, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (person_id, channel, ts, day, float(score), emb.tobytes(), crop_name,
             datetime.utcnow().isoformat()),
        )
        face_id = cur.lastrowid
        # update running centroid (sum) + count
        row = c.execute("SELECT centroid, count, rep_score FROM people WHERE id=?", (person_id,)).fetchone()
        new_sum = np.frombuffer(row["centroid"], dtype=np.float32).copy() if not created else np.zeros_like(emb)
        new_sum = new_sum + emb
        new_count = row["count"] + 1
        upd = {"centroid": new_sum.astype(np.float32).tobytes(), "count": new_count}
        if score > (row["rep_score"] or 0):
            c.execute("UPDATE people SET centroid=?, count=?, rep_face_id=?, rep_score=? WHERE id=?",
                      (upd["centroid"], new_count, face_id, float(score), person_id))
        else:
            c.execute("UPDATE people SET centroid=?, count=? WHERE id=?",
                      (upd["centroid"], new_count, person_id))
    return face_id


def people(min_count: int = 1) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT p.id, p.label, p.count, p.rep_face_id, "
            "       (SELECT MAX(ts) FROM faces WHERE person_id=p.id) AS last_seen "
            "FROM people p WHERE p.count >= ? ORDER BY p.count DESC", (min_count,)
        ).fetchall()
        return [dict(r) for r in rows]


def person_faces(person_id: int, limit: int = 500) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, channel, ts, score, crop FROM faces WHERE person_id=? "
            "ORDER BY ts DESC LIMIT ?", (person_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def crop_path(face_id: int) -> Path | None:
    with _conn() as c:
        r = c.execute("SELECT crop FROM faces WHERE id=?", (face_id,)).fetchone()
    return (CROP_DIR / r["crop"]) if r else None


def rename_person(person_id: int, label: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE people SET label=? WHERE id=?", (label, person_id))


def stats() -> dict:
    with _conn() as c:
        f = c.execute("SELECT COUNT(*) n FROM faces").fetchone()["n"]
        p = c.execute("SELECT COUNT(*) n FROM people").fetchone()["n"]
        ev = c.execute("SELECT COUNT(*) n FROM seen_events").fetchone()["n"]
    return {"faces": f, "people": p, "events_processed": ev}
