"""FastAPI backend: recordings search + camera/motion config over DVRIP.

Live video is handled by go2rtc (proxied by Caddy at /go2rtc/); this service
owns everything go2rtc can't do: recording search, config read/write, PTZ.

A short-lived DVRIP connection is opened per request and closed after, which
keeps things simple and avoids a stale session if the NVR drops us.
"""
from __future__ import annotations

import sys
import asyncio
import hashlib
import queue
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import subprocess

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, Response, FileResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import AppConfig  # noqa: E402
from dvrip import DVRIPClient, DVRIPError, SYSINFO_REQ  # noqa: E402
import audiogen  # noqa: E402
import sofia  # noqa: E402
import auth  # noqa: E402
import seasons  # noqa: E402
import accesslog  # noqa: E402

# Face sorting is optional: if the CV stack isn't installed the rest of the API
# still works and the face endpoints report unavailable.
try:
    import numpy as np  # noqa: E402
    import cv2  # noqa: E402
    import faces as faceslib  # noqa: E402
    import faceindex  # noqa: E402
    faceindex.init_db()
    FACES_OK = True
except Exception as _face_err:  # pragma: no cover
    FACES_OK = False
    _FACE_IMPORT_ERR = str(_face_err)

app = FastAPI(title="uEye", version="0.1.0")
app.include_router(auth.router)
app.include_router(accesslog.router)
CFG = AppConfig.load()


def _cam(ch) -> str:
    try:
        ch = int(ch)
    except (TypeError, ValueError):
        return f"Camera {ch}"
    return next((c.name for c in CFG.channels if c.id == ch), f"Camera {ch}")


def _log_access(request: Request, code: int) -> None:
    """Access log for actions visible from the path alone (see accesslog.py).
    Export / talk are logged in their handlers, which know the details."""
    path, m = request.url.path, request.method
    if path == "/api/auth/check":
        if code >= 300:      # an outside request without a valid login
            accesslog.record(accesslog.client(request), "Blocked (not logged in)",
                             request.headers.get("x-forwarded-uri", "")[:120], ok=False,
                             dedupe_s=600, dedupe_key="")
    elif path == "/api/auth/login" and m == "POST":
        c = accesslog.client(request)
        if code == 200:
            accesslog.record(c, "Logged in")
        else:
            why = {429: "too many attempts", 503: "no password set, outside access off"}.get(code, "wrong password")
            accesslog.record(c, "Login failed", why, ok=False)
    elif path == "/api/auth/logout" and m == "POST":
        accesslog.record(accesslog.client(request), "Logged out")
    elif path == "/api/clip" and code < 400:
        q = request.query_params
        start = q.get("start", "")
        # next-chunk fetches of the same camera collapse into one row
        accesslog.record(accesslog.client(request), "Playback",
                         f"{_cam(q.get('channel'))} {start[:16]}", dedupe_s=300,
                         dedupe_key=f"{q.get('channel')}|{start[:10]}")
    elif m == "GET" and path.startswith("/api/export/") and path.endswith("/file") and code < 400:
        job = _exports.get(path.split("/")[3]) or {}
        accesslog.record(accesslog.client(request), "Downloaded export", job.get("filename", ""), dedupe_s=60)
    elif m == "POST" and (path.startswith("/api/config/") or path == "/api/motion"):
        accesslog.record(accesslog.client(request), "Changed settings",
                         path.removeprefix("/api/config/").removeprefix("/api/"), ok=code < 400)
    elif m == "POST" and path.startswith("/api/faces/") and code < 400:
        accesslog.record(accesslog.client(request), "Faces",
                         "scan started" if path.endswith("/index") else "named a person")


class _AccessLogMiddleware:
    """Plain ASGI pass-through that only peeks at the response status. (FastAPI's
    @app.middleware("http") wraps streaming bodies, which would get in the way of
    the clip streams and their stop-on-disconnect.)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)

        async def send_wrap(msg):
            if msg["type"] == "http.response.start":
                try:
                    await asyncio.to_thread(_log_access, Request(scope), msg["status"])
                except Exception as e:  # logging must never break a request
                    print(f"[accesslog] {e}", flush=True)
            await send(msg)
        await self.app(scope, receive, send_wrap)


app.add_middleware(_AccessLogMiddleware)

# The NVR allows only a few concurrent playback streams. Cap how many ByTime
# pulls (clips + thumbnails) we run at once so a wall of lazy-loading thumbnails
# can't starve the device or step on live view.
_PLAYBACK_SEM = threading.Semaphore(2)
# Background pulls (thumbnails, face-scan frames, exports) may only ever hold ONE
# of those two slots, so a playback clip always has a slot free. Without this a
# wall of event-rail thumbnails made clicking an event take ~14s to start.
_BG_GATE = threading.Semaphore(1)

# Transcoded chunks and thumbnails are deterministic for a given (channel, time,
# params), so cache them on disk: re-seeks, rewatches and timeline scrubbing hit
# the cache instead of re-pulling 4K from the NVR.
CACHE_DIR = Path("/tmp/ipcam-cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_FMT = "%Y-%m-%d %H:%M:%S"


def _cache_path(kind: str, key: str, ext: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{kind}_{h}.{ext}"


def _norm_time(t: str) -> str:
    """Accept 'YYYY-MM-DD HH:MM:SS' or ISO 'YYYY-MM-DDTHH:MM:SS'."""
    t = t.strip().replace("T", " ")
    datetime.strptime(t, _FMT)  # validate / raise
    return t


def _ffmpeg_mp4(es: bytes, codec: str, transcode: bool, scale: int,
                preset: str = "veryfast") -> bytes:
    in_fmt = "hevc" if codec == "hevc" else "h264"
    if transcode:
        vf = ["-vf", f"scale='min({scale},iw)':-2"] if scale else []
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", in_fmt,
                "-i", "pipe:0", *vf, "-c:v", "libx264", "-preset", preset,
                "-pix_fmt", "yuv420p", "-movflags", "frag_keyframe+empty_moov+faststart",
                "-an", "-f", "mp4", "pipe:1"]
    else:
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", in_fmt,
                "-i", "pipe:0", "-c:v", "copy",
                "-movflags", "frag_keyframe+empty_moov+faststart",
                "-an", "-f", "mp4", "pipe:1"]
    proc = subprocess.run(args, input=es, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise HTTPException(status_code=500,
                            detail=f"ffmpeg failed: {proc.stderr.decode()[:300]}")
    return proc.stdout


def _ffmpeg_stream(es: bytes, codec: str, scale: int, preset: str, cache: Path, trim: float = 0.0):
    """Transcode to fragmented MP4 and yield it as ffmpeg produces it, teeing to
    the on-disk cache. The browser starts playing on the first fragments instead
    of waiting for the whole chunk, which is the big latency win on CPU-only 4K.

    `trim` seconds of decoded pre-roll are discarded from the OUTPUT (the input is
    padded earlier so the decoder has a complete keyframe to warm up on, avoiding
    the smeared partial GOP at a ByName sub-range boundary)."""
    in_fmt = "hevc" if codec == "hevc" else "h264"
    ss = ["-ss", f"{trim:.3f}"] if trim > 0 else []
    vf = ["-vf", f"scale='min({scale},iw)':-2"] if scale else []
    args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", in_fmt, "-i", "pipe:0",
            *ss, *vf, "-c:v", "libx264", "-preset", preset, "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", "30", "-avoid_negative_ts", "make_zero",
            "-movflags", "frag_keyframe+empty_moov+faststart+default_base_moof",
            "-an", "-f", "mp4", "pipe:1"]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)

    def feed():
        try:
            proc.stdin.write(es); proc.stdin.close()
        except Exception:
            pass
    threading.Thread(target=feed, daemon=True).start()

    tmp = cache.with_suffix(".part")

    def gen():
        f = open(tmp, "wb")
        try:
            while True:
                b = proc.stdout.read(65536)
                if not b:
                    break
                f.write(b)
                yield b
            proc.wait()
            f.close()
            if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(cache)            # atomically promote to cache
            else:
                tmp.unlink(missing_ok=True)
        finally:
            if not f.closed:
                f.close()
            if proc.poll() is None:
                proc.kill()
    return gen


def _ffmpeg_pipe(dl, demux, first: bytes, codec: str, scale: int, preset: str,
                 cache: Path, trim: float, client, sem, stop_at: datetime | None = None,
                 copy: bool = False, length: float = 0.0):
    """Transcode a LIVE download to fragmented MP4. `first` is the already-demuxed
    opening NALs (used to pick the input format); the rest is demuxed from `dl` as
    it streams in and fed to ffmpeg, so output starts without waiting for the whole
    slice. Owns the NVR client and the concurrency slot: both are released when the
    stream ends or the client disconnects. With stop_at, the download ends at the
    first I-frame stamped at/after it instead of waiting for the idle timeout."""
    in_fmt = "hevc" if codec == "hevc" else "h264"
    # frag_duration: ship a fragment every 0.5s instead of waiting for the NEXT
    # keyframe (2s GOP) to close the first one; the browser starts sooner.
    mov = ["-movflags", "frag_keyframe+empty_moov+faststart+default_base_moof", "-frag_duration", "500000",
           "-an", "-f", "mp4", "pipe:1"]
    if copy:
        # Original stream, no decode: ~0.2s CPU per chunk vs 20-40s to transcode
        # 4K HEVC on the NAS (2012 Xeon, no HEVC hw decode). Frame rate comes
        # from the stream's VUI; the demux already cut on keyframes.
        tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "+genpts",
                "-f", in_fmt, "-i", "pipe:0", "-c:v", "copy", *tag, *mov]
    else:
        ss = ["-ss", f"{trim:.3f}"] if trim > 0 else []
        tt = ["-t", f"{length:.3f}"] if length > 0 else []
        vf = ["-vf", f"scale='min({scale},iw)':-2"] if scale else []
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", in_fmt, "-i", "pipe:0",
                *ss, *tt, *vf, "-c:v", "libx264", "-preset", preset, "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", "10", "-avoid_negative_ts", "make_zero",
                *mov]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    # The NVR stops sending if we fall behind, and the idle timeout then reads
    # that as end-of-stream. 4K HEVC decodes slower than real time on the NAS, so
    # writing to ffmpeg must never stall the socket: drain the download in its own
    # thread into an unbounded queue (a 20s chunk is ~10 MB) and feed from there.
    q: queue.Queue = queue.Queue()
    stop = threading.Event()   # set when the browser gave up on this chunk

    def pull():
        tail = b""
        try:
            for body in dl:
                if stop.is_set():
                    break
                q.put(body)
                if stop_at:
                    t = sofia.last_iframe_time(tail + body)
                    if t and t >= stop_at:
                        break
                    tail = body[-15:]
        except Exception:
            pass
        finally:
            q.put(None)
            try: dl.close()           # unwind the generator -> release socket lock
            except Exception: pass
            try: client.close()
            except Exception: pass

    def feed():
        try:
            proc.stdin.write(first)
            while (body := q.get()) is not None:
                a = demux.feed(body)
                if a:
                    proc.stdin.write(a)
            a = demux.flush()
            if a:
                proc.stdin.write(a)
        except Exception:
            pass
        finally:
            try: proc.stdin.close()
            except Exception: pass

    tmp = cache.with_suffix(".part")
    out: queue.Queue = queue.Queue()

    # ffmpeg's output is owned by a thread, not by the response generator:
    # Starlette never closes a sync generator when the client disconnects, so
    # cleanup in its `finally` never ran and every aborted clip (seek, reload,
    # prefetch) leaked a _PLAYBACK_SEM slot. Two leaks and playback hung until a
    # restart. Now the chunk always finishes into the cache and frees its slot.
    def drain():
        f = open(tmp, "wb")
        try:
            while (b := proc.stdout.read(65536)):
                f.write(b)
                out.put(b)
            proc.wait()
            f.close()
            # A header-only MP4 (~800 B) means no frame made it; don't cache that.
            if (proc.returncode == 0 and not stop.is_set()
                    and tmp.exists() and tmp.stat().st_size > MIN_MP4):
                tmp.replace(cache)
            else:
                tmp.unlink(missing_ok=True)
        except Exception:
            tmp.unlink(missing_ok=True)
        finally:
            if not f.closed:
                f.close()
            if proc.poll() is None:
                proc.kill()
            out.put(None)
            sem.release()

    threading.Thread(target=pull, daemon=True).start()
    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=drain, daemon=True).start()

    async def agen():
        # Async on purpose: Starlette cancels an async body when the client
        # disconnects (seek, next event, closed player), which lands in the
        # finally below and stops the NVR pull instead of finishing a chunk
        # nobody watches while it blocks a playback slot.
        done = False
        try:
            while True:
                b = await asyncio.to_thread(out.get)
                if b is None:
                    done = True
                    return
                yield b
        finally:
            if not done:
                stop.set()
    return agen


@contextmanager
def nvr():
    c = DVRIPClient(host=CFG.host, port=CFG.port,
                    username=CFG.username, password=CFG.password)
    try:
        c.connect()
        yield c
    except DVRIPError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=504, detail=f"NVR unreachable: {e}")
    finally:
        c.close()


# --- NVR clock sync -----------------------------------------------------------
# The NVR is firewalled off the internet, so it has no NTP. Keep its clock
# right from the NAS (container TZ = local time): check at startup + hourly,
# correct when it's off by more than TIME_MAX_DRIFT seconds.
TIME_SYNC_EVERY = 3600
TIME_MAX_DRIFT = 2
_time_state: dict[str, Any] = {"last_check": None, "drift_s": None, "last_set": None, "error": None}


def _sync_nvr_time() -> None:
    c = DVRIPClient(host=CFG.host, port=CFG.port, username=CFG.username, password=CFG.password)
    try:
        c.connect()
        nvr_t = datetime.strptime(c.get_time(), _FMT)
        now = datetime.now()
        drift = round((nvr_t - now).total_seconds())
        _time_state.update(last_check=now.strftime(_FMT), drift_s=drift, error=None)
        if abs(drift) > TIME_MAX_DRIFT:
            r = c.set_time(datetime.now().strftime(_FMT))
            if str(r.get("Ret")) != "100":
                raise DVRIPError(f"OPTimeSetting failed: {r!r}")
            _time_state["last_set"] = datetime.now().strftime(_FMT)
            print(f"[time] NVR clock was {drift:+d}s off, corrected", flush=True)
    except Exception as e:  # never let the loop die
        _time_state["error"] = str(e)
        print(f"[time] sync failed: {e}", flush=True)
    finally:
        c.close()


def _time_sync_loop() -> None:
    while True:
        _sync_nvr_time()
        time.sleep(TIME_SYNC_EVERY)


threading.Thread(target=_time_sync_loop, daemon=True, name="nvr-time-sync").start()


@app.get("/api/nvr-time")
def nvr_time():
    return _time_state


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "nvr": f"{CFG.host}:{CFG.port}",
            "channels": [{"id": c.id, "name": c.name} for c in CFG.channels]}


# --- health: NVR disk, retention, cameras, clock + NAS-side usage -----------
# Metadata only (4 small DVRIP queries, cached), nothing is decoded.
_health_cache: tuple[float, dict[str, Any]] | None = None
_HEALTH_TTL = 60.0


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _nvr_health() -> dict[str, Any]:
    with nvr() as c:
        q = lambda n: c._cmd(SYSINFO_REQ, {"Name": n, "SessionID": c._sid_str()}).get(n)
        info, storage, work, chstat = q("SystemInfo") or {}, q("StorageInfo") or [], q("WorkState") or {}, q("ChannelStatus") or {}
    disks = []
    for d in storage:
        parts = [p for p in d.get("Partition", []) if int(str(p.get("TotalSpace", "0")), 16)]
        if not parts:
            continue
        total = sum(int(str(p["TotalSpace"]), 16) for p in parts)      # MB
        free = sum(int(str(p.get("RemainSpace", "0")), 16) for p in parts)
        starts = [t for p in parts for t in (p.get("NewStartTime"), p.get("OldStartTime")) if t and not t.startswith("0000")]
        ends = [t for p in parts for t in (p.get("NewEndTime"), p.get("OldEndTime")) if t and not t.startswith("0000")]
        disks.append({"total_mb": total, "free_mb": free,
                      "ok": all(int(p.get("Status", 0)) == 0 for p in parts),
                      "oldest": min(starts) if starts else None, "newest": max(ends) if ends else None})
    link = int(str(chstat.get("ChnLinkStatus", "0")), 16)
    loss = int((work.get("AlarmState") or {}).get("VideoLoss", 0))
    blind = int((work.get("AlarmState") or {}).get("VideoBlind", 0))
    states = work.get("ChannelState") or []
    cams = []
    for ch in CFG.channels:
        st = states[ch.id] if ch.id < len(states) else {}
        cams.append({"id": ch.id, "name": ch.name,
                     "online": bool(link >> ch.id & 1) and not (loss >> ch.id & 1),
                     "covered": bool(blind >> ch.id & 1),
                     "recording": bool(st.get("Record")), "bitrate_kbps": st.get("Bitrate", 0)})
    oldest = min((d["oldest"] for d in disks if d["oldest"]), default=None)
    return {
        "model": info.get("HardWare"), "firmware": info.get("SoftWareVersion"),
        "uptime_min": int(str(info.get("DeviceRunTime", "0")), 16),
        "disks": disks, "oldest_footage": oldest,
        "retention_days": round((datetime.now() - datetime.strptime(oldest, _FMT)).total_seconds() / 86400, 1) if oldest else None,
        "cameras": cams,
    }


@app.get("/api/health/status")
def health_status():
    global _health_cache
    now = time.time()
    if _health_cache and now - _health_cache[0] < _HEALTH_TTL:
        data = dict(_health_cache[1])
    else:
        try:
            data = {"nvr": _nvr_health(), "nvr_error": None}
        except HTTPException as e:
            data = {"nvr": None, "nvr_error": e.detail}
        _health_cache = (now, data)
        data = dict(data)
    data["clock"] = _time_state
    data["nas"] = {"cache_bytes": _dir_size(CACHE_DIR) - _dir_size(EXPORT_DIR),
                   "exports_bytes": _dir_size(EXPORT_DIR),
                   "exports": [{k: j[k] for k in ("filename", "state", "size")} for j in _exports.values()]}
    data["checked"] = datetime.fromtimestamp(_health_cache[0]).strftime(_FMT)
    return data


@app.get("/api/system")
def system() -> dict[str, Any]:
    with nvr() as c:
        return c.system_info()


def _parse_record(channel: int, f: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw OPFileQuery entry into a clean segment object."""
    name = f.get("FileName", "")
    # event tag in the filename, e.g. ...[R]... = regular/continuous, [M] = motion, [A] = alarm
    tag = "R"
    for t in ("M", "A", "H", "R"):
        if f"[{t}]" in name:
            tag = t
            break
    type_label = {"R": "continuous", "M": "motion", "A": "alarm", "H": "manual"}.get(tag, tag)
    begin, end = f.get("BeginTime", ""), f.get("EndTime", "")
    dur = 0
    try:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        dur = int((datetime.strptime(end, fmt) - datetime.strptime(begin, fmt)).total_seconds())
    except Exception:
        pass
    size_kb = 0
    try:
        size_kb = int(str(f.get("FileLength", "0x0")), 16)
    except Exception:
        pass
    return {
        "channel": channel,
        "begin": begin, "end": end,
        "day": begin[:10],
        "duration_s": dur,
        "size_kb": size_kb,
        "type": type_label,
        "fileName": name,
        "diskNo": f.get("DiskNo", 0),
    }


@app.get("/api/recordings")
def recordings(
    channel: str = "all",
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    event: str = "all",
    stream: str = "Main",
) -> dict[str, Any]:
    """Search recordings across a date range and one or all cameras.

    - channel: '0'..'7' or 'all'
    - a single `date`, or a `date_from`..`date_to` range (YYYY-MM-DD)
    - event: 'all' | 'continuous' | 'motion' | 'alarm'
    """
    if date and not date_from:
        date_from = date_to = date
    if not date_from:
        from datetime import date as _d
        date_from = date_to = _d.today().isoformat()
    date_to = date_to or date_from
    begin, end = f"{date_from} 00:00:00", f"{date_to} 23:59:59"

    from datetime import date as _date, timedelta
    d0 = _date.fromisoformat(date_from)
    d1 = _date.fromisoformat(date_to)
    day_list = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]

    chans = [c.id for c in CFG.channels] if channel == "all" else [int(channel)]
    out: list[dict[str, Any]] = []
    with nvr() as conn:
        for ch in chans:
            for day in day_list:
                # query one day at a time: the NVR caps results per OPFileQuery,
                # so a wide range would truncate. Per-day stays under the cap.
                b, e = f"{day} 00:00:00", f"{day} 23:59:59"
                try:
                    for f in conn.search_recordings(ch, b, e, stream=stream):
                        out.append(_parse_record(ch, f))
                except DVRIPError:
                    continue  # empty/invalid channel or no data that day
    if event != "all":
        out = [r for r in out if r["type"] == event]
    out.sort(key=lambda r: (r["begin"], r["channel"]))
    # group by day for the UI
    days: dict[str, list[dict[str, Any]]] = {}
    for r in out:
        days.setdefault(r["day"], []).append(r)
    return {
        "from": date_from, "to": date_to, "channels": chans, "event": event,
        "count": len(out),
        "total_size_kb": sum(r["size_kb"] for r in out),
        "days": [{"day": d, "files": days[d]} for d in sorted(days)],
    }


@app.get("/api/playback")
def playback(file: str, begin: str, end: str, size_kb: int = 0,
             transcode: int = 1, scale: int = 1280) -> StreamingResponse:
    """Download a recording, demux sofia -> Annex-B, and stream it as MP4.

    transcode=1 (default) re-encodes to H.264 so any browser plays it (cameras
    record 4K HEVC). transcode=0 copies the original codec into MP4 (fast, but
    needs a browser with HEVC support).
    """
    expected = size_kb * 1024 if size_kb else None
    with nvr() as c:
        raw = c.download_recording(file, begin, end, expected_bytes=expected)
    if not raw:
        raise HTTPException(status_code=404, detail="no data returned for that clip")
    es = sofia.to_annexb(raw)
    if not es:
        raise HTTPException(status_code=422, detail="could not demux recording")
    mp4 = _ffmpeg_mp4(es, sofia.detect_codec(es), bool(transcode), scale)
    return Response(content=mp4, media_type="video/mp4")


# --- seekable, time-based playback (the fancy timeline rides on these) -------
CLIP_MAX_DURATION = 300      # never pull more than 5 min in one chunk
CLIP_MAX_BYTES = 180_000_000  # hard byte cap per chunk (safety on 4K main stream)

# IMPORTANT: this NVR IGNORES the channel in ByTime playback (every channel returns
# channel 0's footage). The channel is only honoured via ByName — the filename path
# (/001 /002 /003 = ch0/1/2) selects it. ByName also honours a StartTime/EndTime
# sub-range, so we seek by looking up the file covering a moment on a channel and
# fetching just that slice by name.
_filelist_cache: dict[tuple[int, str], tuple[float, list[dict[str, Any]]]] = {}
_FILELIST_TTL = 120.0
_filelist_lock = threading.Lock()


def _files_for(channel: int, day: str) -> list[dict[str, Any]]:
    key = (channel, day)
    now = time.time()
    with _filelist_lock:
        hit = _filelist_cache.get(key)
        if hit and now - hit[0] < _FILELIST_TTL:
            return hit[1]
    with nvr() as c:
        raw = c.search_recordings(channel, f"{day} 00:00:00", f"{day} 23:59:59")
    parsed = [_parse_record(channel, f) for f in raw]
    with _filelist_lock:
        _filelist_cache[key] = (now, parsed)
    return parsed


def _file_at(channel: int, ts: str) -> dict[str, Any] | None:
    """The recording file on `channel` that contains timestamp ts.

    End is EXCLUSIVE: at a motion boundary a continuous segment ends exactly when
    the motion segment begins, and both nominally "cover" ts. The footage at ts
    lives in the one ts is *inside*, so prefer begin <= ts < end; fall back to a
    later file that starts at ts.
    """
    best = None
    for r in _files_for(channel, ts[:10]):
        if r["begin"] <= ts < r["end"]:
            if best is None or r["begin"] > best["begin"]:
                best = r
    if best:
        return best
    # exact-boundary / end-of-footage: accept a file that begins at ts
    for r in _files_for(channel, ts[:10]):
        if r["begin"] == ts:
            return r
    return None


# A ByName sub-range begins mid-GOP, so its first keyframe is partial -> a smear
# at the start of playback / in a one-shot frame. We pad the download this many
# seconds earlier (clamped to the file start) to give the decoder a complete
# keyframe to warm up on, then trim the pre-roll from the output.
PAD = 3.0
THUMB_WINDOW = 6   # s searched for a keyframe (GOP is ~2s)
CLIP_IDLE = 4.0  # s of NVR silence that means "no more footage"
MIN_MP4 = 4096  # bytes; below this a fragmented MP4 holds no video


def _padded(rec: dict[str, Any], ts: str) -> tuple[str, float]:
    """Return (download_start, trim_seconds) for a clean decode at ts."""
    t = datetime.strptime(ts, _FMT)
    fb = datetime.strptime(rec["begin"], _FMT)
    dl = max(t - timedelta(seconds=PAD), fb)
    return dl.strftime(_FMT), (t - dl).total_seconds()


# Quality presets. The NVR records main-stream 4K HEVC only (no sub on disk) and
# the container has no GPU, so SD can't be "free" — but a lower resolution with a
# faster preset cuts encode time and (greatly) the bytes streamed, which matters
# for remote/VPN viewing. HD is the full-detail path.
QUALITY = {
    "sd": {"scale": 640, "preset": "ultrafast"},
    "hd": {"scale": 1280, "preset": "veryfast"},
    # Original 4K stream, no transcode. For browsers that play HEVC (Chrome on
    # macOS, Safari); the frontend only offers it when canPlayType says so.
    "orig": {"scale": 0, "preset": "", "copy": True},
}


@app.get("/api/clip")
def clip(channel: int, start: str, duration: int = 90,
         transcode: int = 1, q: str = "hd") -> Response:
    """Play an arbitrary TIME WINDOW of a camera as MP4 (ByTime seek).

    Unlike /api/playback (whole named file), this fetches just [start, start+duration]
    so the timeline can seek to any moment and the player can stream hour-long
    continuous recordings in chunks. `q` is 'hd' or 'sd'. Result is cached on disk.
    """
    start = _norm_time(start)
    duration = max(1, min(int(duration), CLIP_MAX_DURATION))
    qp = QUALITY.get(q, QUALITY["hd"])
    scale, preset, copy = qp["scale"], qp["preset"], qp.get("copy", False)
    end = (datetime.strptime(start, _FMT) + timedelta(seconds=duration)).strftime(_FMT)
    cache = _cache_path("clip", f"{channel}|{start}|{duration}|{transcode}|{q}", "mp4")
    if cache.exists() and cache.stat().st_size > 0:
        return FileResponse(cache, media_type="video/mp4")
    rec = _file_at(channel, start)        # ByName selects the right channel
    if not rec:
        raise HTTPException(status_code=404, detail="no footage at that time")
    end = min(end, rec["end"])            # clamp the slice to the covering file
    dl_start, trim = _padded(rec, start)  # pad start so the first GOP is clean
    if copy:
        # Stream copy starts at the first keyframe AT/AFTER `start` (no decode, so
        # no pre-roll to warm up): downloading the PAD seconds before it was waste.
        dl_start = max(start, rec["begin"])
    # Stream the NVR download straight through the demux + transcode so the browser
    # gets the first fragment after the opening GOP arrives (~2-3s) instead of after
    # the whole slice downloads (~7-8s). The concurrency cap is held for the life of
    # the stream and released when the generator finishes/aborts.
    _PLAYBACK_SEM.acquire()
    released = False
    client = None
    dl = None
    try:
        if cache.exists() and cache.stat().st_size > 0:  # filled while we waited
            _PLAYBACK_SEM.release(); released = True
            return FileResponse(cache, media_type="video/mp4")
        client = DVRIPClient(host=CFG.host, port=CFG.port,
                             username=CFG.username, password=CFG.password)
        client.connect()
        # End-of-chunk is detected from the I-frame clock (stop_at below), so the
        # idle timeout only fires at the real end of footage. Keep it generous: a
        # >1s hiccup on the NVR link used to end the clip after one GOP.
        # The NVR stops at EndTime itself, so ask a GOP past the end: the keyframe
        # that closes this chunk then arrives and stop_at ends the pull right
        # there instead of after CLIP_IDLE of silence.
        nvr_end = min((datetime.strptime(end, _FMT) + timedelta(seconds=PAD)).strftime(_FMT), rec["end"])
        dl = client.stream_recording(rec["fileName"], dl_start, nvr_end, idle_timeout=CLIP_IDLE)
        if copy:   # no trim possible without decoding: cut on keyframes instead
            demux = sofia.StreamDemux(from_keyframe=True,
                                      start_at=datetime.strptime(start, _FMT),
                                      stop_at=datetime.strptime(end, _FMT))
        else:
            demux = sofia.StreamDemux(from_keyframe=True)
        first = b""
        for body in dl:                   # prime: pull until the first decodable NALs
            first = demux.feed(body)
            if first:
                break
        if not first:
            first = demux.flush()
        if not first:
            raise HTTPException(status_code=404, detail="no footage at that time")
        codec = sofia.detect_codec(first)
        if copy:
            trim = 0.0
        elif demux.key_time:              # output starts at the first keyframe
            trim = max(0.0, (datetime.strptime(start, _FMT) - demux.key_time).total_seconds())
    except HTTPException:
        if dl is not None:
            try: dl.close()
            except Exception: pass
        if client is not None:
            try: client.close()
            except Exception: pass
        if not released:
            _PLAYBACK_SEM.release()
        raise
    except Exception as e:
        if dl is not None:
            try: dl.close()
            except Exception: pass
        if client is not None:
            try: client.close()
            except Exception: pass
        if not released:
            _PLAYBACK_SEM.release()
        raise HTTPException(status_code=502, detail=f"playback failed: {e}")
    gen = _ffmpeg_pipe(dl, demux, first, codec, scale, preset, cache,
                       trim=trim, client=client, sem=_PLAYBACK_SEM,
                       stop_at=datetime.strptime(end, _FMT), copy=copy,
                       length=(datetime.strptime(end, _FMT) - datetime.strptime(start, _FMT)).total_seconds())
    return StreamingResponse(gen(), media_type="video/mp4")


# --- export: a time range of one camera as a downloadable MP4 ----------------
# Original stream, no re-encode (cheap on the NAS CPU): walk every NVR file that
# overlaps [start, end), cut on keyframes by the I-frame clock, remux to MP4.
# Runs as a background job because the NVR only streams recorded data at ~1 MB/s.
EXPORT_MAX = 30 * 60          # seconds per export
EXPORT_DIR = CACHE_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
for _f in EXPORT_DIR.iterdir():   # jobs live in memory; files from before a restart are orphans
    _f.unlink(missing_ok=True)
EXPORT_KEEP = 6 * 3600        # finished files are deleted after this
_exports: dict[str, dict[str, Any]] = {}
_export_lock = threading.Lock()   # one export at a time; the rest queue


class ExportReq(BaseModel):
    channel: int
    start: str
    end: str


def _segments(channel: int, start: str, end: str) -> list[dict[str, Any]]:
    d0 = datetime.strptime(start, _FMT).date() - timedelta(days=1)  # past-midnight files
    d1 = datetime.strptime(end, _FMT).date()
    seen, out = set(), []
    d = d0
    while d <= d1:
        for r in _files_for(channel, d.isoformat()):
            if r["begin"] < end and r["end"] > start and r["fileName"] not in seen:
                seen.add(r["fileName"]); out.append(r)
        d += timedelta(days=1)
    return sorted(out, key=lambda r: r["begin"])


def _run_export(job: dict[str, Any]) -> None:
    ch, start, end = job["channel"], job["start"], job["end"]
    t0, t1 = datetime.strptime(start, _FMT), datetime.strptime(end, _FMT)
    total = (t1 - t0).total_seconds()
    es = EXPORT_DIR / f"{job['id']}.hevc"
    out = EXPORT_DIR / f"{job['id']}.mp4"
    with _export_lock, _BG_GATE:
        job["state"] = "running"
        _PLAYBACK_SEM.acquire()
        client = None
        try:
            segs = _segments(ch, start, end)
            if not segs:
                raise RuntimeError("no footage in that range")
            codec = None
            with open(es, "wb") as f:
                client = DVRIPClient(host=CFG.host, port=CFG.port,
                                     username=CFG.username, password=CFG.password)
                client.connect()
                cursor = start   # files can overlap (e.g. after a clock correction): take each moment once
                for rec in segs:
                    s, e = max(cursor, rec["begin"]), min(end, rec["end"])
                    if s >= e:
                        continue
                    cursor = e
                    dl_start, _ = _padded(rec, s)
                    nvr_end = min((datetime.strptime(e, _FMT) + timedelta(seconds=PAD)).strftime(_FMT), rec["end"])
                    demux = sofia.StreamDemux(from_keyframe=True,
                                              start_at=datetime.strptime(s, _FMT),
                                              stop_at=datetime.strptime(e, _FMT))
                    dl = client.stream_recording(rec["fileName"], dl_start, nvr_end, idle_timeout=CLIP_IDLE)
                    try:
                        for body in dl:
                            a = demux.feed(body)
                            if a:
                                codec = codec or sofia.detect_codec(a)
                                f.write(a)
                            t = sofia.last_iframe_time(body)
                            if t:
                                job["progress"] = max(job["progress"], min(0.99, (t - t0).total_seconds() / total))
                            if demux.done:
                                break
                        if not demux.done:
                            f.write(demux.flush())
                    finally:
                        dl.close()
            if not codec or es.stat().st_size < MIN_MP4:
                raise RuntimeError("no video in that range")
            tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
            r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-fflags", "+genpts",
                                "-f", "hevc" if codec == "hevc" else "h264", "-i", str(es),
                                "-c:v", "copy", *tag, "-movflags", "+faststart", str(out)],
                               capture_output=True, timeout=600)
            if r.returncode != 0 or not out.exists():
                raise RuntimeError(f"ffmpeg failed: {r.stderr.decode(errors='replace')[-300:]}")
            job.update(state="done", progress=1.0, size=out.stat().st_size, finished=time.time())
        except Exception as e:
            job.update(state="error", error=str(e), finished=time.time())
            out.unlink(missing_ok=True)
        finally:
            es.unlink(missing_ok=True)
            if client is not None:
                try: client.close()
                except Exception: pass
            _PLAYBACK_SEM.release()


def _prune_exports() -> None:
    now = time.time()
    for jid, j in list(_exports.items()):
        if j.get("finished") and now - j["finished"] > EXPORT_KEEP:
            (EXPORT_DIR / f"{jid}.mp4").unlink(missing_ok=True)
            _exports.pop(jid, None)


@app.post("/api/export")
def export_start(req: ExportReq, request: Request):
    start, end = _norm_time(req.start), _norm_time(req.end)
    span = (datetime.strptime(end, _FMT) - datetime.strptime(start, _FMT)).total_seconds()
    if span <= 0:
        raise HTTPException(status_code=400, detail="end must be after start")
    if span > EXPORT_MAX:
        raise HTTPException(status_code=400, detail=f"max {EXPORT_MAX // 60} minutes per export")
    _prune_exports()
    jid = hashlib.sha1(f"{req.channel}|{start}|{end}|{time.time()}".encode()).hexdigest()[:12]
    name = next((c.name for c in CFG.channels if c.id == req.channel), f"Camera {req.channel}")
    job = {"id": jid, "channel": req.channel, "start": start, "end": end, "state": "queued",
           "progress": 0.0, "error": None, "size": 0,
           "filename": f"{name} {start.replace(':', '.')}.mp4"}
    _exports[jid] = job
    threading.Thread(target=_run_export, args=(job,), daemon=True, name=f"export-{jid}").start()
    accesslog.record(accesslog.client(request), "Export", f"{name} {start[:16]} to {end[11:16]}")
    return job


@app.get("/api/export/{jid}")
def export_status(jid: str):
    job = _exports.get(jid)
    if not job:
        raise HTTPException(status_code=404, detail="unknown export")
    return job


@app.get("/api/export/{jid}/file")
def export_file(jid: str):
    job = _exports.get(jid)
    path = EXPORT_DIR / f"{jid}.mp4"
    if not job or job["state"] != "done" or not path.exists():
        raise HTTPException(status_code=404, detail="export not ready")
    return FileResponse(path, media_type="video/mp4", filename=job["filename"])


@app.get("/api/thumb")
def thumb(channel: int, time: str, scale: int = 320) -> Response:
    """A single JPEG frame grabbed at `time` (for timeline hover + event cards).

    Takes the first COMPLETE keyframe at/after `time`: the download stops as soon
    as that I-frame is fully in, and only that one frame is decoded. Cheap on the
    NAS CPU (no 4K HEVC GOP to chew through) and never half-drawn: the old padded
    window decoded whatever arrived, so a cut download gave a magenta/smeared
    frame. Cached hard; thumbnails for a day are stable.
    """
    time = _norm_time(time)
    cache = _cache_path("thumb", f"{channel}|{time}|{scale}", "jpg")
    if cache.exists() and cache.stat().st_size > 0:
        return FileResponse(cache, media_type="image/jpeg")
    rec = _file_at(channel, time)
    if not rec:
        raise HTTPException(status_code=404, detail="no footage at that time")
    end = min((datetime.strptime(time, _FMT) + timedelta(seconds=THUMB_WINDOW)).strftime(_FMT), rec["end"])
    with _BG_GATE, _PLAYBACK_SEM:
        if cache.exists() and cache.stat().st_size > 0:
            return FileResponse(cache, media_type="image/jpeg")
        try:
            with nvr() as c:
                demux = sofia.StreamDemux(from_keyframe=True)
                es = b""
                dl = c.stream_recording(rec["fileName"], time, end, idle_timeout=CLIP_IDLE)
                try:
                    for body in dl:
                        # feed() only returns frames whose end has been seen, so the
                        # first non-empty output holds a complete I-frame.
                        es = demux.feed(body)
                        if es:
                            break
                    else:
                        es = demux.flush()
                finally:
                    dl.close()
            if not es:
                raise HTTPException(status_code=404, detail="no frame at that time")
            in_fmt = "hevc" if sofia.detect_codec(es) == "hevc" else "h264"
            args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-skip_frame", "nokey",
                    "-f", in_fmt, "-i", "pipe:0", "-frames:v", "1",
                    "-vf", f"scale='min({scale},iw)':-2", "-q:v", "4",
                    "-f", "image2", "pipe:1"]
            proc = subprocess.run(args, input=es, capture_output=True)
            if proc.returncode != 0 or not proc.stdout:
                raise HTTPException(status_code=500, detail="thumb encode failed")
            tmp = cache.with_suffix(".part")
            tmp.write_bytes(proc.stdout)
            tmp.replace(cache)
        except DVRIPError:
            raise HTTPException(status_code=404, detail="no frame at that time")
    return FileResponse(cache, media_type="image/jpeg")


# ============================================================================
#  FACE SORTING  (hybrid: NVR motion events -> frames -> CPU faces -> clusters)
#  First slice: index motion events, detect+embed faces, greedy-cluster into
#  "people", and browse them. Recognition/labels are user-editable.
# ============================================================================
_FRAME_RE = None  # placeholder to keep import list tidy


def _frames_bgr(channel: int, ts: str, secs: int = 15, cap: int = 12) -> list:
    """Decode the KEYFRAMES across a time window as BGR images (one NVR pull).

    A person usually appears only briefly within a motion event, so we sample
    several frames (all keyframes in the window) rather than just the first one.
    -skip_frame nokey both avoids the grey mid-GOP frame and keeps this cheap.
    """
    rec = _file_at(channel, ts)
    if not rec:
        return []
    dl_start, trim = _padded(rec, ts)
    end = min((datetime.strptime(ts, _FMT) + timedelta(seconds=secs)).strftime(_FMT), rec["end"])
    with _BG_GATE, _PLAYBACK_SEM:
        with nvr() as c:
            raw = c.download_recording(rec["fileName"], dl_start, end, expected_bytes=None, idle_timeout=1.2)
    es = sofia.to_annexb(raw) if raw else b""
    if not es:
        return []
    in_fmt = "hevc" if sofia.detect_codec(es) == "hevc" else "h264"
    ss = ["-ss", f"{trim:.3f}"] if trim > 0 else []
    args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-skip_frame", "nokey",
            "-f", in_fmt, "-i", "pipe:0", *ss, "-vsync", "0",
            "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
    proc = subprocess.run(args, input=es, capture_output=True)
    data = proc.stdout or b""
    frames, i = [], 0
    while len(frames) < cap:
        s = data.find(b"\xff\xd8", i)
        if s < 0:
            break
        e = data.find(b"\xff\xd8", s + 2)
        chunk = data[s:e] if e > 0 else data[s:]
        img = cv2.imdecode(np.frombuffer(chunk, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            frames.append(img)
        if e < 0:
            break
        i = e
    return frames


_index_job = {"running": False, "done": 0, "total": 0, "faces": 0, "label": "", "error": ""}
_index_lock = threading.Lock()


def _run_index(dates: list[str]) -> None:
    try:
        faceslib.warm()  # load models up front; surfaces download errors
        events: list[dict] = []
        for d in dates:
            r = recordings(channel="all", date=d, event="motion")
            for day in r["days"]:
                events.extend(day["files"])
        with _index_lock:
            _index_job.update(total=len(events), done=0, faces=0, error="")
        for ev in events:
            ch, ts = ev["channel"], ev["begin"]
            with _index_lock:
                _index_job["label"] = f"{ch} @ {ts}"
            if not faceindex.event_done(ch, ts):
                try:
                    secs = min(max(int(ev.get("duration_s", 10)), 6), 20)
                    kept: list[np.ndarray] = []   # embeddings already stored this event
                    for img in _frames_bgr(ch, ts, secs=secs):
                        h, w = img.shape[:2]
                        for f in faceslib.detect(img, min_score=0.80):
                            x1, y1, x2, y2 = f["bbox"]
                            x1, y1 = max(0, x1), max(0, y1)
                            x2, y2 = min(w, x2), min(h, y2)
                            if x2 - x1 < 32 or y2 - y1 < 32:   # drop tiny/low-quality faces
                                continue
                            emb = f["embedding"]
                            # within-event dedup: skip a face very similar to one we
                            # already saved for this same event (same person, next frame)
                            if any(float(np.dot(emb, k)) > 0.5 for k in kept):
                                continue
                            crop = img[y1:y2, x1:x2]
                            ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
                            if not ok:
                                continue
                            faceindex.add_face(ch, ts, emb, buf.tobytes(), f["score"])
                            kept.append(emb)
                            with _index_lock:
                                _index_job["faces"] += 1
                except Exception:
                    pass
                faceindex.mark_event(ch, ts)
            with _index_lock:
                _index_job["done"] += 1
    except Exception as e:
        with _index_lock:
            _index_job["error"] = str(e)
    finally:
        with _index_lock:
            _index_job["running"] = False
            _index_job["label"] = ""


def _require_faces() -> None:
    if not FACES_OK:
        raise HTTPException(status_code=503,
                            detail=f"face stack unavailable: {_FACE_IMPORT_ERR}")


@app.post("/api/faces/index")
def faces_index(date: str | None = None, date_from: str | None = None,
                date_to: str | None = None) -> dict[str, Any]:
    """Kick off background face indexing of motion events for a day or range."""
    _require_faces()
    with _index_lock:
        if _index_job["running"]:
            return {"started": False, **_index_job}
        if date:
            dates = [date]
        else:
            from datetime import date as _d
            d0 = _d.fromisoformat(date_from or _d.today().isoformat())
            d1 = _d.fromisoformat(date_to or (date_from or _d.today().isoformat()))
            dates = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
        _index_job.update(running=True, done=0, total=0, faces=0, label="", error="")
    threading.Thread(target=_run_index, args=(dates,), daemon=True).start()
    return {"started": True, "dates": dates}


@app.get("/api/faces/status")
def faces_status() -> dict[str, Any]:
    _require_faces()
    with _index_lock:
        job = dict(_index_job)
    return {**job, **faceindex.stats()}


@app.get("/api/faces/people")
def faces_people() -> dict[str, Any]:
    _require_faces()
    return {"people": faceindex.people()}


@app.get("/api/faces/person/{pid}")
def faces_person(pid: int) -> dict[str, Any]:
    _require_faces()
    return {"id": pid, "faces": faceindex.person_faces(pid)}


@app.get("/api/faces/crop/{face_id}")
def faces_crop(face_id: int) -> Response:
    _require_faces()
    p = faceindex.crop_path(face_id)
    if not p or not p.exists():
        raise HTTPException(status_code=404, detail="crop not found")
    return FileResponse(p, media_type="image/jpeg")


class PersonLabel(BaseModel):
    label: str


@app.post("/api/faces/person/{pid}/label")
def faces_label(pid: int, body: PersonLabel) -> dict[str, Any]:
    _require_faces()
    faceindex.rename_person(pid, body.label.strip())
    return {"ok": True}


# Config sections to offer in the editor, grouped for the UI. Probed against the
# device on first request; only supported ones are shown.
CONFIG_CATALOG: dict[str, list[str]] = {
    "System": ["General.General", "General.Location", "General.AutoMaintain",
               "General.PwdSafety", "System.TimeZone"],
    "Display": ["fVideo.GUISet", "AVEnc.VideoWidget", "AVEnc.VideoColor"],
    "Encoding": ["Simplify.Encode", "AVEnc.EncodeStaticParam", "AVEnc.MultiChannelEncode"],
    "Detection": ["Detect.MotionDetect", "Detect.BlindDetect", "Detect.LossDetect",
                  "Detect.FaceDetection"],
    "Recording": ["Record"],
    "Network": ["NetWork.NetCommon", "NetWork.Wifi", "NetWork.OnvifPwdCheckout"],
    "Status": ["Status.NatInfo"],
}
_sections_cache: dict[str, Any] | None = None


@app.get("/api/config-sections")
def config_sections() -> dict[str, Any]:
    """Probe which catalog sections this device supports; cache the result."""
    global _sections_cache
    if _sections_cache is not None:
        return _sections_cache
    groups: list[dict[str, Any]] = []
    with nvr() as c:
        for group, names in CONFIG_CATALOG.items():
            supported = []
            for name in names:
                try:
                    r = c._cmd(1042, {"Name": name, "SessionID": c._sid_str()})
                    if name in r and str(r.get("Ret")) == "100":
                        supported.append(name)
                except DVRIPError:
                    continue
            if supported:
                groups.append({"group": group, "sections": supported})
    _sections_cache = {"groups": groups}
    return _sections_cache


@app.get("/api/config/{name}")
def get_config(name: str) -> JSONResponse:
    with nvr() as c:
        return JSONResponse({"name": name, "value": c.get_config(name)})


class ConfigWrite(BaseModel):
    value: Any


@app.post("/api/config/{name}")
def set_config(name: str, body: ConfigWrite) -> dict[str, Any]:
    """Write a config section back. Send the full section object (as returned by
    GET) with edits applied; DVRIP replaces the whole section."""
    with nvr() as c:
        resp = c.set_config(name, body.value)
    ret = resp.get("Ret")
    if str(ret) not in ("100",):
        raise HTTPException(status_code=400, detail=f"device rejected config: Ret={ret}")
    _invalidate = _sections_cache  # sections unchanged, value changed
    return {"name": name, "ok": True, "ret": ret}


class MotionToggle(BaseModel):
    channel: int
    enabled: bool


@app.get("/api/motion")
def get_motion(channel: int | None = None) -> JSONResponse:
    with nvr() as c:
        return JSONResponse({"MotionDetect": c.motion_config(channel)})


@app.post("/api/motion")
def set_motion(body: MotionToggle) -> dict[str, Any]:
    with nvr() as c:
        resp = c.set_motion_enabled(body.channel, body.enabled)
    return {"channel": body.channel, "enabled": body.enabled, "result": resp}


# --- two-way talk / intercom ------------------------------------------------
# Only one talk session can run at a time (the NVR routes talk through a single
# intercom path); this lock serializes sound playback and push-to-talk.
_TALK_LOCK = threading.Lock()
_TALK_FRAME = 320       # G.711a bytes per sofia frame = 40 ms @ 8 kHz
_TALK_PACE = 0.04       # seconds per frame, real-time


class TalkSession:
    """Owns a DVRIP connection with the across-IPC talk channel open to one
    camera, and drains the device->client downlink so the socket never
    backpressures while we push audio up. Use as a context manager."""

    def __init__(self, channel: int) -> None:
        self.channel = channel
        self.client = DVRIPClient(host=CFG.host, port=CFG.port,
                                  username=CFG.username, password=CFG.password)
        self._drain_stop = False
        self._drain_thread: threading.Thread | None = None

    def __enter__(self) -> "TalkSession":
        self.client.connect()
        self.client.talk_open(self.channel)
        self._drain_thread = threading.Thread(target=self._drain, daemon=True)
        self._drain_thread.start()
        return self

    def _drain(self) -> None:
        sock = self.client._sock
        if sock is None:
            return
        sock.settimeout(0.4)
        while not self._drain_stop:
            try:
                if not sock.recv(65536):
                    break
            except socket.timeout:
                continue
            except OSError:
                break

    def send_alaw(self, alaw: bytes, pace: bool = True) -> None:
        """Send G.711 a-law audio. When `pace`, send 40 ms frames in real time
        (for pre-rendered clips); push-to-talk passes pace=False because the
        browser already delivers audio at capture speed."""
        if not pace:
            self.client.talk_send(alaw)
            return
        t0 = time.time()
        for i in range(0, len(alaw), _TALK_FRAME):
            self.client.talk_send(alaw[i:i + _TALK_FRAME])
            target = t0 + (i / _TALK_FRAME + 1) * _TALK_PACE
            dt = target - time.time()
            if dt > 0:
                time.sleep(dt)

    def __exit__(self, *exc: Any) -> None:
        self._drain_stop = True
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2)
        try:
            self.client.talk_close()
        finally:
            self.client.close()


class TalkSound(BaseModel):
    channel: int
    sound: str = "halloween"


@app.get("/api/talk/sounds")
def talk_sounds() -> dict[str, Any]:
    """The current season (emoji + its sounds) for the live-view sound button,
    plus every sound id that can be played explicitly."""
    return {"season": seasons.current(),
            "sounds": [{"id": k, "label": seasons.label_of(k)} for k in seasons.all_ids()]}


@app.post("/api/talk/sound")
def talk_sound(body: TalkSound, request: Request) -> dict[str, Any]:
    """Play a sound out of one camera's speaker (across-IPC talk). sound='season'
    plays a random sound of the current season."""
    try:
        chosen, alaw = seasons.render(body.sound)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown sound {body.sound!r}")
    accesslog.record(accesslog.client(request), "Played sound", f"{_cam(body.channel)}: {seasons.label_of(chosen)}")
    if not _TALK_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="talk is busy on another camera")
    try:
        with TalkSession(body.channel) as talk:
            talk.send_alaw(alaw, pace=True)
    except DVRIPError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=504, detail=f"NVR unreachable: {e}")
    finally:
        _TALK_LOCK.release()
    return {"ok": True, "channel": body.channel, "sound": chosen,
            "label": seasons.label_of(chosen), "seconds": round(len(alaw) / 8000, 1)}


@app.websocket("/api/talk/ws/{channel}")
async def talk_ws(ws: WebSocket, channel: int) -> None:
    """Live push-to-talk. The browser streams little-endian 16-bit mono PCM @
    8 kHz as binary frames; we a-law encode each and push it to the camera.
    Holds the talk session for the life of the socket."""
    await ws.accept()
    accesslog.record(accesslog.client(ws), "Talk", _cam(channel))
    if not _TALK_LOCK.acquire(blocking=False):
        await ws.close(code=1013, reason="talk busy")   # try again later
        return
    talk: TalkSession | None = None
    try:
        talk = await asyncio.to_thread(lambda: TalkSession(channel).__enter__())
        await ws.send_json({"ok": True, "channel": channel})
        while True:
            pcm = await ws.receive_bytes()
            if pcm:
                alaw = audiogen.pcm16le_to_alaw(pcm)
                await asyncio.to_thread(talk.send_alaw, alaw, False)
    except WebSocketDisconnect:
        pass
    except (DVRIPError, OSError) as e:
        try:
            await ws.close(code=1011, reason=str(e)[:100])
        except RuntimeError:
            pass
    finally:
        if talk is not None:
            await asyncio.to_thread(talk.__exit__, None, None, None)
        _TALK_LOCK.release()
