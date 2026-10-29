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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, Response, FileResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import AppConfig  # noqa: E402
from dvrip import DVRIPClient, DVRIPError  # noqa: E402
import audiogen  # noqa: E402
import sofia  # noqa: E402

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
CFG = AppConfig.load()

# The NVR allows only a few concurrent playback streams. Cap how many ByTime
# pulls (clips + thumbnails) we run at once so a wall of lazy-loading thumbnails
# can't starve the device or step on live view.
_PLAYBACK_SEM = threading.Semaphore(2)

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
                 cache: Path, trim: float, client, sem, stop_at: datetime | None = None):
    """Transcode a LIVE download to fragmented MP4. `first` is the already-demuxed
    opening NALs (used to pick the input format); the rest is demuxed from `dl` as
    it streams in and fed to ffmpeg, so output starts without waiting for the whole
    slice. Owns the NVR client and the concurrency slot: both are released when the
    stream ends or the client disconnects. With stop_at, the download ends at the
    first I-frame stamped at/after it instead of waiting for the idle timeout."""
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
    # The NVR stops sending if we fall behind, and the idle timeout then reads
    # that as end-of-stream. 4K HEVC decodes slower than real time on the NAS, so
    # writing to ffmpeg must never stall the socket: drain the download in its own
    # thread into an unbounded queue (a 20s chunk is ~10 MB) and feed from there.
    q: queue.Queue = queue.Queue()

    def pull():
        tail = b""
        try:
            for body in dl:
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

    threading.Thread(target=pull, daemon=True).start()
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
            # A header-only MP4 (~800 B) means no frame made it; don't cache that.
            if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > MIN_MP4:
                tmp.replace(cache)
            else:
                tmp.unlink(missing_ok=True)
        finally:
            if not f.closed:
                f.close()
            if proc.poll() is None:
                proc.kill()
            sem.release()
    return gen


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


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "nvr": f"{CFG.host}:{CFG.port}",
            "channels": [{"id": c.id, "name": c.name} for c in CFG.channels]}


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
    scale, preset = qp["scale"], qp["preset"]
    end = (datetime.strptime(start, _FMT) + timedelta(seconds=duration)).strftime(_FMT)
    cache = _cache_path("clip", f"{channel}|{start}|{duration}|{transcode}|{q}", "mp4")
    if cache.exists() and cache.stat().st_size > 0:
        return FileResponse(cache, media_type="video/mp4")
    rec = _file_at(channel, start)        # ByName selects the right channel
    if not rec:
        raise HTTPException(status_code=404, detail="no footage at that time")
    end = min(end, rec["end"])            # clamp the slice to the covering file
    dl_start, trim = _padded(rec, start)  # pad start so the first GOP is clean
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
        dl = client.stream_recording(rec["fileName"], dl_start, end, idle_timeout=CLIP_IDLE)
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
        if demux.key_time:                # output starts at the first keyframe
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
                       stop_at=datetime.strptime(end, _FMT))
    return StreamingResponse(gen(), media_type="video/mp4")


@app.get("/api/thumb")
def thumb(channel: int, time: str, scale: int = 320) -> Response:
    """A single JPEG frame grabbed at `time` (for timeline hover + event cards).

    Pulls a ~2s ByTime window and extracts the first decodable frame. Cached hard;
    thumbnails for a day are stable so this is a one-time cost per moment.
    """
    time = _norm_time(time)
    cache = _cache_path("thumb", f"{channel}|{time}|{scale}", "jpg")
    if cache.exists() and cache.stat().st_size > 0:
        return FileResponse(cache, media_type="image/jpeg")
    rec = _file_at(channel, time)
    if not rec:
        raise HTTPException(status_code=404, detail="no footage at that time")
    dl_start, trim = _padded(rec, time)
    end = min((datetime.strptime(time, _FMT) + timedelta(seconds=2)).strftime(_FMT), rec["end"])
    with _PLAYBACK_SEM:
        if cache.exists() and cache.stat().st_size > 0:
            return FileResponse(cache, media_type="image/jpeg")
        try:
            with nvr() as c:
                raw = c.download_recording(rec["fileName"], dl_start, end, expected_bytes=None, idle_timeout=1.2)
            es = sofia.to_annexb(raw) if raw else b""
            if not es:
                raise HTTPException(status_code=404, detail="no frame at that time")
            codec = sofia.detect_codec(es)
            in_fmt = "hevc" if codec == "hevc" else "h264"
            # Pad earlier + output -ss trim so the decoder warms up on a complete
            # keyframe and we emit a clean frame at the requested moment (a raw
            # sub-range boundary otherwise yields a smeared/partial first keyframe).
            ss = ["-ss", f"{trim:.3f}"] if trim > 0 else []
            args = ["ffmpeg", "-nostdin", "-loglevel", "error",
                    "-f", in_fmt, "-i", "pipe:0", *ss, "-frames:v", "1",
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
    with _PLAYBACK_SEM:
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
    return {"sounds": [{"id": k, "label": v[0]} for k, v in audiogen.SOUNDS.items()]}


@app.post("/api/talk/sound")
def talk_sound(body: TalkSound) -> dict[str, Any]:
    """Play a built-in sound out of one camera's speaker (across-IPC talk)."""
    entry = audiogen.SOUNDS.get(body.sound)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown sound {body.sound!r}")
    alaw = entry[1]()
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
    return {"ok": True, "channel": body.channel, "sound": body.sound,
            "seconds": round(len(alaw) / 8000, 1)}


@app.websocket("/api/talk/ws/{channel}")
async def talk_ws(ws: WebSocket, channel: int) -> None:
    """Live push-to-talk. The browser streams little-endian 16-bit mono PCM @
    8 kHz as binary frames; we a-law encode each and push it to the camera.
    Holds the talk session for the life of the socket."""
    await ws.accept()
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
