import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _bitmask():
    # import just the helper without starting the app (main.py spawns threads)
    import ast
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_bitmask")
    ns = {}
    exec(compile(ast.Module([fn], []), "main.py", "exec"), ns)
    return ns["_bitmask"]


def test_bitmask_accepts_number_and_hex_string():
    f = _bitmask()
    assert f(2) == 2
    assert f("2") == 2
    assert f("0x00000002") == 2
    assert f("00000004") == 4          # hex without 0x (ChnLinkStatus style)
    assert f("ff") == 255
    assert f("10") == 16               # strings are hex, like the NVR sends them
    assert f(None) == 0
    assert f("") == 0
    assert f("garbage") == 0
