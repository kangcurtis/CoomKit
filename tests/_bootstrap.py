#!/usr/bin/env python3
"""Put the repo root on `sys.path` and hand back its path as `ROOT`.

Every test imports this first. It exists because the tests live one directory
down from the code they test, but are still run as plain scripts
(`python3 tests/test_studio.py`) rather than through a runner — there is no
pytest here and no dependencies to add one.

That leaves `sys.path[0]` pointing at `tests/`, so `import server` fails and
`Path(__file__).parent` means the wrong directory. Both are fixed here, once,
rather than in twenty-one files that would drift apart.

`ROOT` is the repo root. Use it for anything on disk — `ROOT / "web"`,
`ROOT / "data"`, `ROOT / "st-presets"`. Never hardcode an absolute path: this
tree has already been relocated once and every absolute path in it broke.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
