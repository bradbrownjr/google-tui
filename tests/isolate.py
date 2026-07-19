"""Shared helper for isolating google_tui's on-disk state during tests.

Every pilot scenario in tests/pilot/ MUST call `isolate()` as its very first
action, before importing anything from google_tui — settings.py/cache.py/
remote_creds.py compute their on-disk paths as module-level constants from
platformdirs.user_config_dir()/user_cache_dir()/user_documents_dir() at
import time, using the same "google-tui" app name as the real installed app.
Patching those functions after the fact is too late; a prior session
shutil.rmtree()'d a resolved real path directly and wiped a real user's
~/.config/google-tui and ~/.cache/google-tui twice before this pattern
became mandatory (see AGENTS.md §6). Patching the platformdirs functions
themselves, instead of clearing whatever path they resolve to, makes this
structurally incapable of touching real user data no matter where it runs.

Each pilot scenario also runs as its own process (see AGENTS.md §6's note on
a real DuplicateIds crash from chaining multiple GoogleTUI() instances in one
process, caused by a leftover background thread=True worker) — so calling
isolate() gives that process a fresh temp dir, no cleanup between scenarios
needed.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import platformdirs


def isolate(prefix: str = "google-tui-test-") -> Path:
    home = Path(tempfile.mkdtemp(prefix=prefix))
    platformdirs.user_config_dir = lambda *a, **k: str(home / "config")
    platformdirs.user_cache_dir = lambda *a, **k: str(home / "cache")
    platformdirs.user_documents_dir = lambda *a, **k: str(home / "documents")
    return home
