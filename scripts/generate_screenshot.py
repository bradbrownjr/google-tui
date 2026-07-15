"""Regenerates assets/screenshot.png, the README hero image (P1 M7).

Drives the real app through Textual's `run_test` pilot against an entirely
fabricated dataset — fake threads/events/tasks/Drive files/contacts, zero
real PII — so the screenshot never depends on (or leaks) live account data.
Every `gauth` call that would otherwise hit the network is mocked,
including `get_credentials`/`services`, so this needs no real Google token
at all and makes zero live API calls.

Run whenever the Mail tab's visual design changes enough that the current
screenshot looks stale (new tab added, pane layout changed, color scheme
changed, etc.) — not on every commit; this is a manually-triggered, manually
-reviewed asset, not part of CI.

Usage (from the repo root):
    .venv/bin/pip install cairosvg   # one-time, not a runtime dependency
    .venv/bin/python scripts/generate_screenshot.py

Writes assets/screenshot.png directly; review the result before committing
(open it, don't just trust the exit code) since a Textual layout regression
could still "succeed" while looking wrong.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import platformdirs

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PNG = REPO_ROOT / "assets" / "screenshot.png"

# Redirect platformdirs' config/cache dirs to an isolated temp directory
# BEFORE importing anything from google_tui — settings.py/cache.py compute
# SETTINGS_PATH/CACHE_DB_PATH/KEY_FILE_PATH as module-level constants from
# platformdirs.user_config_dir()/user_cache_dir() at import time, using the
# SAME "google-tui" app name as the real installed app. On a dev machine
# that's literally your real ~/.config/google-tui and ~/.cache/google-tui.
# An earlier version of this script `shutil.rmtree`'d those paths directly
# to guarantee a clean slate for the fabricated dataset below, and it wiped
# a real user's settings.json/cache.db (twice — see AGENTS.md §6). Patching
# the platformdirs functions themselves, instead of clearing whatever path
# they happen to resolve to, makes this script structurally incapable of
# touching real user data no matter where/how it's run — there's no path to
# `rm` in the first place.
_ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="google-tui-screenshot-"))
platformdirs.user_config_dir = lambda *a, **k: str(_ISOLATED_HOME / "config")
platformdirs.user_cache_dir = lambda *a, **k: str(_ISOLATED_HOME / "cache")

from google.oauth2.credentials import Credentials  # noqa: E402
from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402

now = datetime.now(timezone.utc)


def _dt(days: int, hour: int = 9) -> datetime:
    d = now + timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


FAKE_LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "SENT", "name": "SENT", "type": "system"},
    {"id": "Label_1", "name": "Family", "type": "user"},
    {"id": "Label_2", "name": "Projects", "type": "user"},
]

FAKE_THREADS = [
    {"threadId": "th1", "subject": "Weekend plans?", "from": "Priya Rao <priya.rao@example.com>",
     "date": _dt(-1).strftime("%a, %d %b %Y %H:%M:%S +0000"), "count": 2, "unread": True,
     "snippet": "Are we still on for hiking Saturday morning? The trailhead opens at 7."},
    {"threadId": "th2", "subject": "Re: Q3 roadmap draft", "from": "Marcus Webb <marcus.webb@example.com>",
     "date": _dt(-1, 8).strftime("%a, %d %b %Y %H:%M:%S +0000"), "count": 4, "unread": True,
     "snippet": "Looks good overall — one note on the timeline for the search backend rollout."},
    {"threadId": "th3", "subject": "Your library holds are ready", "from": "City Library <noreply@example-library.org>",
     "date": _dt(-2).strftime("%a, %d %b %Y %H:%M:%S +0000"), "count": 1, "unread": False,
     "snippet": "2 items are ready for pickup at the Main Branch."},
    {"threadId": "th4", "subject": "Invoice #4471 receipt", "from": "billing@example-hosting.com",
     "date": _dt(-3).strftime("%a, %d %b %Y %H:%M:%S +0000"), "count": 1, "unread": False,
     "snippet": "Thanks for your payment. Your receipt is attached."},
    {"threadId": "th5", "subject": "Fwd: Recipe - garlic butter pasta", "from": "Dana Lin <dana.lin@example.com>",
     "date": _dt(-4).strftime("%a, %d %b %Y %H:%M:%S +0000"), "count": 1, "unread": False,
     "snippet": "Found this and thought of you, super easy weeknight dinner."},
]

FAKE_EVENTS = [
    {"id": "ev1", "summary": "Tick/Flea Appt", "start": {"dateTime": _dt(1, 14).isoformat()},
     "end": {"dateTime": _dt(1, 15).isoformat()}},
    {"id": "ev2", "summary": "OHD Water Testing", "start": {"dateTime": _dt(2, 10).isoformat()},
     "end": {"dateTime": _dt(2, 11).isoformat()}},
    {"id": "ev3", "summary": "Team standup", "start": {"dateTime": _dt(0, 9).isoformat()},
     "end": {"dateTime": _dt(0, 9).isoformat()}},
    {"id": "ev4", "summary": "Dentist", "start": {"dateTime": _dt(5, 13).isoformat()},
     "end": {"dateTime": _dt(5, 14).isoformat()}},
]

FAKE_TASKLISTS = [{"id": "list1", "title": "My Tasks"}]
FAKE_TASKS = [
    {"id": "t1", "title": "Buy cat food", "status": "needsAction", "_list": "list1"},
    {"id": "t2", "title": "Pay electric bill", "status": "completed", "_list": "list1"},
    {"id": "t3", "title": "Schedule oil change", "status": "needsAction", "_list": "list1"},
    {"id": "t4", "title": "Return library books", "status": "needsAction", "_list": "list1"},
]

FAKE_DRIVE = [
    {"id": "d1", "name": "Budget 2026.xlsx", "mimeType": "application/vnd.google-apps.spreadsheet",
     "modifiedTime": _dt(-2).isoformat(), "parents": ["root"], "size": "10240"},
    {"id": "d2", "name": "Trip Photos", "mimeType": "application/vnd.google-apps.folder",
     "modifiedTime": _dt(-10).isoformat(), "parents": ["root"], "size": ""},
    {"id": "d3", "name": "Notes.txt", "mimeType": "text/plain",
     "modifiedTime": _dt(-1).isoformat(), "parents": ["root"], "size": "512"},
]

FAKE_CONTACTS = [
    {"resource_name": "people/c1", "name": "Priya Rao", "email": "priya.rao@example.com", "phone": ""},
    {"resource_name": "people/c2", "name": "Marcus Webb", "email": "marcus.webb@example.com", "phone": "555-0142"},
    {"resource_name": "people/c3", "name": "Dana Lin", "email": "dana.lin@example.com", "phone": ""},
]

FAKE_CREDS = Credentials(token="fake-token", refresh_token=None, client_id="fake", client_secret="fake",
                          token_uri="https://example.com/token", scopes=[])


def _fake_services():
    return {"gmail": object(), "calendar": object(), "drive": object(), "tasks": object(), "people": object()}


async def _capture(svg_path: Path) -> None:
    app = GoogleTUI()
    async with app.run_test(size=(150, 42)) as pilot:
        await asyncio.sleep(2.5)
        await pilot.pause()
        app.action_goto_pane_email()
        await pilot.pause()
        lst = app.query_one("#email-list")
        if lst.children:
            lst.index = 0
        await pilot.pause()
        app.save_screenshot(str(svg_path))


def main() -> None:
    with patch.object(gauth, "get_credentials", return_value=FAKE_CREDS), \
         patch.object(gauth, "services", side_effect=_fake_services), \
         patch.object(gauth, "list_labels", return_value=FAKE_LABELS), \
         patch.object(gauth, "list_threads", return_value=FAKE_THREADS), \
         patch.object(gauth, "list_events", return_value=FAKE_EVENTS), \
         patch.object(gauth, "events_between", return_value=FAKE_EVENTS), \
         patch.object(gauth, "month_events", return_value=FAKE_EVENTS), \
         patch.object(gauth, "list_tasklists", return_value=FAKE_TASKLISTS), \
         patch.object(gauth, "list_tasks", return_value=FAKE_TASKS), \
         patch.object(gauth, "list_drive", return_value=FAKE_DRIVE), \
         patch.object(gauth, "list_contacts", return_value=FAKE_CONTACTS), \
         patch.object(gauth, "get_thread", return_value=[]), \
         patch.object(gauth, "get_file_metadata", return_value={}), \
         patch.object(gauth, "mark_read", return_value=None), \
         patch.object(gauth, "reply_to", return_value=None), \
         patch.object(gauth, "forward", return_value=None), \
         patch.object(gauth, "send_message", return_value=None), \
         patch.object(gauth, "set_task_status", return_value=None):

        with tempfile.TemporaryDirectory() as tmp:
            svg_path = Path(tmp) / "hero.svg"
            asyncio.run(_capture(svg_path))

            try:
                import cairosvg
            except ImportError:
                raise SystemExit(
                    "cairosvg not installed — run `.venv/bin/pip install cairosvg` "
                    "first (it's a one-off dev tool for this script, not a project "
                    "dependency, so it's not in pyproject.toml)."
                )
            OUTPUT_PNG.parent.mkdir(parents=True, exist_ok=True)
            cairosvg.svg2png(url=str(svg_path), write_to=str(OUTPUT_PNG), scale=2.0)

    print(f"Wrote {OUTPUT_PNG} — review it before committing.")


if __name__ == "__main__":
    main()
