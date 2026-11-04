# Push-to-talk latency

Noted 3 Nov 2026, steps 1 to 3 of the plan done 4 Nov 2026 (see Results).

## The issue

Holding the 🎤 button on a live tile and talking, it seems like the camera only
plays the audio after you let go, and then you hear yourself.

## What is actually going on

The audio is already streamed live:

- `web/app.js` `wirePushToTalk()`: the browser captures the mic with a
  ScriptProcessor (2048 samples, ~43 ms at 48 kHz), downsamples to 8 kHz 16-bit
  PCM and sends every chunk over the WebSocket `/api/talk/ws/{channel}` right away.
- `backend/main.py` `talk_ws()`: a-law encodes each chunk and pushes it to the
  NVR immediately (`TalkSession.send_alaw(..., pace=False)`), 320-byte / 40 ms
  frames on msgid 1432 (`backend/dvrip.py` `talk_send`).
- A one-off tone test (3 Nov, 0.5 s 1 kHz beep to Baghave ch0, read back
  through the camera's own mic over RTSP) found the tone while the talk session
  was still open. The timing number from that run was not reliable (ffmpeg probe
  buffering), so the real speaker latency is still unmeasured.

Why it feels like "plays on release":

1. **The echo you hear is the camera mic, not the speaker starting late.** While
   talking, `muteLive()` mutes every live tile (to avoid feedback). On release,
   `unmuteLive()` unmutes the tile you talked to. The live video (go2rtc MSE)
   lags a couple of seconds, so the first thing you hear is the camera mic
   replaying your own voice from its speaker.
2. **Start-up delay per press.** Every press opens a new WebSocket, then a new
   DVRIP connection: TCP connect + login + OPTalk Claim (1434) + AcrossStart (1430).
   Maybe 0.5 to 1 s. Audio captured meanwhile is queued (browser queue until
   `ws.onopen`, then the server's WebSocket buffer until `TalkSession` is up) and
   then sent in one burst. For short phrases most of the talking happens before
   the line is open.

## Plan

Do these in order, test each one before the next.

### 1. Stop hearing yourself after release (easy, biggest win)

- In `unmuteLive()`, delay unmuting the talked-to tile by ~3 s (or by the measured
  live lag), so the replay from the camera mic is skipped.
- Optional: instead of auto-unmute, leave it muted and show a small
  "🔇 tap to hear" on the tile.

### 2. Cut the start-up delay

- Pre-warm: when you press down, the session opens anyway; the win is opening it
  *before* the press. Options:
  - Open the talk WebSocket + DVRIP session on `pointerenter`/`touchstart` of the
    mic button, or when Live is open and a tile is tapped, and keep it idle for
    ~30 s. Idle sessions must not hold `_TALK_LOCK` forever (seasonal sounds and
    the other cameras need it): release after the idle timeout.
  - Or keep one DVRIP connection logged in (no talk claimed) and only do
    Claim + AcrossStart on press. Saves the TCP + login part.
- Check whether the NVR plays audio sent while the line is not yet open or drops
  it, and whether the burst of queued audio is played or skipped. If it plays the
  burst, consider dropping queued audio older than ~300 ms so the speaker is not
  always a second behind you.
- Replace the deprecated `ScriptProcessor` with an `AudioWorklet` and smaller
  chunks (e.g. 20 ms) for a bit less delay. Keep the iOS rule: the
  `AudioContext` must be created synchronously in the gesture.

### 3. Show the state on the button

- Mic button states: idle → "connecting" (pulse) → "live" (red) so you know when
  to start talking. The backend already sends `{"ok": true}` when the session is
  up; use that as the "live" signal.
- Log the time from press to "live" in the browser console for testing.

## How to test (quietly!)

**No sound on any camera without asking Peter first.** Wife sleeps with an open
window next to a camera; never at night. A test that reaches a speaker also
records on the NVR.

Silent tests (no speaker):

- Time press → `{"ok": true}` in the browser (Playwright with a fake mic:
  `--use-fake-device-for-media-stream`), before and after the pre-warm change.
- Send all-zero audio (silence) through `talk_ws` and time the backend steps
  (connect, login, claim, AcrossStart) with logging. Silence on the speaker is
  inaudible.
- Check the unmute delay in the UI with Playwright (tile `muted` stays true for
  ~3 s after `pointerup`).

Audible test (only with OK, daytime, short):

- Script from 3 Nov: send a short 1 kHz tone with `DVRIPClient.talk_open(0)` +
  `talk_send`, record the camera audio with ffmpeg over the NVR RTSP
  (`rtsp://<nvr>:554/user=..&password=..&channel=1&stream=1.sdp`), timestamp each
  chunk on arrival, and wait ~10 s for the stream to settle before sending.
  Lag = time the tone shows up (Goertzel at 1 kHz) minus send time; this
  includes the RTSP delay, so it is an upper bound.

## Results (4 Nov 2026)

Done: delayed unmute (2.5 s), line kept open 20 s after release and opened on
mouse hover, amber "connecting" / red "live" button, ScriptProcessor 1024
(~21 ms chunks), server waits up to 3 s for the talk lock, closes a line idle
for 30 s, and logs "Talk" only when audio arrives.

Measured on the real NVR, Bagdør (ch1), Mac on the LAN, tone read back over
RTSP (numbers include the RTSP delay):

| | press → heard |
|---|---|
| before (live site, every press opens a new line) | 0.77 s |
| new, cold press (phone, first press) | 0.86 s (local Docker on the Mac) |
| new, after mouse hover | 0.60 s |
| new, second press within 20 s | 0.58 s |

NVR facts found on the way:
- Line open (Claim + AcrossStart) takes 0.15 to 0.4 s; login is ~0.02 s on the LAN.
- The camera speaker needs ~0.25 s after AcrossStart before it plays.
- An open line with nothing sent for 10 s still plays at once (no keep-alive needed).
- A burst of queued audio is played in full in real time: a backlog never catches up.

Routing check (4 Nov): one tone each with OPTalk Channel 0/1/2, all three camera
mics recorded at once. The target camera's own mic was 60+ dB louder than the
others every time (Baghave 118 dB vs 57; Bagdør 125 vs 65; Forhave 124 vs 69),
so Channel picks the right speaker; the DVRIP header channel byte stays 0.

Phone pass (4 Nov, later):
- Mic capture runs in an AudioWorklet (`web/talk-worklet.js`, 20 ms chunks at
  8 kHz), ScriptProcessor kept as fallback. One AudioContext reused across
  presses and suspended in between.
- Talk state on the tile itself (amber "Connecting…", red "Talking to X" with a
  mic level meter, red outline), since the thumb covers the button and the
  status line is at the top. Mic errors in plain words on the tile. A quick tap
  shows "Hold to talk". Mic button 56 px on touch screens. Vibrates on Android
  when live.
- iOS: `navigator.audioSession` set to play-and-record while talking and back to
  playback after, so the reply should come out of the loudspeaker. If Safari
  pauses the video when it is unmuted after the 2.5 s delay (outside a tap), it
  is re-muted, keeps playing, and the tile shows "🔈 Tap to hear".
- Tested in Chromium with iPhone 13 emulation, touch events and a fake mic on
  Bagdør: press to tone ~0.65 s. The Safari pause was simulated, not seen on a
  real iPhone; audioSession and haptics are untested on real phones.

Still open: a phone has no hover, so its first press is still cold.
