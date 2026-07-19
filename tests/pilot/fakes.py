"""Shared fabricated dataset + gauth patch list for tests/pilot/ scenarios,
factored out of the one-off pattern used by scripts/generate_screenshot.py
and this feature's original scratchpad verification scripts. No real PII,
zero live API calls -- every gauth call a GoogleTUI() instance would
otherwise make over the network is mocked.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from google.oauth2.credentials import Credentials

from google_tui import fetchers, gauth

now = datetime.now(timezone.utc)


def dt_iso(days: int, hour: int = 9) -> str:
    d = now + timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


FAKE_LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
]

FAKE_THREADS = [
    {"threadId": "th1", "subject": "Weekend plans?", "from": "Priya Rao <priya.rao@example.com>",
     "date": (now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000"), "count": 2,
     "unread": True, "snippet": "Are we still on for hiking Saturday morning?"},
]

FAKE_EVENTS = [
    {"id": "ev1", "summary": "Team standup", "start": {"dateTime": dt_iso(0, 9)},
     "end": {"dateTime": dt_iso(0, 10)}},
]

FAKE_TASKLISTS = [{"id": "list1", "title": "My Tasks"}]
FAKE_TASKS = [
    {"id": "t1", "title": "Buy cat food", "status": "needsAction", "_list": "list1"},
]

FAKE_DRIVE = [
    {"id": "d1", "name": "Notes.txt", "mimeType": "text/plain",
     "modifiedTime": dt_iso(-1), "parents": ["root"], "size": "512"},
]

FAKE_CONTACTS: list[dict] = []

FAKE_CALENDARS = [{"id": "primary", "backgroundColor": "#039BE5", "selected": True}]

# Dashboard external cards (2026-07-19) -- these are only ever fetched when
# their card is enabled AND (for weather/stocks) configured, which no default
# Settings() is, so most scenarios never touch these; mocked here anyway on
# base_patches()' own "zero live API calls, ever" principle, in case a future
# scenario enables one without remembering to add its own patch.
FAKE_WEATHER = {"location": "Testville, TS", "temp_f": 72.0, "wind_mph": 5.0,
                "condition": "Clear sky", "high_f": 78.0, "low_f": 60.0}
FAKE_STOCKS = [{"symbol": "AAPL", "price": 123.45, "change": 1.23, "change_pct": 1.01}]
FAKE_WORD_OF_DAY = {"word": "quixotic", "definition": "Exceedingly idealistic; unrealistic.",
                     "link": "https://www.merriam-webster.com/word-of-the-day"}
FAKE_WIKI_POTD = {"title": "Example Bridge", "description": "A scenic bridge at sunset.",
                   "link": "https://commons.wikimedia.org/wiki/File:Example.jpg"}


def _fake_fetch_feed(url: str, timeout: int = 15) -> list[dict]:
    """Stand-in for fetchers.fetch_feed -- one fabricated entry per url,
    tagged with that same url as feed_url/link so cache keys and any
    per-feed assertions stay meaningful regardless of which feed a scenario
    subscribes to (e.g. via the popular-feeds picker)."""
    return [{
        "id": f"{url}#1", "title": "Fake headline", "link": url,
        "summary": "Fake summary.", "published": dt_iso(-1),
        "feed_title": "Fake Feed", "feed_url": url,
    }]

FAKE_CREDS = Credentials(token="fake-token", refresh_token=None, client_id="fake", client_secret="fake",
                          token_uri="https://example.com/token", scopes=[])


def _fake_services() -> dict:
    return {"gmail": object(), "calendar": object(), "drive": object(), "tasks": object(), "people": object()}


def base_patches() -> list:
    """Un-started patch objects covering every gauth call GoogleTUI() makes
    during startup/normal navigation. Callers enter them via an ExitStack
    (see `applied()` below) and may append scenario-specific patches on top."""
    return [
        patch.object(gauth, "get_credentials", return_value=FAKE_CREDS),
        patch.object(gauth, "services", side_effect=_fake_services),
        patch.object(gauth, "list_labels", return_value=FAKE_LABELS),
        patch.object(gauth, "list_threads", return_value=(FAKE_THREADS, None)),
        patch.object(gauth, "get_thread", return_value=[]),
        patch.object(gauth, "list_events", return_value=FAKE_EVENTS),
        patch.object(gauth, "events_between", return_value=FAKE_EVENTS),
        patch.object(gauth, "month_events", return_value=FAKE_EVENTS),
        patch.object(gauth, "list_calendars", return_value=FAKE_CALENDARS),
        patch.object(gauth, "list_tasklists", return_value=FAKE_TASKLISTS),
        patch.object(gauth, "list_tasks", return_value=FAKE_TASKS),
        patch.object(gauth, "list_drive", return_value=(FAKE_DRIVE, None)),
        patch.object(gauth, "list_contacts", return_value=FAKE_CONTACTS),
        patch.object(gauth, "reply_to", return_value=None),
        patch.object(gauth, "forward", return_value=None),
        patch.object(gauth, "send_message", return_value=None),
        patch.object(gauth, "mark_read", return_value=None),
        patch.object(gauth, "set_task_status", return_value=None),
        patch.object(fetchers, "fetch_weather", return_value=FAKE_WEATHER),
        patch.object(fetchers, "fetch_stocks", return_value=FAKE_STOCKS),
        patch.object(fetchers, "fetch_word_of_day", return_value=FAKE_WORD_OF_DAY),
        patch.object(fetchers, "fetch_wiki_potd", return_value=FAKE_WIKI_POTD),
        patch.object(fetchers, "fetch_feed", side_effect=_fake_fetch_feed),
    ]


@contextlib.contextmanager
def applied(patches: list):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield
