"""
Minimal DVRIP (Sofia / NETsurveillance) client for Xiongmai-family NVRs/DVRs.

This talks the same LAN protocol the XMEye app's native libFunSDK.so speaks
(TCP 34567): a 20-byte binary header followed by a NUL-terminated JSON payload.
It is a clean-room reimplementation of the openly documented protocol, used to
drive *your own* recorder for live metadata, recording search/playback and
config (camera + motion detection). No vendor cloud / P2P is involved.

Protocol reference confirmed against the app's own symbols via REA:
  DevLogin / DevGetConfigJson / DevSetConfigJson / DevCmdGeneral / MediaPlayBack
and the login-hash alphabet found verbatim in libFunSDK.so.
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Any

# --- message ids (QCODES) we actually use -----------------------------------
LOGIN_REQ = 1000
LOGIN_RSP = 1000
LOGOUT_REQ = 1001
KEEPALIVE_REQ = 1006
SYSINFO_REQ = 1020
CONFIG_SET = 1040
CONFIG_SET_RSP = 1041
CONFIG_GET = 1042
CONFIG_GET_RSP = 1043
ABILITY_GET = 1360
PTZ_REQ = 1400
FILESEARCH_REQ = 1440          # OPFileQuery
FILESEARCH_RSP = 1441
PLAY_REQ = 1420                # OPPlayBack (playback / download by time)
PLAY_RSP = 1421
TALK_CLAIM = 1434              # OPTalk Claim (open the talk-out / intercom channel)
TALK_CLAIM_RSP = 1435
TALK_REQ = 1430               # OPTalk AcrossStart/AcrossStop (begin/end talk)
TALK_DATA = 1432             # TALK_CU_PU_DATA: audio frames client -> device speaker
# NB: 1436 is TALK_PU_CU_DATA (device -> client) and rejects sends (Ret 102).
GUARD_REQ = 1500
UNGUARD_REQ = 1502
ALARM_REQ = 1504               # alarm channel (motion events)

HEADER_MAGIC = 0xFF
HEADER_LEN = 20

_HASH_ALPHABET = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)  # standard base62: n%62 -> '0'-'9','A'-'Z','a'-'z'


def sofia_hash(password: str) -> str:
    """The Xiongmai 'sofia' login hash: 8 chars derived from the MD5 of the
    password. The alphabet below appears verbatim in libFunSDK.so.
    Known vector: sofia_hash("") == "tlJwpbo6".
    """
    md5 = hashlib.md5(password.encode("utf-8")).digest()
    out = []
    for i in range(8):
        n = (md5[2 * i] + md5[2 * i + 1]) % 62
        out.append(_HASH_ALPHABET[n])
    return "".join(out)


class DVRIPError(RuntimeError):
    pass


def _is_status(body: bytes) -> bool:
    """True for a JSON status reply on the media msgid. Media chunks are arbitrary
    slices of the byte stream, so ~1 in 256 starts with b"{" by chance; checking
    the first byte alone dropped those and corrupted the clip until the next GOP."""
    if body[:1] != b"{":
        return False
    try:
        json.loads(body.rstrip(b"\x00"))
        return True
    except ValueError:
        return False


@dataclass
class DVRIPClient:
    host: str
    port: int = 34567
    username: str = "admin"
    password: str = ""
    timeout: float = 10.0

    _sock: socket.socket | None = field(default=None, repr=False)
    _session: int = 0
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- framing -------------------------------------------------------------
    def _build(self, msgid: int, payload: dict[str, Any] | None) -> bytes:
        body = b""
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\x00"
        # 0xff | ver | 0 | 0 | session(4 LE) | seq(4 LE) | 0 | 0 | msgid(2 LE) | len(4 LE)
        header = struct.pack(
            "<BBBBIIBBHI",
            HEADER_MAGIC, 0, 0, 0,
            self._session, self._seq,
            0, 0,
            msgid, len(body),
        )
        return header + body

    def _build_raw(self, msgid: int, body: bytes) -> bytes:
        """Frame a RAW binary body (no JSON, no trailing NUL) — used for talk audio."""
        header = struct.pack(
            "<BBBBIIBBHI",
            HEADER_MAGIC, 0, 0, 0,
            self._session, self._seq,
            0, 0,
            msgid, len(body),
        )
        return header + body

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise DVRIPError("connection closed by device")
            buf += chunk
        return buf

    def _recv(self) -> tuple[int, dict[str, Any]]:
        head = self._recv_exact(HEADER_LEN)
        if head[0] != HEADER_MAGIC:
            raise DVRIPError(f"bad header magic {head[0]:#x}")
        session, _seq = struct.unpack_from("<II", head, 4)
        msgid, length = struct.unpack_from("<HI", head, 14)
        self._session = session
        body = self._recv_exact(length) if length else b""
        body = body.rstrip(b"\x00")
        data: dict[str, Any] = {}
        if body:
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                data = {"_raw": body.decode("latin-1")}
        return msgid, data

    def _cmd(self, msgid: int, payload: dict[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            assert self._sock is not None, "not connected"
            self._sock.sendall(self._build(msgid, payload))
            self._seq += 1
            _rid, data = self._recv()
            return data

    # --- lifecycle -----------------------------------------------------------
    def connect(self) -> dict[str, Any]:
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        self._session = 0
        self._seq = 0
        resp = self._cmd(LOGIN_REQ, {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "UserName": self.username,
            "PassWord": sofia_hash(self.password),
        })
        if str(resp.get("Ret")) not in ("100", "515"):  # 100 = OK
            raise DVRIPError(f"login failed: {resp!r}")
        sid = resp.get("SessionID")
        if isinstance(sid, str):
            self._session = int(sid, 16)
        return resp

    def close(self) -> None:
        if self._sock:
            try:
                self._cmd(LOGOUT_REQ, {"Name": "", "SessionID": self._sid_str()})
            except Exception:
                pass
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _sid_str(self) -> str:
        return f"0x{self._session:08X}"

    def __enter__(self) -> "DVRIPClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- high-level operations ----------------------------------------------
    def keepalive(self) -> dict[str, Any]:
        return self._cmd(KEEPALIVE_REQ, {"Name": "KeepAlive", "SessionID": self._sid_str()})

    def system_info(self) -> dict[str, Any]:
        return self._cmd(SYSINFO_REQ, {"Name": "SystemInfo", "SessionID": self._sid_str()})

    def get_config(self, name: str) -> dict[str, Any]:
        """Read a config section, e.g. 'Detect.MotionDetect', 'Simplify.Encode',
        'General.General', 'Camera.Param', 'fVideo.OSD'."""
        resp = self._cmd(CONFIG_GET, {"Name": name, "SessionID": self._sid_str()})
        return resp.get(name, resp)

    def set_config(self, name: str, value: Any) -> dict[str, Any]:
        """Write a config section back. `value` is the section body as returned
        by get_config()."""
        return self._cmd(CONFIG_SET, {"Name": name, "SessionID": self._sid_str(), name: value})

    def channel_count(self) -> int:
        info = self.system_info().get("SystemInfo", {})
        return int(info.get("VideoInChannel", 1) or 1)

    def search_recordings(
        self, channel: int, begin: str, end: str,
        stream: str = "Main", filetype: str = "h264",
    ) -> list[dict[str, Any]]:
        """List recordings in [begin, end] (strings 'YYYY-MM-DD HH:MM:SS').

        The file list arrives in a follow-up 1441 packet, not the request ack,
        so we read until we see the OPFileQuery array (or an error Ret).
        `stream` is a label kept for the API; the wire StreamType is a mask.
        """
        stream_mask = "0x00000001" if stream.lower() == "sub" else "0x00000000"
        with self._lock:
            assert self._sock is not None, "not connected"
            self._sock.sendall(self._build(FILESEARCH_REQ, {
                "Name": "OPFileQuery",
                "SessionID": self._sid_str(),
                "OPFileQuery": {
                    "BeginTime": begin, "EndTime": end,
                    "Channel": channel, "Type": filetype,
                    "Event": "*", "StreamType": stream_mask,
                    "DriveTypeMask": "0x0000FFFF",
                },
            }))
            self._seq += 1
            for _ in range(32):
                _mid, data = self._recv()
                files = data.get("OPFileQuery")
                if isinstance(files, list):
                    return files
                ret = data.get("Ret")
                if ret not in (100, None):
                    raise DVRIPError(f"file search failed: Ret={ret}")
            return []

    def download_by_time(
        self, channel: int, begin: str, end: str,
        max_bytes: int | None = None, idle_timeout: float = 4.0,
    ) -> bytes:
        """Stream a recording by TIME WINDOW (PlayMode 'ByTime') for one channel.

        Unlike download_recording (ByName, whole file), this asks the NVR for just
        the [begin, end] slice across whatever files cover it. That lets us seek to
        any moment and fetch a short chunk instead of a whole 1-hour / hundreds-of-MB
        segment. Times are 'YYYY-MM-DD HH:MM:SS'. Returns raw sofia-framed bytes.
        """
        # NB: the channel must go in its own "Channel" field for ByTime; putting
        # it in "Value" (as ByName implies via FileName) yields an empty stream.
        param = {"PlayMode": "ByTime", "FileName": "", "StreamType": 0,
                 "Value": 0, "Channel": channel, "TransMode": "TCP"}
        return self._playback_stream(param, begin, end, max_bytes, idle_timeout)

    def download_recording(
        self, filename: str, begin: str, end: str,
        expected_bytes: int | None = None, idle_timeout: float = 4.0,
    ) -> bytes:
        """Download one recording by name over DVRIP (Claim=1424, DownloadStart=1420,
        media on 1426). Returns the raw sofia-framed bytes."""
        param = {"PlayMode": "ByName", "FileName": filename,
                 "StreamType": 0, "Value": 0, "TransMode": "TCP"}
        return self._playback_stream(param, begin, end, expected_bytes, idle_timeout)

    def _playback_stream(
        self, param: dict[str, Any], begin: str, end: str,
        max_bytes: int | None, idle_timeout: float,
    ) -> bytes:
        with self._lock:
            assert self._sock is not None, "not connected"
            def send(mid: int, action: str, extra: dict | None = None) -> None:
                p = dict(param, **(extra or {}))
                self._sock.sendall(self._build(mid, {
                    "Name": "OPPlayBack", "SessionID": self._sid_str(),
                    "OPPlayBack": {"Action": action, "StartTime": begin,
                                   "EndTime": end, "Parameter": p},
                }))
                self._seq += 1
            send(1424, "Claim")
            self._recv()  # claim ack
            send(1420, "DownloadStart",
                 {"IntelligentPlayBackEvent": "", "IntelligentPlayBackSpeed": 0})
            buf = bytearray()
            self._sock.settimeout(idle_timeout)
            try:
                while True:
                    head = self._recv_exact(HEADER_LEN)
                    mid, length = struct.unpack_from("<HI", head, 14)
                    body = self._recv_exact(length) if length else b""
                    if mid == 1426 and not _is_status(body):
                        buf += body
                    if max_bytes and len(buf) >= max_bytes:
                        break
            except (OSError, DVRIPError):
                pass  # idle timeout = end of stream
            try:
                send(1420, "DownloadStop")
            except OSError:
                pass
            self._sock.settimeout(self.timeout)
            return bytes(buf)

    def stream_recording(
        self, filename: str, begin: str, end: str,
        idle_timeout: float = 1.0,
    ):
        """Like download_recording but YIELDS raw sofia-framed bytes as the NVR sends
        them, instead of buffering the whole slice first. Lets the caller demux and
        transcode on the fly so playback starts after the first GOP, not the whole
        chunk. The socket lock is held for the life of the generator, so the caller
        must drain it promptly (and close it) before reusing the client."""
        param = {"PlayMode": "ByName", "FileName": filename,
                 "StreamType": 0, "Value": 0, "TransMode": "TCP"}
        with self._lock:
            assert self._sock is not None, "not connected"

            def send(mid: int, action: str, extra: dict | None = None) -> None:
                p = dict(param, **(extra or {}))
                self._sock.sendall(self._build(mid, {
                    "Name": "OPPlayBack", "SessionID": self._sid_str(),
                    "OPPlayBack": {"Action": action, "StartTime": begin,
                                   "EndTime": end, "Parameter": p},
                }))
                self._seq += 1

            send(1424, "Claim")
            self._recv()  # claim ack
            send(1420, "DownloadStart",
                 {"IntelligentPlayBackEvent": "", "IntelligentPlayBackSpeed": 0})
            self._sock.settimeout(idle_timeout)
            try:
                while True:
                    head = self._recv_exact(HEADER_LEN)
                    mid, length = struct.unpack_from("<HI", head, 14)
                    body = self._recv_exact(length) if length else b""
                    if mid == 1426 and not _is_status(body):
                        yield body
            except (OSError, DVRIPError):
                pass  # idle timeout = end of stream
            try:
                send(1420, "DownloadStop")
            except OSError:
                pass
            self._sock.settimeout(self.timeout)

    # --- two-way audio (talk / intercom) ------------------------------------
    # BitRate 0 matches what the official XMEye lib (libFunSDK NewMediaControlPTL)
    # puts on the wire for talk; the device ignores a non-zero value but we mirror
    # it exactly.
    _TALK_FMT = {"BitRate": 0, "EncodeType": "G711_ALAW",
                 "SampleBit": 8, "SampleRate": 8000}
    # Sofia audio frame header the device itself writes for G.711a @ 8 kHz
    # (confirmed from this NVR's own recordings): 00 00 01 FA | 0E (G711a) |
    # 02 (8 kHz) | <payload length LE16>. We mirror it exactly for talk-out.
    _TALK_AHDR = b"\x00\x00\x01\xfa\x0e\x02"
    TALK_FRAME = 320          # bytes of G.711a = 40 ms @ 8 kHz, one sofia frame

    def talk_open(self, channel: int | None = None) -> dict[str, Any]:
        """Open the device talk channel so audio we then send plays on the camera
        speaker. For a PoE camera behind the NVR the handshake is Claim (1434) then
        *AcrossStart* (1430) with the camera index as OPTalk "Channel" — this is the
        across-IPC path the official XMEye app uses (verified on the real NVR: the
        camera plays our audio). Plain "Start" only reaches the NVR, not the camera.
        Audio frames then go on 1432. Pair with talk_send()/talk_close()."""
        self._talk_channel = channel
        op: dict[str, Any] = {"Action": "Claim", "AudioFormat": self._TALK_FMT}
        if channel is not None:
            op["Channel"] = channel
        resp = self._cmd(TALK_CLAIM,
                         {"Name": "OPTalk", "SessionID": self._sid_str(), "OPTalk": op})
        if str(resp.get("Ret")) != "100":
            raise DVRIPError(f"talk claim failed: {resp!r}")
        op2 = dict(op, Action="AcrossStart")
        self._cmd(TALK_REQ,
                  {"Name": "OPTalk", "SessionID": self._sid_str(), "OPTalk": op2})
        return resp

    def talk_send(self, g711a: bytes) -> None:
        """Send G.711 a-law audio to the claimed talk channel, split into 320-byte
        (40 ms) sofia audio frames on the talk data message id. Caller paces in real
        time (~40 ms/frame) so the device's jitter buffer doesn't overrun."""
        with self._lock:
            for i in range(0, len(g711a), self.TALK_FRAME):
                chunk = g711a[i:i + self.TALK_FRAME]
                frame = self._TALK_AHDR + struct.pack("<H", len(chunk)) + chunk
                self._sock.sendall(self._build_raw(TALK_DATA, frame))
                self._seq += 1

    def talk_close(self) -> None:
        """End a talk session (best effort): OPTalk AcrossStop on 1430 for the camera
        we opened. The device also ends talk when the connection drops."""
        op: dict[str, Any] = {"Action": "AcrossStop", "AudioFormat": self._TALK_FMT}
        channel = getattr(self, "_talk_channel", None)
        if channel is not None:
            op["Channel"] = channel
        try:
            self._cmd(TALK_REQ, {"Name": "OPTalk", "SessionID": self._sid_str(),
                                 "OPTalk": op})
        except Exception:
            pass

    def motion_config(self, channel: int | None = None) -> Any:
        cfg = self.get_config("Detect.MotionDetect")
        if channel is None or not isinstance(cfg, list):
            return cfg
        return cfg[channel] if channel < len(cfg) else None

    def set_motion_enabled(self, channel: int, enabled: bool) -> dict[str, Any]:
        cfg = self.get_config("Detect.MotionDetect")
        if not isinstance(cfg, list) or channel >= len(cfg):
            raise DVRIPError("unexpected MotionDetect shape")
        cfg[channel]["Enable"] = bool(enabled)
        return self.set_config("Detect.MotionDetect", cfg)
