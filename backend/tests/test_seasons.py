import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seasons  # noqa: E402


def test_season_boundaries():
    cases = {
        "2026-11-01": "christmas", "2026-12-25": "christmas",
        "2026-12-26": "newyear", "2027-01-01": "newyear", "2027-01-06": "newyear",
        "2027-01-07": "winter", "2027-02-28": "winter",
        "2027-03-21": "easter", "2027-03-29": "easter", "2027-03-30": "spring",
        "2027-06-01": "summer", "2027-09-15": "autumn", "2027-10-31": "halloween",
    }
    for d, want in cases.items():
        assert seasons.season_for(date.fromisoformat(d)) == want, d


def test_easter_dates():
    assert seasons._easter(2027) == date(2027, 3, 28)
    assert seasons._easter(2028) == date(2028, 4, 16)
    assert seasons._easter(2030) == date(2030, 4, 21)


def test_every_sound_renders():
    for sid in seasons.all_ids():
        chosen, alaw = seasons.render(sid)
        assert chosen == sid and len(alaw) > 4000
    for _, _, sounds in seasons.SEASONS.values():
        for s in sounds:
            assert s in seasons.all_ids()
