"""Audio helpers for two-way talk: G.711 a-law encoding and a synthesized
Halloween sound. Pure standard library (no numpy) so the talk path works even
when the optional CV/face stack isn't installed."""
from __future__ import annotations

import math
import struct


def linear2alaw(pcm: int) -> int:
    """ITU-T G.711 A-law encode one 16-bit signed sample -> one a-law byte."""
    sign = 0x00 if pcm >= 0 else 0x80
    if sign:
        pcm = -pcm
    if pcm > 32635:
        pcm = 32635
    if pcm >= 256:
        exponent = 7
        mask = 0x4000
        while exponent > 0 and (pcm & mask) == 0:
            exponent -= 1
            mask >>= 1
        mantissa = (pcm >> (exponent + 3)) & 0x0F
        alaw = (exponent << 4) | mantissa
    else:
        alaw = pcm >> 4
    return (alaw ^ sign ^ 0x55) & 0xFF


def pcm16le_to_alaw(pcm: bytes) -> bytes:
    """Encode little-endian 16-bit mono PCM to G.711 a-law."""
    n = len(pcm) // 2
    if n == 0:
        return b""
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return bytes(linear2alaw(s) for s in samples)


def _samples_to_alaw(samples: list[float]) -> bytes:
    """Clip float samples in [-1, 1] to 16-bit and a-law encode."""
    out = bytearray(len(samples))
    for i, s in enumerate(samples):
        if s > 1.0:
            s = 1.0
        elif s < -1.0:
            s = -1.0
        out[i] = linear2alaw(int(s * 32767))
    return bytes(out)


def halloween_alaw(rate: int = 8000) -> bytes:
    """A fun, clearly-spooky ghost wail, synthesized on the fly.

    A swelling 'wooOOOooo' built from a few detuned sines whose pitch glides
    up and back down with a slow vibrato, a shimmer of tremolo, and a soft
    swell-and-fade envelope — then a low descending 'boo' to finish. Returned
    as G.711 a-law @ 8 kHz, ready for DVRIPClient.talk_send()."""
    def wail(dur: float, f0: float, f1: float, f2: float, amp: float) -> list[float]:
        n = int(dur * rate)
        out = [0.0] * n
        # three detuned voices for a chorused, hollow tone
        detunes = (1.0, 1.006, 0.994)
        phases = [0.0, 0.0, 0.0]
        for i in range(n):
            t = i / n                      # 0..1 across the wail
            # pitch glides f0 -> f1 -> f2 (up then down), plus 5.5 Hz vibrato
            if t < 0.5:
                base = f0 + (f1 - f0) * (t / 0.5)
            else:
                base = f1 + (f2 - f1) * ((t - 0.5) / 0.5)
            vib = 1.0 + 0.03 * math.sin(2 * math.pi * 5.5 * (i / rate))
            freq = base * vib
            # swell in, hold, fade out (half-sine envelope) + 7 Hz tremolo
            env = math.sin(math.pi * t) ** 1.5
            trem = 0.8 + 0.2 * math.sin(2 * math.pi * 7.0 * (i / rate))
            s = 0.0
            for v, d in enumerate(detunes):
                phases[v] += 2 * math.pi * freq * d / rate
                s += math.sin(phases[v])
            out[i] = amp * env * trem * (s / len(detunes))
        return out

    samples: list[float] = []
    samples += wail(2.6, 240, 430, 200, 0.85)      # main rising/falling wail
    samples += [0.0] * int(0.15 * rate)
    samples += wail(2.2, 300, 500, 170, 0.8)       # a second, higher cry
    samples += [0.0] * int(0.2 * rate)
    samples += wail(1.6, 180, 120, 90, 0.9)        # low descending "boo"
    return _samples_to_alaw(samples)


def _formant_gain(f: float, formants: tuple[tuple[float, float], ...]) -> float:
    """Vowel colour: sum of Lorentzian resonances (centre, bandwidth) so
    harmonics near a formant are boosted. Shapes a buzzy glottal source into a
    vowel-ish 'ah'."""
    g = 0.0
    for centre, bw in formants:
        g += 1.0 / (1.0 + ((f - centre) / (bw / 2.0)) ** 2)
    return g


def evil_laugh_alaw(rate: int = 8000) -> bytes:
    """A cackling 'MUHAHAHA' evil laugh, synthesized on the fly.

    A train of voiced vowel bursts ('ha'), each a formant-shaped harmonic stack
    with a quick attack and exponential decay. Pitch and timing vary per syllable
    and drift down over the laugh, ending on a long drawn-out 'haaa' with vibrato.
    Returned as G.711 a-law @ 8 kHz."""
    AH = ((720.0, 130.0), (1100.0, 150.0), (2600.0, 220.0))  # "ah" formants

    def syllable(dur: float, f_start: float, f_peak: float, f_end: float,
                 amp: float, decay: float = 7.0, vibrato: float = 0.0) -> list[float]:
        """One voiced burst. Pitch rises to a peak at ~20% then falls (the lively
        arc of a real laugh), fast attack, exponential decay. `vibrato` adds a
        wobble (for the drawn-out finale)."""
        n = max(1, int(dur * rate))
        out = [0.0] * n
        phase = 0.0
        atk = max(1, int(0.012 * rate))         # ~12 ms attack
        pk = 0.2                                  # pitch peaks a fifth of the way in
        for i in range(n):
            t = i / n
            if t < pk:
                f0 = f_start + (f_peak - f_start) * (t / pk)
            else:
                f0 = f_peak + (f_end - f_peak) * ((t - pk) / (1 - pk))
            if vibrato:
                f0 *= 1.0 + vibrato * math.sin(2 * math.pi * 5.5 * (i / rate))
            phase += 2 * math.pi * f0 / rate
            s = 0.0                               # formant-weighted harmonic stack
            h = 1
            while h * f0 < rate / 2:
                s += (_formant_gain(h * f0, AH) / h) * math.sin(h * phase)
                h += 1
            if i < atk:
                env = i / atk
            else:
                env = math.exp(-decay * (i - atk) / n)
            trem = 0.85 + 0.15 * math.sin(2 * math.pi * 11.0 * (i / rate))  # liveliness
            out[i] = amp * env * trem * s
        return out

    def gap(sec: float) -> list[float]:
        return [0.0] * int(sec * rate)

    samples: list[float] = []
    # 1) wind-up: a low "mwaaa" that swoops upward
    samples += syllable(0.34, 110, 150, 300, 0.6, decay=2.5)
    samples += gap(0.05)
    # 2) main cackle: sing-song "HA-ha-HA-ha", bouncy (alternating high/low),
    #    drifting down overall, gaps accelerating to build momentum
    base =  [300, 250, 285, 235, 270, 220, 255, 210, 240, 200]
    gaps =  [0.07, 0.05, 0.065, 0.048, 0.06, 0.045, 0.055, 0.045, 0.05, 0.06]
    for k, (p, g) in enumerate(zip(base, gaps)):
        d = 0.15 - 0.004 * k                      # syllables tighten slightly
        samples += syllable(d, p * 0.95, p * 1.18, p * 0.85, 0.85)
        samples += gap(g)
    # 3) cheeky high witch-giggle: four quick bright bursts
    for _ in range(4):
        samples += syllable(0.085, 430, 500, 400, 0.6, decay=9.0)
        samples += gap(0.03)
    samples += gap(0.04)
    # 4) big drawn-out finale "HAAAAA" descending with vibrato
    samples += syllable(1.1, 240, 300, 150, 0.95, decay=2.0, vibrato=0.05)

    # normalize so the harmonic stacks don't clip
    peak = max((abs(s) for s in samples), default=1.0) or 1.0
    scale = 0.95 / peak
    return _samples_to_alaw([s * scale for s in samples])


# sound name -> builder. Add more here (e.g. a siren) and they appear in the UI.
# The pumpkin button plays "halloween".
def jingle_bells_alaw(rate: int = 8000) -> bytes:
    """First line of the 'Jingle Bells' chorus (public-domain tune) as a music box: each note a
    sine plus bright partials with a plucked exponential decay, notes overlap
    so they ring like tines."""
    C5, D5, E5, F5, G5 = 523.25, 587.33, 659.25, 698.46, 783.99
    e, q, h = 0.17, 0.34, 0.68          # eighth / quarter / half at ~88 bpm
    tune = [(E5, q), (E5, q), (E5, h), (E5, q), (E5, q), (E5, h),
            (E5, q), (G5, q), (C5, q + e), (D5, e), (E5, h + h)]   # "...jingle all the way"
    total = sum(d for _, d in tune) + 1.2
    out = [0.0] * int(total * rate)
    t0 = 0.0
    for f, d in tune:
        start = int(t0 * rate)
        ring = int(1.1 * rate)
        for i in range(min(ring, len(out) - start)):
            t = i / rate
            env = math.exp(-t * 4.5) * min(1.0, t * 400)
            v = (math.sin(2 * math.pi * f * t) + 0.35 * math.sin(2 * math.pi * 2 * f * t)
                 + 0.15 * math.sin(2 * math.pi * 3.01 * f * t) * math.exp(-t * 9))
            out[start + i] += 0.32 * env * v
        t0 += d
    return _samples_to_alaw(out)


def fireworks_alaw(rate: int = 8000, seed: int = 2027) -> bytes:
    """Two rockets: a rising whistle, a deep thump-bang, then a crackling
    shower of tiny pops. Deterministic noise so it sounds the same each time."""
    import random
    rnd = random.Random(seed)
    out = [0.0] * int(5.2 * rate)

    def add(at: float, samples: list[float]) -> None:
        s0 = int(at * rate)
        for i, v in enumerate(samples):
            if s0 + i < len(out):
                out[s0 + i] += v

    def whistle(dur: float, f0: float, f1: float) -> list[float]:
        n, ph, res = int(dur * rate), 0.0, []
        for i in range(n):
            x = i / n
            ph += 2 * math.pi * (f0 + (f1 - f0) * x * x) / rate
            res.append(0.10 * math.sin(ph) * min(1.0, x * 8) * (1 - x * 0.5) + 0.03 * (rnd.random() - 0.5))
        return res

    def bang(dur: float) -> list[float]:
        n, lp, res = int(dur * rate), 0.0, []
        for i in range(n):
            t = i / rate
            lp += 0.35 * ((rnd.random() * 2 - 1) - lp)          # one-pole low-pass: boomy noise
            res.append(0.95 * lp * math.exp(-t * 7) + 0.5 * math.sin(2 * math.pi * 55 * t) * math.exp(-t * 12))
        return res

    def crackle(dur: float, density: int) -> list[float]:
        res = [0.0] * int(dur * rate)
        for _ in range(density):
            p = rnd.randrange(len(res) - 60)
            amp = 0.9 * rnd.random() * (1 - 0.7 * p / len(res))
            for k in range(rnd.randrange(15, 50)):
                res[p + k] += amp * (rnd.random() * 2 - 1) * math.exp(-k / 10)
        return res

    add(0.0, whistle(1.0, 700, 2300)); add(1.0, bang(1.2)); add(1.15, crackle(1.6, 140))
    add(2.4, whistle(0.8, 900, 2700)); add(3.2, bang(1.1)); add(3.3, crackle(1.8, 170))
    return _samples_to_alaw(out)


SOUNDS = {
    "halloween": ("🎃 Evil laugh", evil_laugh_alaw),
    "ghost": ("👻 Ghost wail", halloween_alaw),
    "jingle_bells": ("🎶 Jingle Bells music box", jingle_bells_alaw),
    "fireworks": ("🎆 Fireworks", fireworks_alaw),
}
