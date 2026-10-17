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
