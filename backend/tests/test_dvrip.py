import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dvrip import DVRIPClient, sofia_hash, HEADER_MAGIC  # noqa: E402


def test_sofia_hash_known_vectors():
    # The canonical empty-password hash for Xiongmai devices.
    assert sofia_hash("") == "tlJwpbo6"
    # Hash is always 8 chars from the known alphabet, deterministic.
    h = sofia_hash("admin")
    assert len(h) == 8
    assert sofia_hash("admin") == h
    assert sofia_hash("password1") != sofia_hash("password2")


def test_header_framing_roundtrip():
    c = DVRIPClient(host="192.0.2.1", username="admin", password="")
    c._session = 0x1A2B3C4D
    c._seq = 7
    pkt = c._build(1042, {"Name": "Detect.MotionDetect", "SessionID": "0x1A2B3C4D"})
    assert pkt[0] == HEADER_MAGIC
    session, seq = struct.unpack_from("<II", pkt, 4)
    msgid, length = struct.unpack_from("<HI", pkt, 14)
    assert session == 0x1A2B3C4D
    assert seq == 7
    assert msgid == 1042
    body = pkt[20:]
    assert len(body) == length
    assert body.endswith(b"\x00")
    assert b'"Name":"Detect.MotionDetect"' in body


def test_sid_str_format():
    c = DVRIPClient(host="192.0.2.1")
    c._session = 0xABCD
    assert c._sid_str() == "0x0000ABCD"


def test_media_chunk_starting_with_brace_is_not_status():
    from dvrip import _is_status
    assert _is_status(b'{"Ret": 100}\x00')
    assert not _is_status(b"\x7b\xe8\xb2\x11\x67\xca")  # real 32 KB chunk head
    assert not _is_status(b"\x00\x00\x01\xfd")


def _iframe(v: int, payload: bytes) -> bytes:
    return b"\x00\x00\x01\xfc\x53\x0a\xe0\x0e" + struct.pack("<I", v) + b"\x00" * 4 + payload


def test_demux_from_keyframe_drops_orphan_p_frames_and_reads_time():
    import sofia
    from datetime import datetime
    v = struct.unpack("<I", bytes.fromhex("8297ba6a"))[0]   # 2026-10-29 09:30:02
    p = b"\x00\x00\x01\xfd" + b"\x00" * 4 + b"\x00\x00\x00\x01\x02\x01PP"
    i = _iframe(v, b"\x00\x00\x00\x01\x26\x01II")
    d = sofia.StreamDemux(from_keyframe=True)
    out = d.feed(p + p + i + p) + d.flush()
    assert out == b"\x00\x00\x00\x01\x26\x01II" + b"\x00\x00\x00\x01\x02\x01PP"
    assert d.key_time == datetime(2026, 10, 29, 9, 30, 2)
    # default keeps everything (thumb/face paths rely on that)
    d2 = sofia.StreamDemux()
    assert (d2.feed(p + i) + d2.flush()).startswith(b"\x00\x00\x00\x01\x02\x01PP")


def test_last_iframe_time_ignores_truncated_header():
    import sofia
    from datetime import datetime
    v = struct.unpack("<I", bytes.fromhex("8297ba6a"))[0]
    blob = _iframe(v, b"xx") + b"\x00\x00\x01\xfc\x53\x0a"   # 2nd header cut off
    assert sofia.last_iframe_time(blob) == datetime(2026, 10, 29, 9, 30, 2)
    assert sofia.last_iframe_time(b"\x00\x00\x01\xfd") is None


def _stamp(sec: int) -> int:
    # 2026-10-29 09:30:<sec> in the sofia I-frame clock layout
    return (26 << 26) | (10 << 22) | (29 << 17) | (9 << 12) | (30 << 6) | sec


def test_demux_keyframe_cuts_tile_consecutive_chunks():
    import sofia
    from datetime import datetime
    at = lambda s: datetime(2026, 10, 29, 9, 30, s)
    p = lambda n: b"\x00\x00\x01\xfd" + b"\x00" * 4 + b"\x00\x00\x00\x01\x02\x01P" + bytes([n])
    gop = lambda s: _iframe(_stamp(s), b"\x00\x00\x00\x01\x26\x01I" + bytes([s])) + p(s) + p(s)
    stream = p(99) + b"".join(gop(s) for s in (0, 2, 4, 6, 8))

    def cut(a, b):
        d = sofia.StreamDemux(from_keyframe=True, start_at=at(a), stop_at=at(b))
        return d.feed(stream) + d.flush()

    whole, first, second = cut(1, 7), cut(1, 4), cut(4, 7)
    assert first + second == whole                 # no gap, no overlap
    assert whole.startswith(b"\x00\x00\x00\x01\x26\x01I\x02")   # first I-frame >= :01
    assert b"I\x06" in whole and b"I\x08" not in whole   # cut before the :08 I-frame


def _paged_client(pages):
    """Client whose _search_page answers from a list of fake NVR pages."""
    c = DVRIPClient(host="x", port=1, username="u", password="p")
    calls = []

    def fake(channel, begin, end, stream="Main", filetype="h264"):
        calls.append(begin)
        return pages[len(calls) - 1] if len(calls) <= len(pages) else []
    c._search_page = fake
    return c, calls


def _files(start_min, n):
    out = []
    for i in range(start_min, start_min + n):
        hh, mm = divmod(i, 60)
        out.append({"FileName": f"/idea0/{i}.h264",
                    "BeginTime": f"2026-11-19 {hh:02d}:{mm:02d}:00",
                    "EndTime": f"2026-11-19 {hh:02d}:{mm:02d}:30"})
    return out


DAY = ("2026-11-19 00:00:00", "2026-11-19 23:59:59")


def test_search_pages_past_the_cap_and_dedupes():
    from dvrip import SEARCH_PAGE_CAP
    p1 = _files(0, SEARCH_PAGE_CAP)
    p2 = _files(SEARCH_PAGE_CAP - 1, 10)      # the NVR repeats the boundary file
    c, calls = _paged_client([p1, p2])
    got = c.search_recordings(0, *DAY)
    assert len(got) == SEARCH_PAGE_CAP + 9
    assert len({f["FileName"] for f in got}) == len(got)
    assert calls == [DAY[0], p1[-1]["BeginTime"]]


def test_search_short_page_is_one_query():
    c, calls = _paged_client([_files(0, 5)])
    assert len(c.search_recordings(0, *DAY)) == 5
    assert len(calls) == 1


def test_search_stops_when_a_page_brings_nothing_new():
    from dvrip import SEARCH_PAGE_CAP
    p = _files(0, SEARCH_PAGE_CAP)
    c, calls = _paged_client([p, p, p])
    assert len(c.search_recordings(0, *DAY)) == SEARCH_PAGE_CAP
    assert len(calls) == 2


def _wire_client(reply):
    """Client with a fake socket whose OPFileQuery answer is `reply`."""
    class Sock:
        def sendall(self, _):
            pass
    c = DVRIPClient(host="x", port=1, username="u", password="p")
    c._sock = Sock()
    c._recv = lambda *a, **k: (1441, reply)
    return c


def test_search_ret_119_means_no_files():
    assert _wire_client({"Ret": 119}).search_recordings(1, *DAY) == []


def test_search_other_errors_still_raise():
    import pytest
    from dvrip import DVRIPError
    with pytest.raises(DVRIPError):
        _wire_client({"Ret": 103}).search_recordings(1, *DAY)


def test_search_long_file_does_not_skip_shorter_ones():
    """A long file on a full page must not push the cursor past clips that start
    before it ends (cursor = latest BeginTime, not EndTime)."""
    from dvrip import SEARCH_PAGE_CAP
    p1 = _files(0, SEARCH_PAGE_CAP)
    p1[10] = dict(p1[10], EndTime="2026-11-19 23:00:00")   # a long continuous file
    c, calls = _paged_client([p1, _files(SEARCH_PAGE_CAP - 1, 3)])
    c.search_recordings(0, *DAY)
    assert calls[1] == p1[-1]["BeginTime"]


def test_search_keeps_partial_list_when_a_later_page_fails():
    from dvrip import SEARCH_PAGE_CAP, DVRIPError
    p1 = _files(0, SEARCH_PAGE_CAP)
    c = DVRIPClient(host="x", port=1, username="u", password="p")
    answers = iter([p1])

    def fake(*a, **k):
        try:
            return next(answers)
        except StopIteration:
            raise DVRIPError("file search failed: Ret=108")
    c._search_page = fake
    assert len(c.search_recordings(0, *DAY)) == SEARCH_PAGE_CAP


def test_search_first_page_error_still_raises():
    import pytest
    from dvrip import DVRIPError
    c = DVRIPClient(host="x", port=1, username="u", password="p")

    def fake(*a, **k):
        raise DVRIPError("file search failed: Ret=103")
    c._search_page = fake
    with pytest.raises(DVRIPError):
        c.search_recordings(0, *DAY)


def test_page_cap_from_env(monkeypatch):
    import dvrip
    monkeypatch.setenv("UEYE_SEARCH_PAGE_CAP", "32")
    assert dvrip._env_page_cap() == 32
    for bad in ("0", "-5", "abc"):
        monkeypatch.setenv("UEYE_SEARCH_PAGE_CAP", bad)
        assert dvrip._env_page_cap() == 64
    monkeypatch.delenv("UEYE_SEARCH_PAGE_CAP")
    assert dvrip._env_page_cap() == 64


def test_smaller_cap_pages_when_configured(monkeypatch):
    import dvrip
    monkeypatch.setattr(dvrip, "_page_cap", 32)
    c, calls = _paged_client([_files(0, 32), _files(31, 5)])
    assert len(c.search_recordings(0, *DAY)) == 36
    assert len(calls) == 2


def test_cap_is_learned_upward(monkeypatch):
    import dvrip
    monkeypatch.setattr(dvrip, "_page_cap", 64)
    c, calls = _paged_client([_files(0, 128), _files(127, 5)])
    assert len(c.search_recordings(0, *DAY)) == 132
    assert dvrip._page_cap == 128
