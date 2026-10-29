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
