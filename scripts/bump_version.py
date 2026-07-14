#!/usr/bin/env python3
"""Bump the patch version in google_tui/__init__.py and pyproject.toml.

Single source of truth is `__version__` in google_tui/__init__.py; pyproject.toml
is kept in lockstep so a `pip install` reports the same number the app does.

Run by the pre-commit hook (hooks/pre-commit) so every commit carries its own
version — that's what makes the updater's "updated to vX.Y.Z" message mean
something. Also runnable by hand:

    python scripts/bump_version.py            # 0.1.4 -> 0.1.5
    python scripts/bump_version.py --minor    # 0.1.4 -> 0.2.0
    python scripts/bump_version.py --major    # 0.1.4 -> 1.0.0
    python scripts/bump_version.py --show     # print current version, change nothing
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "google_tui" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"

_INIT_RE = re.compile(r'^(__version__\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', re.M)
_PYPROJECT_RE = re.compile(r'^(version\s*=\s*")\d+\.\d+\.\d+(")', re.M)


def read_version() -> tuple[int, int, int]:
    m = _INIT_RE.search(INIT.read_text())
    if not m:
        raise SystemExit(f"no __version__ found in {INIT}")
    return int(m[2]), int(m[3]), int(m[4])


def write_version(major: int, minor: int, patch: int) -> None:
    new = f"{major}.{minor}.{patch}"
    INIT.write_text(_INIT_RE.sub(rf'\g<1>{new}\g<5>', INIT.read_text()))
    text = PYPROJECT.read_text()
    if _PYPROJECT_RE.search(text):
        PYPROJECT.write_text(_PYPROJECT_RE.sub(rf'\g<1>{new}\g<2>', text))
    else:
        print(f"warning: no version line in {PYPROJECT}; left it alone", file=sys.stderr)


def main() -> None:
    major, minor, patch = read_version()
    if "--show" in sys.argv:
        print(f"{major}.{minor}.{patch}")
        return
    if "--major" in sys.argv:
        major, minor, patch = major + 1, 0, 0
    elif "--minor" in sys.argv:
        minor, patch = minor + 1, 0
    else:
        patch += 1
    write_version(major, minor, patch)
    print(f"{major}.{minor}.{patch}")


if __name__ == "__main__":
    main()
