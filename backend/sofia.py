"""Demux Xiongmai 'sofia' frame format to an Annex-B elementary stream.

Recordings download as sofia-framed data: each frame is `00 00 01 TT` + header
+ payload. Video I-frames (0xFC) have a 16-byte header, P-frames (0xFD) an
8-byte header; audio (0xFA) and other types are skipped. The video payloads are
raw H.264/H.265 Annex-B NALs, so concatenating them yields a muxable stream.
We slice each frame from its header to the next marker (robust to length-field
quirks) rather than trusting the per-frame length.
"""
from __future__ import annotations

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
    frame buffered until the next marker arrives. flush() emits the final frame."""

    def __init__(self) -> None:
        self.buf = bytearray()

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
            if hl is not None:
                out += buf[o + hl:nxt]
        self.buf = bytearray() if final else bytearray(buf[marks[-1]:])
        return bytes(out)

    def feed(self, data: bytes) -> bytes:
        self.buf += data
        return self._emit(final=False)

    def flush(self) -> bytes:
        return self._emit(final=True)


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
