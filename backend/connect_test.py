"""Quick real-NVR connectivity check. No deps beyond stdlib + PyYAML.

    python3 backend/connect_test.py            # reads ./config.yaml
    python3 backend/connect_test.py 192.168.1.50 admin 'password'

Verifies: TCP reach -> DVRIP login -> system info -> channel count ->
a sample of the motion-detect config shape (so we learn this firmware's layout).
Does not print the password.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dvrip import DVRIPClient, DVRIPError  # noqa: E402


def load_args() -> tuple[str, int, str, str]:
    if len(sys.argv) >= 4:
        host = sys.argv[1]
        user = sys.argv[2]
        pw = sys.argv[3]
        port = int(sys.argv[4]) if len(sys.argv) > 4 else 34567
        return host, port, user, pw
    import yaml
    raw = yaml.safe_load(Path("config.yaml").read_text())
    n = raw["nvr"]
    return n["host"], int(n.get("port", 34567)), n.get("username", "admin"), str(n.get("password", ""))


def main() -> int:
    host, port, user, pw = load_args()
    print(f"[1/5] connecting to {host}:{port} as {user!r} ...")
    c = DVRIPClient(host=host, port=port, username=user, password=pw, timeout=8)
    try:
        resp = c.connect()
    except DVRIPError as e:
        print(f"  LOGIN FAILED: {e}")
        print("  -> check the device-local username/password (not the XMEye cloud account).")
        return 2
    except OSError as e:
        print(f"  CANNOT REACH NVR: {e}")
        print("  -> is the IP right, and is this machine on the same LAN as the NVR?")
        return 1
    print(f"  OK. SessionID={resp.get('SessionID')}  Ret={resp.get('Ret')}")

    try:
        print("[2/5] system info ...")
        info = c.system_info().get("SystemInfo", {})
        print(f"  device: {info.get('DeviceType') or info.get('SerialNo') or '?'}  "
              f"hw={info.get('HardWare','?')}  sw={info.get('SoftWareVersion','?')}")
        print("[3/5] channel count ...")
        n = c.channel_count()
        print(f"  VideoInChannel = {n}")
        print("[4/5] encode config (confirms stream params) ...")
        enc = c.get_config("Simplify.Encode")
        print(f"  Simplify.Encode: {'list of '+str(len(enc)) if isinstance(enc,list) else type(enc).__name__}")
        print("[5/5] motion-detect config shape ...")
        md = c.get_config("Detect.MotionDetect")
        if isinstance(md, list) and md:
            keys = sorted(md[0].keys())
            print(f"  Detect.MotionDetect: list[{len(md)}]  ch0 keys: {keys}")
        else:
            print(f"  Detect.MotionDetect: {type(md).__name__}")
        print("\nALL GOOD — login works and config reads back. "
              "Fill config.yaml with these and run the container.")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
