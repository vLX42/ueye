"""Seasonal sound button: which season it is, and which sounds belong to it.

Sounds are either synthesized (audiogen.SOUNDS) or short clips in
backend/sounds/*.wav (8 kHz mono 16-bit, see sounds/CREDITS.md). The button in
the live view shows the current season's emoji and plays a random sound of it.
"""
from __future__ import annotations

import random
import wave
from datetime import date, timedelta
from pathlib import Path

import audiogen

SOUND_DIR = Path(__file__).resolve().parent / "sounds"

# id -> label for the recorded clips (file = sounds/<id>.wav)
CLIPS = {
    "sleigh_bells": "🔔 Sleigh bells",
    "ho_ho_ho": "🎅 Ho ho ho",
    "party_blower": "🥳 Party blower",
    "rooster": "🐓 Rooster",
    "cuckoo": "🐦 Cuckoo",
    "gull": "🐦 Seagull",
    "wind_chimes": "🎐 Wind chimes",
    "barn_owl": "🦉 Barn owl",
}

# (id, emoji, label, sounds). Order matters only for documentation; dates decide.
SEASONS = {
    "christmas": ("🎄", "Christmas", ["sleigh_bells", "ho_ho_ho", "jingle_bells"]),
    "newyear": ("🎆", "New Year", ["fireworks", "party_blower"]),
    "winter": ("❄️", "Winter", ["wind_chimes"]),
    "easter": ("🐣", "Easter", ["rooster", "cuckoo"]),
    "spring": ("🌷", "Spring", ["cuckoo", "rooster"]),
    "summer": ("☀️", "Summer", ["gull", "wind_chimes"]),
    "autumn": ("🍂", "Autumn", ["barn_owl", "wind_chimes"]),
    "halloween": ("🎃", "Halloween", ["halloween", "ghost", "barn_owl"]),
}


def _easter(y: int) -> date:
    """Gregorian Easter Sunday (anonymous / Meeus algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def season_for(d: date) -> str:
    md = (d.month, d.day)
    if (11, 1) <= md <= (12, 25):
        return "christmas"
    if md >= (12, 26) or md <= (1, 6):
        return "newyear"
    e = _easter(d.year)
    if e - timedelta(days=7) <= d <= e + timedelta(days=1):    # Palm Sunday .. Easter Monday
        return "easter"
    if d.month == 10:
        return "halloween"
    if d.month in (3, 4, 5):
        return "spring"
    if d.month in (6, 7, 8):
        return "summer"
    if d.month == 9:
        return "autumn"
    return "winter"                                             # 7 Jan .. end of Feb


def current() -> dict:
    sid = season_for(date.today())
    emoji, label, sounds = SEASONS[sid]
    return {"id": sid, "emoji": emoji, "label": label,
            "sounds": [{"id": s, "label": label_of(s)} for s in sounds]}


def label_of(sound: str) -> str:
    if sound in CLIPS:
        return CLIPS[sound]
    return audiogen.SOUNDS[sound][0]


def all_ids() -> list[str]:
    return list(CLIPS) + list(audiogen.SOUNDS)


def render(sound: str) -> tuple[str, bytes]:
    """(chosen id, G.711 a-law @ 8 kHz) for a sound id. 'season' picks a random one of the
    current season's sounds."""
    if sound == "season":
        sound = random.choice(SEASONS[season_for(date.today())][2])
    if sound in CLIPS:
        with wave.open(str(SOUND_DIR / f"{sound}.wav"), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (8000, 1, 2):
                raise ValueError(f"{sound}.wav must be 8 kHz mono 16-bit")
            return sound, audiogen.pcm16le_to_alaw(w.readframes(w.getnframes()))
    if sound in audiogen.SOUNDS:
        return sound, audiogen.SOUNDS[sound][1]()
    raise KeyError(sound)
