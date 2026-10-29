"""Demux Xiongmai 'sofia' frame format to an Annex-B elementary stream.

Recordings download as sofia-framed data: each frame is `00 00 01 TT` + header
+ payload. Video I-frames (0xFC) have a 16-byte header, P-frames (0xFD) an
8-byte header; audio (0xFA) and other types are skipped. The video payloads are
raw H.264/H.265 Annex-B NALs, so concatenating them yields a muxable stream.
We slice each frame from its header to the next marker (robust to length-field
quirks) rather than trusting the per-frame length.
"""
from __future__ import annotations

import struct
from datetime import datetime

VIDEO_HEADER = {0xFC: 16, 0xFD: 8}          # I-frame, P-frame
SKIP = {0xFA, 0xFB, 0xFE, 0xF9, 0xF8, 0xF0}  # audio / other
_MARKERS = set(VIDEO_HEADER) | SKIP


def to_annexb(raw: bytes) -> bytes:
    n = len(raw)
    marks: list[int] = []
    i = 0
    while i + 4 <= n:
        if raw[i:i + 3] == b"\x00\x00\x01" and raw[i + 3] in _MARKERS:
            marks.append(i)
            i += 4
        else:
            i += 1
    marks.append(n)
    out = bytearray()
    for k in range(len(marks) - 1):
        o, nxt = marks[k], marks[k + 1]
        hl = VIDEO_HEADER.get(raw[o + 3])
        if hl is not None:
            out += raw[o + hl:nxt]
    return bytes(out)


class StreamDemux:
    """Incremental version of to_annexb for a live download. Feed it sofia-framed
    bytes as they arrive; it returns the Annex-B for every frame whose end is now
    known (i.e. up to the last complete marker), and keeps the trailing partial
    frame buffered until the next marker arrives. flush() emits the final frame.

    With from_keyframe, frames before the first I-frame are dropped: a ByName
    sub-range starts mid-GOP, and those orphan P-frames only decode to errors (and
    Debian's ffmpeg 7 still emits them as smeared frames)."""

    def __init__(self, from_keyframe: bool = False) -> None:
        self.buf = bytearray()
        self.synced = not from_keyframe
        self.key_time: datetime | None = None  # wall clock of the first I-frame

    def _emit(self, final: bool) -> bytes:
        buf = self.buf
        n = len(buf)
        marks: list[int] = []
        i = 0
        while i + 4 <= n:
            if buf[i:i + 3] == b"\x00\x00\x01" and buf[i + 3] in _MARKERS:
                marks.append(i)
                i += 4
            else:
                i += 1
        if not marks:
            return b""
        # Without `final`, the last marker's frame isn't complete yet (its payload
        # runs to the NEXT marker, not yet seen), so hold it back.
        last = len(marks) if final else len(marks) - 1
        out = bytearray()
        for k in range(last):
            o = marks[k]
            nxt = marks[k + 1] if k + 1 < len(marks) else n
            hl = VIDEO_HEADER.get(buf[o + 3])
            if buf[o + 3] == 0xFC and self.key_time is None:
                self.synced = True
                self.key_time = iframe_time(buf[o:o + 16])
            if hl is not None and self.synced:
                out += buf[o + hl:nxt]
        self.buf = bytearray() if final else bytearray(buf[marks[-1]:])
        return bytes(out)

    def feed(self, data: bytes) -> bytes:
        self.buf += data
        return self._emit(final=False)

    def flush(self) -> bytes:
        return self._emit(final=True)


def iframe_time(hdr: bytes) -> datetime | None:
    """Wall-clock time packed into a sofia I-frame header (bytes 8-11, LE):
    year-2000:6 | month:4 | day:5 | hour:5 | minute:6 | second:6. One-second
    resolution; verified against the NVR's OSD clock."""
    if len(hdr) < 12:
        return None
    v = struct.unpack_from("<I", hdr, 8)[0]
    try:
        return datetime((v >> 26) + 2000, (v >> 22) & 15, (v >> 17) & 31,
                        (v >> 12) & 31, (v >> 6) & 63, v & 63)
    except ValueError:
        return None


def last_iframe_time(data: bytes) -> datetime | None:
    """Time of the last I-frame header fully inside data, or None."""
    i = data.rfind(b"\x00\x00\x01\xfc")
    while i >= 0 and i + 12 > len(data):
        i = data.rfind(b"\x00\x00\x01\xfc", 0, i)
    return iframe_time(data[i:i + 16]) if i >= 0 else None


def detect_codec(annexb: bytes) -> str:
    """Best-effort: 'hevc' or 'h264' from the first NAL after a start code."""
    i = annexb.find(b"\x00\x00\x00\x01")
    if i < 0:
        i = annexb.find(b"\x00\x00\x01")
        off = i + 3
    else:
        off = i + 4
    if i < 0 or off >= len(annexb):
        return "hevc"
    nal = annexb[off]
    # H.264 NAL: bits 1-5 type in low bits of 1 byte; HEVC: type in (byte>>1)&0x3f
    h264_type = nal & 0x1F
    if h264_type in (7, 5, 1, 8):  # SPS/IDR/non-IDR/PPS
        return "h264"
    return "hevc"
