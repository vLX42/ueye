# uEye

A self-hosted replacement for the XMEye app for Xiongmai-family NVRs/DVRs.
One Docker container, one web port, no vendor cloud. Live grid, recordings
browser, and camera/motion-detection config — all by talking the device's own
LAN protocol (DVRIP / "Sofia", TCP 34567).

## Why this design

The XMEye app is a thin Java shell over a native `libFunSDK.so`. Two ways it
reaches a recorder:

- **DVRIP over the LAN (port 34567)** — an openly documented protocol. This
  app speaks it directly. Clean, no dependencies.
- **P2P cloud by device serial** — proprietary native code that routes through
  Xiongmai servers (`mi.xmeye.net`, `*.secu100.net`). We deliberately **don't**
  use it: it would mean depending on their cloud, the opposite of the goal.

Remote access, when you want it, is your own problem to solve cleanly: reach
the NAS through a VPN (Tailscale/WireGuard) rather than exposing the NVR to the
internet. (Exposing these recorders directly is exactly how they get botnetted.)
For now this runs LAN-only on a port.

```
 Unraid container (one port, 8080)
 ┌───────────────────────────────────────┐
 │ Caddy  ──/────────────▶ web UI         │
 │        ──/api/*───────▶ FastAPI ──┐    │
 │        ──/go2rtc/*────▶ go2rtc ─┐ │    │        LAN
 │                                 │ └────┼──DVRIP──▶ NVR :34567
 │                   dvrip:// ◀────┘      │   (recordings, config)
 └───────────────────────────────────────┘
      live video (go2rtc)  ▲
      recordings + config  ┘ (FastAPI, backend/dvrip.py)
```

## Login — important

DVRIP LAN login uses the **device-local user**, not your XMEye *cloud* account.
On these recorders that's almost always `admin` plus the password set on the box
itself. Your cloud username (e.g. `Vlx42`) is for the P2P path we don't use — it
will **not** work for the LAN login. Put the device-local credentials in
`config.yaml`.

How login works: TCP connect to `:34567`, send a `LOGIN_REQ` (id 1000) with
`{"EncryptType":"MD5","LoginType":"DVRIP-Web","UserName":...,"PassWord":<sofia-hash>}`,
get back `{"Ret":100,"SessionID":"0x..."}`, then echo that SessionID on every
later command. The password is the Xiongmai "sofia hash" (MD5 → base62, 8 chars);
see `backend/dvrip.py` (verified against the canonical `""` → `tlJwpbo6`).

## Quick start (Unraid)

1. Copy `config.example.yaml` to `config.yaml` and set your NVR IP, the
   device-local username/password, and your channels.
2. Build and run:
   ```bash
   docker compose up -d --build
   ```
   (or build the image and add a container in the Unraid UI: port `8080`,
   mount your `config.yaml` at `/config/config.yaml`.)
3. Open `http://<nas-ip>:8080`.

## What's verified vs. needs your NVR

Verified offline on this machine:
- Sofia login-hash algorithm (canonical vector) and DVRIP packet framing — unit tests in `backend/tests/`.
- Config loading and go2rtc config generation from `config.yaml`.
- FastAPI routes; graceful 502/504 when the NVR is unreachable.
- Docker image builds; container boots go2rtc + API + Caddy; UI, `/api/*`,
  and `/go2rtc/*` all serve through the single port; all channels registered.

Needs your NVR on the LAN to confirm (can't be tested without the device):
- DVRIP login against the real recorder.
- Live video actually decoding in the grid.
- Recording search returning real files, and playback.
- Motion-detect config round-trip (the `Detect.MotionDetect` shape varies by firmware).

## Layout

```
backend/dvrip.py      DVRIP protocol client (login, config, file search)
backend/main.py       FastAPI: /api/health /system /recordings /config /motion
backend/config.py     config.yaml loader
backend/go2rtc_gen.py generates go2rtc.yaml (dvrip:// per channel)
backend/tests/        offline protocol tests
web/                  single-page UI (live grid / recordings / settings)
Caddyfile run.sh      single-port front door + process supervisor
Dockerfile docker-compose.yml
```

## Roadmap

- Recording playback in the browser (DVRIP download-by-time → fMP4), currently list-only.
- PTZ controls (protocol primitive `DevPtzControl` / id 1400 is confirmed present).
- Snapshots and a motion-event feed (alarm channel, id 1504).
