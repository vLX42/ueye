import ast
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException


def _day_range():
    # load just the helper (importing main.py starts the app's background threads)
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    tree = ast.parse(src)
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name == "_day_range")
             or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "MAX_RANGE_DAYS")]
    ns = {"HTTPException": HTTPException, "timedelta": timedelta}
    exec(compile(ast.Module(nodes, []), "main.py", "exec"), ns)
    return ns["_day_range"], ns["MAX_RANGE_DAYS"]


def test_single_day_and_short_range():
    f, _ = _day_range()
    assert f("2026-11-19", "2026-11-19") == [date(2026, 11, 19)]
    assert len(f("2026-11-01", "2026-11-07")) == 7


def test_range_at_the_limit_is_allowed_and_over_it_is_400():
    f, cap = _day_range()
    d0 = date(2026, 1, 1)
    assert len(f(d0.isoformat(), (d0 + timedelta(days=cap - 1)).isoformat())) == cap
    with pytest.raises(HTTPException) as e:
        f("2000-01-01", "2026-11-19")
    assert e.value.status_code == 400


def test_bad_dates_are_400_not_500():
    f, _ = _day_range()
    for a, b in (("2026-13-01", "2026-13-01"), ("yesterday", "today"), (None, None)):
        with pytest.raises(HTTPException) as e:
            f(a, b)
        assert e.value.status_code == 400


def test_reversed_range_is_empty_as_before():
    f, _ = _day_range()
    assert f("2026-11-19", "2026-11-18") == []
