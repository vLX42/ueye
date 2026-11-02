"""Pause all cameras for a while (privacy), then switch them back on by itself.

Pausing changes two NVR settings for every channel:
  - Record.RecordMode -> "ClosedRecord" (no recording at all)
  - Detect.MotionDetect Enable -> False (no motion alerts / push to XMEye)
The values that were there before are saved in /config/pause.json *before*
anything is changed, so a crash or container restart halfway still knows what
to put back. A background loop in main.py resumes when the time is up, and
again after a restart if the time ran out while uEye was down.

uEye also stops showing live video while paused. The NVR still produces a
picture (its own monitor output and the XMEye app can show it); only
recording and alerts are off.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

MAX_MINUTES = 7 * 24 * 60
STATE = Path(os.environ.get("NVR_CONFIG", "/config/config.yaml")).parent / "pause.json"

_lock = threading.Lock()


def _load() -> dict[str, Any] | None:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def _save(st: dict[str, Any]) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def status() -> dict[str, Any]:
    st = _load()
    if not st:
        return {"paused": False}
    return {"paused": True, "until": st.get("until"), "since": st.get("since"), "error": st.get("error")}


def _set(c, name: str, value: Any) -> None:
    r = c.set_config(name, value)
    if str(r.get("Ret")) not in ("100", "603"):     # 603 = ok, takes effect after restart (not seen here)
        raise RuntimeError(f"NVR refused {name}: Ret={r.get('Ret')}")


def pause(connect: Callable[[], Any], minutes: int) -> dict[str, Any]:
    """Pause now (or move the end time if already paused)."""
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    until = time.time() + minutes * 60
    with _lock:
        st = _load()
        if st:
            st["until"] = until
            _save(st)
            return status()
        c = connect()
        try:
            rec = c.get_config("Record")
            md = c.get_config("Detect.MotionDetect")
            if not isinstance(rec, list) or not isinstance(md, list):
                raise RuntimeError("unexpected NVR config shape")
            _save({"since": time.time(), "until": until,
                   "saved": {"record_mode": [r.get("RecordMode") for r in rec],
                             "motion": [m.get("Enable") for m in md]}})
            try:
                for r in rec:
                    r["RecordMode"] = "ClosedRecord"
                _set(c, "Record", rec)
                for m in md:
                    m["Enable"] = False
                _set(c, "Detect.MotionDetect", md)
            except Exception:
                _restore(c, _load())         # half-paused: put it back, then report
                STATE.unlink(missing_ok=True)
                raise
        finally:
            c.close()
    return status()


def _restore(c, st: dict[str, Any] | None) -> None:
    if not st:
        return
    saved = st.get("saved") or {}
    rec = c.get_config("Record")
    for i, mode in enumerate(saved.get("record_mode") or []):
        if i < len(rec) and mode:
            rec[i]["RecordMode"] = mode
    _set(c, "Record", rec)
    md = c.get_config("Detect.MotionDetect")
    for i, en in enumerate(saved.get("motion") or []):
        if i < len(md) and en is not None:
            md[i]["Enable"] = en
    _set(c, "Detect.MotionDetect", md)


def resume(connect: Callable[[], Any]) -> bool:
    """Put the saved settings back. True if something was resumed."""
    with _lock:
        st = _load()
        if not st:
            return False
        c = connect()
        try:
            _restore(c, st)
        except Exception as e:
            st["error"] = str(e)
            _save(st)
            raise
        finally:
            c.close()
        STATE.unlink(missing_ok=True)
        return True


def due() -> bool:
    st = _load()
    return bool(st and st.get("until") and time.time() >= st["until"])
