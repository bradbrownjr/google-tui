"""google-tui — multi-pane TUI for Gmail / Calendar / Tasks / Drive / Browser / News / Navigation / Hermes.

Top-level layout is nine full-width TABS in the blue bar: Dashboard, Mail,
Calendar, Drive, Browser, News, Navigation, Contacts, Settings (F1..F8, also
Ctrl+1..8 -- Settings is the odd one out at Ctrl+9, no F-key alias, since
F9+ isn't reliably delivered by every terminal). The Mail tab is Email-only
(list + toggleable preview pane, "p"). The Dashboard tab (2026-07-17, grown
2026-07-19 with weather/stocks/dictionary/Wikipedia, restructured 2026-07-23
into two independent columns) is a wide-left/narrow-right card layout --
MAIL (unread), NEWS (headlines), WORD OF THE DAY, PICTURE OF THE DAY on the
wide side; TIME (clock + compact month calendar + today's events), TASKS
(grouped), WEATHER, STOCKS on the narrow side -- plus a full-width ASK
HERMES card below both. Dashboard cards are reached with Tab/Shift+Tab or
Alt+arrows (no per-card digit shortcut -- Alt+<digit> pane jumps were
removed 2026-07-23, confirmed unreachable over SSH through some terminals);
"g" jumps from the focused card to whatever tab/website its data comes
from. See AGENTS.md for the full keybinding reference and the
DASH_ADJACENCY rationale.
"""
from __future__ import annotations

import base64
import datetime as dt
import email.utils
import logging
import os
import re
import sys
import textwrap
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import platformdirs
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from rapidfuzz import fuzz
from rich.text import Text
from textual import events
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Digits, Header, Input, Label, ListItem, ListView,
    RadioButton, RadioSet, RichLog, Select, SelectionList, Static, Switch,
    TabbedContent, TabPane, TextArea,
)
from textual.worker import get_current_worker  # noqa: F401 (kept for future threaded workers)

from . import bindings
from . import gauth
from . import ask
from .ask import needs_agent
from . import setup_instructions
from . import fetchers
from . import drive_sources
from . import list_tables
from . import remote_creds
from .popular_feeds import POPULAR_FEEDS
from . import render
from . import updater
from .render import DocumentView
from .cache import Cache, derive_key_from_passphrase, make_canary, new_salt, read_or_create_keyfile, verify_canary
from . import cache as cache_mod
from .settings import Settings, load_settings, save_settings
from .app_config import AppConfig, load_config

# The Mail tab is single-purpose (Email only, `2026-07-16`) -- Events/Tasks/
# Hermes live on the Dashboard tab (`tab-dashboard`). The Dashboard grew from
# 3 stacked interim panes into the real Google-native dashboard (`2026-07-17`,
# ROADMAP P4): a 2x2 card grid -- TODAY (today's events), TASKS (grouped
# overdue/today/upcoming/unscheduled), MAIL (unread count + top unread), NEWS
# (top headlines from subscribed feeds) -- with the ASK HERMES pane full-width
# below it. PANE_IDS now covers ONLY Email's own tab, which has nothing to
# switch to; DASH_PANE_IDS/DASH_ADJACENCY (below) is the Dashboard's own group.
PANE_IDS = ["email"]
# Container ids of the Dashboard's eleven cards. Order matches the two-column
# layout (2026-07-23): the WIDE left column (cards that want more horizontal
# room -- unread threads, headlines, prose) then the NARROW right column
# (compact/glanceable cards -- 6 of them now, vs the wide side's 4, so the
# two columns are different lengths; see DASH_ADJACENCY below for how that's
# handled), then Hermes (full width, below both). This is also the
# Tab/Shift+Tab cycle order and the fallback "first enabled card" order --
# unrelated to DASH_ADJACENCY's spatial (up/down/left/right) map below, which
# is what Alt+arrows actually walks.
# "tasks"/"hermes" keep their pre-2026-07-17 ids (so the #task-list populate
# path is unchanged). "events" (the old standalone TODAY card) was retired
# 2026-07-23, briefly folded into a single combined "dash-time" card (clock +
# mini calendar + today's events) the same day, then immediately split back
# into three separate cards: "dash-clock" (big-digit local time + configured
# timezones below), "dash-calendar" (a real navigable compact month view --
# arrow keys move the cursor, Enter opens a day's events, "["/"]" change
# month, all shared with the Calendar tab's own #cal-grid state), and
# "dash-today" (today's events -- keeps #event-list/#events-search's ids, so
# nothing downstream needed to change there). Combining calendar+today
# navigation in one card made moving between dates awkward, hence the split.
_DASH_WIDE_IDS = ["dash-mail", "dash-news", "dash-word", "dash-potd"]
_DASH_NARROW_IDS = ["dash-clock", "dash-calendar", "dash-today", "tasks",
                     "dash-weather", "dash-stocks"]
DASH_PANE_IDS = _DASH_WIDE_IDS + _DASH_NARROW_IDS + ["hermes"]
# pane id -> which #dash-cards column Container it lives in ("hermes" isn't
# in either -- it's outside #dash-cards entirely, its own full-width slice).
# Used by _apply_narrow_layout to hide the OTHER column when narrow (so the
# active pane's column can take the full width instead of keeping its
# non-narrow 65%/35% share).
_DASH_COL_OF = {pid: "dash-col-wide" for pid in _DASH_WIDE_IDS}
_DASH_COL_OF.update({pid: "dash-col-narrow" for pid in _DASH_NARROW_IDS})
PANE_TITLES = {
    "email": "EMAIL",
    "dash-clock": "CLOCK",
    "dash-calendar": "CALENDAR",
    "dash-today": "TODAY",
    "tasks": "TASKS",
    "dash-mail": "MAIL",
    "dash-news": "NEWS",
    "dash-weather": "WEATHER",
    "dash-stocks": "STOCKS",
    "dash-word": "WORD OF THE DAY",
    "dash-potd": "PICTURE OF THE DAY",
    "hermes": "ASK HERMES",
}
# The card grid + full-width Hermes below drives Alt+arrow adjacency as a
# real 2-D map (see CHANGELOG 2026-07-16/2026-07-17/2026-07-19; restructured
# 2026-07-23 into two independent columns, wide left / narrow right, instead
# of paired same-width rows -- see _DASH_WIDE_IDS/_DASH_NARROW_IDS above for
# why). The narrow column is now LONGER than the wide one (6 cards vs 4), so
# rows 5/6 (WEATHER/STOCKS) simply have no "left" target -- Alt+Left there is
# a harmless no-op (_adjacent's walk already tolerates a missing direction).
# Layout:
#   [ dash-mail  ][ dash-clock    ]
#   [ dash-news  ][ dash-calendar ]
#   [ dash-word  ][ dash-today    ]
#   [ dash-potd  ][ tasks         ]
#   [            ][ dash-weather  ]
#   [            ][ dash-stocks   ]
#   [      hermes (full width)      ]
DASH_ADJACENCY = {
    "dash-mail":      {"right": "dash-clock", "down": "dash-news"},
    "dash-news":      {"up": "dash-mail", "right": "dash-calendar", "down": "dash-word"},
    "dash-word":      {"up": "dash-news", "right": "dash-today", "down": "dash-potd"},
    "dash-potd":      {"up": "dash-word", "right": "tasks", "down": "hermes"},
    "dash-clock":     {"left": "dash-mail", "down": "dash-calendar"},
    "dash-calendar":  {"up": "dash-clock", "left": "dash-news", "down": "dash-today"},
    "dash-today":     {"up": "dash-calendar", "left": "dash-word", "down": "tasks"},
    "tasks":          {"up": "dash-today", "left": "dash-potd", "down": "dash-weather"},
    "dash-weather":   {"up": "tasks", "down": "dash-stocks"},
    "dash-stocks":    {"up": "dash-weather", "down": "hermes"},
    "hermes":         {"up": "dash-stocks"},
}

# Ctrl+R debounce (see action_refresh): a repeated manual refresh inside this
# window is a no-op instead of firing another real, blocking Google API round
# trip — Worker.cancel()'s own docstring says cancelled work "may still be
# running", so exclusive=True alone doesn't stop an in-flight fetch from
# costing quota, it only discards its result.
REFRESH_COOLDOWN_SECONDS = 5.0

TAB_ORDER = ["tab-dashboard", "tab-mail", "tab-calendar", "tab-drive", "tab-browser", "tab-news", "tab-navigation",
             "tab-contacts", "tab-settings"]
SETTINGS_TAB_ORDER = ["settings-tab-general", "settings-tab-browser", "settings-tab-ai", "settings-tab-feeds",
                       "settings-tab-search", "settings-tab-navigation", "settings-tab-dashboard"]
# TAB_ORDER id -> its tab label, for Settings -> General's "Start on tab"
# Select (Settings.default_start_tab, 2026-07-22) -- the compose()-time
# _tab_label() calls carry these same strings as literals since they also
# need per-tab Alt-number badges, so this is a second source of the same
# names rather than a shared constant; keep both in sync if a tab is renamed.
TAB_TITLES: dict[str, str] = {
    "tab-dashboard": "Dashboard", "tab-mail": "Mail", "tab-calendar": "Calendar",
    "tab-drive": "Drive", "tab-browser": "Browser", "tab-news": "News",
    "tab-navigation": "Navigation", "tab-contacts": "Contacts", "tab-settings": "Settings",
}
_STARTUP_TAB_CHOICES = [(TAB_TITLES[t], t) for t in TAB_ORDER]

# Narrow-terminal responsive layout (P2, 2026-07-15). Textual 8.2.8 has no
# CSS media-query/container-query feature scoped to an arbitrary container,
# but App/Screen DO support a native width-breakpoint mechanism
# (App.HORIZONTAL_BREAKPOINTS -- see screen.py's Screen._on_resize, which
# toggles a class on the Screen automatically on every resize): a breakpoint
# list of (min_width, class_name) tuples, the highest one whose min_width the
# current width satisfies gets applied as a class on the Screen. GoogleTUI
# sets HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (NARROW_WIDTH_THRESHOLD,
# "-normal")] below, so "Screen.-narrow ..." selectors in CSS drive the
# purely-visual parts of this (Drive tab list/preview stacking) with no
# Python code. The one part that ISN'T pure CSS -- hiding every Mail-tab pane
# except the active one, since which pane is "active" is runtime state, not
# something a CSS selector can see -- is handled by GoogleTUI.on_resize +
# _apply_narrow_layout()/_focus_pane(), using this same threshold so both
# mechanisms agree on what "narrow" means. 100 columns (not the 80 the
# ROADMAP names as the target) leaves headroom for borders/padding, and was
# chosen so 80x25 -- the smallest size this is verified against -- is
# comfortably inside "-narrow", not right at the boundary.
NARROW_WIDTH_THRESHOLD = 100

_SUPERSCRIPT = {1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹"}

# Shared local-file destination for anything this app writes out on request
# (Navigation's itinerary export, Drive's file download below) — one place
# under the user's Documents folder rather than a picker/prompt, matching
# this app's no-native-picker-widget precedent (see the comment near
# action_toggle_preview's Drive/Mail split for the sibling "one key, two
# tabs" precedent this borrows from).
EXPORT_DIR = Path(platformdirs.user_documents_dir()) / "google-tui"

# Where "Save to file" in the re-auth modal drops the Google authorization URL,
# for terminals that swallow OSC 52 clipboard writes (see GoogleReauthModal).
AUTH_URL_FILE = Path(platformdirs.user_cache_dir("google-tui")) / "auth_url.txt"

# Every error-severity toast is also appended here (see GoogleTUI.notify) — a
# toast that gets missed or scrolls past (e.g. a duplicate pair firing at
# startup) previously left no other record it ever happened.
LOG_FILE = Path(platformdirs.user_log_dir("google-tui")) / "google-tui.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_logger = logging.getLogger("google_tui")
_logger.setLevel(logging.INFO)
_log_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_logger.addHandler(_log_handler)

# Seconds the Drive/Email cursor must sit still before we fetch a preview,
# and the Contacts/Email/Tasks search boxes must sit idle before we
# re-filter. All of these handlers fire on every keypress and each used to
# do its (expensive) work synchronously on each one; long enough to swallow
# a held-down arrow key or a fast typist, short enough to still feel
# immediate once you stop.
_PREVIEW_DEBOUNCE = 0.25
_CONTACTS_SEARCH_DEBOUNCE = 0.15
_EMAIL_SEARCH_DEBOUNCE = 0.15
_TASKS_SEARCH_DEBOUNCE = 0.15
_EVENTS_SEARCH_DEBOUNCE = 0.15
# Events pane "Load more" (action_load_more_events): how many more days the
# window grows by each time. Calendar's events.list is a plain date-range
# query, not cursor-paginated, so there's no natural end to reach.
_EVENTS_WINDOW_STEP_DAYS = 21
_DRIVE_SEARCH_DEBOUNCE = 0.15
_NEWS_SEARCH_DEBOUNCE = 0.15

# _apply_dashboard_extras' default for weather/stocks/word_of_day/wiki_potd:
# "this refresh has nothing new for this card, leave whatever's currently
# painted alone" -- distinct from an explicit None ("this card has no data,
# paint the friendly empty state"), which only _load_from_cache and
# _apply_dashboard_panes_enabled pass (both doing a full repaint from
# whatever's cached, where "nothing cached" really does mean empty). Without
# this distinction, a single transient fetch failure on live refresh would
# blank an already-populated card back to its empty state -- see
# _live_refresh_thread's own comment for how this plays out per-card.
_DASH_EXTRA_UNCHANGED = object()

# WEATHER card fallback when Settings.weather_location is unset AND a GeoIP
# guess (GoogleTUI._resolve_weather_location) also fails/is unavailable --
# picked as a real, working default rather than an empty string so the card
# always has something to show out of the box.
_DEFAULT_WEATHER_LOCATION = "Portland, ME"

_PREVIEWABLE_PREFIXES = (
    "text/",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
)
_PREVIEWABLE_EXTRA = {"application/json", "application/xml"}

# Outlook-style cache limits (Settings -> General). 0 means "no limit" in both,
# and is the default: this app's cache is small for most people, and silently
# throwing away their offline data by default would be a rude surprise.
_RETENTION_CHOICES = [
    ("Forever", 0),
    ("30 days", 30),
    ("90 days", 90),
    ("6 months", 180),
    ("1 year", 365),
]
_CACHE_SIZE_CHOICES = [
    ("No limit", 0),
    ("50 MB", 50),
    ("100 MB", 100),
    ("250 MB", 250),
    ("500 MB", 500),
    ("1 GB", 1024),
]
_BROWSER_START_PAGE_CHOICES = [
    ("Bookmarks", "bookmarks"),
    ("Home page", "home"),
]

# Friendly names for the cache categories in the size breakdown — the raw table
# names ("thread_summary:INBOX", "drive_file_text") mean nothing to a user
# trying to work out what's eating their disk.
_CACHE_CATEGORY_LABELS = {
    "thread_body": "Email (full messages)",
    "thread_summary": "Email (list)",
    "drive_file_text": "Drive (file contents)",
    "drive_file_meta": "Drive (file details)",
    "drive_listing": "Drive (folders)",
    "feed_entry": "News articles",
    "contact": "Contacts",
    "event": "Calendar events",
    "cal_month": "Calendar (month view)",
    "cal_week": "Calendar (week view)",
    "task": "Tasks",
    "tasklist": "Task lists",
    "label": "Mail folders",
    "gemini_cert": "Gemini certificates",
    "weather": "Weather",
    "stocks": "Stock quotes",
    "word_of_day": "Word of the day",
    "wiki_potd": "Wikipedia picture of the day",
}


def _cache_category_label(category: str) -> str:
    # thread_summary is stored per-label ("thread_summary:INBOX"), so collapse
    # the suffix before looking the name up.
    base = category.split(":", 1)[0]
    return _CACHE_CATEGORY_LABELS.get(base, base)


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


def _nearest_choice(choices: list[tuple[str, int]], value: int) -> int:
    """Coerce a settings value onto one of a Select's offered options.

    settings.json is a plain file people can (and do) hand-edit, and Textual's
    Select raises InvalidSelectValueError if handed a `value` that isn't in its
    options — i.e. a stray `"cache_max_mb": 42` would crash the Settings tab on
    open. Snap to the nearest offered value instead of exploding.
    """
    allowed = [v for _, v in choices]
    if value in allowed:
        return value
    return min(allowed, key=lambda v: abs(v - value))

HELP_GLOBAL = bindings.HELP_GLOBAL_TEXT

_KEY_METHOD_LABELS = {
    "passphrase": "Passphrase (prompt at launch)",
    "keyfile": "Local key file (no prompt)",
}

HELP_TEXT = bindings.HELP_TEXT


class GtHeader(Header):
    """Textual's Header toggles to a 3-row 'tall' mode on click by default
    (Header._on_click -> toggle_class('-tall')). Disabled — not wanted here.

    NOTE: a bare no-op override (`def _on_click(self): pass`) does NOT
    suppress this — confirmed empirically via a live pilot click. Textual's
    MessagePump._get_dispatch_methods() walks the FULL MRO and invokes the
    naming-convention handler from EVERY class that defines one (there is no
    dedup for `_on_click`-style handlers, only for `@on`-decorated ones), so
    both GtHeader._on_click AND Header._on_click would fire on a single
    click, and Header's still toggles the class. The actual fix is
    `event.prevent_default()`, whose docstring says exactly this: "prevent
    handlers in any base classes from being called" — `_get_dispatch_methods`
    checks `message._no_default_action` and `break`s out of the MRO walk
    before reaching Header's own handler.
    """
    def _on_click(self, event) -> None:
        event.prevent_default()


class TabCyclingInput(Input):
    """Input's own Ctrl+Left/Right (word-jump) bindings shadow the App-level
    tab-cycling bindings of the same keys whenever this widget has focus, so
    Ctrl+Left/Right silently stop switching tabs while the address bar is
    focused. Redefining the same keys here (subclass BINDINGS override the
    base class's for a given key) restores tab-cycling; plain word-jump
    within the URL bar isn't a loss anyone will miss.
    """
    BINDINGS = [
        ("ctrl+left", "cycle_tab_back", "Prev Tab"),
        ("ctrl+right", "cycle_tab", "Next Tab"),
    ]

    def action_cycle_tab_back(self) -> None:
        self.app.action_cycle_tab_back()

    def action_cycle_tab(self) -> None:
        self.app.action_cycle_tab()


class BookmarksTable(DataTable):
    """DataTable's own Home/End bindings (scroll_home/scroll_end) only
    scroll the viewport -- they don't move the row cursor at all, unlike
    every other list in this app where Home/End jump to the first/last row.
    Redefining the same keys here (subclass BINDINGS override the base
    class's for a given key, same trick TabCyclingInput uses above) makes
    the Browser tab's bookmarks table behave the same way.
    """
    BINDINGS = [
        ("home", "cursor_home", "Cursor Home"),
        ("end", "cursor_end", "Cursor End"),
    ]

    def action_cursor_home(self) -> None:
        if self.row_count:
            self.move_cursor(row=0)

    def action_cursor_end(self) -> None:
        if self.row_count:
            self.move_cursor(row=self.row_count - 1)


def _fmt_date(s: str) -> str:
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.strftime("%m/%d %I:%M%p")
    except Exception:
        return s


def _fmt_email_date(s: str) -> str:
    """Format a message's raw RFC 2822 "Date" header (e.g. from a Gmail
    thread summary's "date" field) the same way `_fmt_date` formats the
    ISO 8601 timestamps used elsewhere — but parsed differently, since
    email headers aren't ISO 8601."""
    if not s:
        return ""
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d.strftime("%m/%d %I:%M%p")
    except Exception:
        return ""


def _fmt_deg(v) -> str:
    """WEATHER card helper: a Fahrenheit reading, or an em dash if the API
    left the field null (Open-Meteo does this for a station gap)."""
    return f"{v:.0f}°F" if isinstance(v, (int, float)) else "—"


def _mk_id(prefix: str, raw: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)
    return f"{prefix}-{safe}"


def _unique_id(cid: str, seen: set) -> str:
    """Return `cid`, or `cid-2`/`cid-3`/… if it's already in `seen`, recording
    the result. Textual raises DuplicateIds/MountError if two mounted widgets
    share an id, and _mk_id slugifies away punctuation, so two distinct feed
    entries whose ids differ only in stripped characters (e.g. `a/b` vs `a-b`)
    would otherwise collide and crash the whole list on mount. A per-batch
    `seen` set makes every row's widget id unique regardless of input."""
    if cid not in seen:
        seen.add(cid)
        return cid
    n = 2
    while f"{cid}-{n}" in seen:
        n += 1
    uid = f"{cid}-{n}"
    seen.add(uid)
    return uid


def _dedup_by_key(entries: list[dict], key: str = "id") -> list[dict]:
    """Drop entries repeating an earlier one's `key` value, keeping the first.
    News feeds routinely surface the same article twice — the same feed URL
    configured twice, or one story syndicated across feeds sharing a guid/link
    — which showed a doubled headline AND (because each row's widget id is
    derived from the entry id) used to crash the News tab and Dashboard card
    with a duplicate-widget-id mount error."""
    seen: set = set()
    out: list[dict] = []
    for e in entries:
        k = e.get(key)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


# ---- offline CREATE temp ids -----------------------------------------------
# An event/task created while offline has no server id yet, but still needs to
# show up in its list immediately. We give it a client-side placeholder id
# until a reconnect replays the create and Google hands back the real id (see
# _replay_one_mutation's create_* branches, which reconcile temp -> real).
_TEMP_ID_PREFIX = "tmp"


def _new_temp_id() -> str:
    """Hyphen-free (uuid4().hex carries no dashes) on purpose: the Tasks pane
    packs `<list_id>-<task_id>` into one widget id and splits on the LAST
    hyphen to recover the task id (see _selected_task), so a hyphen inside the
    id half would corrupt that parse."""
    return _TEMP_ID_PREFIX + uuid.uuid4().hex


def _is_temp_id(x: object) -> bool:
    return isinstance(x, str) and x.startswith(_TEMP_ID_PREFIX)


def _is_not_found_error(e: Exception) -> bool:
    """True if `e` is Google rejecting a request because its target (thread,
    task) no longer exists — as opposed to a transient network/API error.
    Every queued-mutation replay target (reply_to/forward's threads().get(),
    set_task_status's tasks().get()) does a .get() before mutating, so a
    since-deleted target surfaces the same way regardless of mutation type:
    an HttpError with a 404 status. Distinguishing this from "still offline"
    or "quota hit" is what lets _replay_pending_mutations_thread drop a
    queued item instead of retrying it forever."""
    return isinstance(e, HttpError) and getattr(e.resp, "status", None) == 404


def _is_google_auth_error(message: str) -> bool:
    """True if `message` is a notify() string produced by a dead Google
    OAuth token (revoked/expired refresh_token). When that happens, every
    data source touched by a single refresh — mail, labels, calendar, drive
    — independently gets rejected and each formats its own
    "<Section> error: ... invalid_grant ..." toast. They all carry the same
    OAuth signature, which is what lets notify() rewrite them to one
    actionable re-auth message and dedup the burst down to a single toast."""
    low = message.lower()
    return "invalid_grant" in low or "token has been expired or revoked" in low


_PENDING_MUTATION_LABELS = {
    "reply": "Reply",
    "reply_all": "Reply All",
    "forward": "Forward",
    "new": "New message",
    "draft": "Save draft",
    "toggle_task": "Toggle task",
    "mark_unread": "Mark unread",
    "trash": "Trash",
    "archive": "Archive",
    "modify_labels": "Apply labels",
    "create_event": "New event",
    "create_task": "Add subtask",
    "delete_task": "Delete task",
}


def _pending_mutation_summary(mutation: dict) -> str:
    """One-line description for PendingMutationsModal — e.g. "Reply to
    someone@example.com" or "Toggle task complete". Deliberately doesn't try
    to resolve a thread_id/task_id back to a subject/title: that would need
    a network call or a cache lookup that might itself be stale, and this is
    read-only status text, not something worth that complexity for."""
    label = _PENDING_MUTATION_LABELS.get(mutation["type"], mutation["type"])
    if mutation["type"] == "forward":
        return f"{label} to {mutation.get('to', '')}"
    if mutation["type"] in ("new", "draft"):
        return f"{label} to {mutation.get('to', '')}"
    if mutation["type"] == "toggle_task":
        state = "complete" if mutation.get("done") else "incomplete"
        return f"{label}: mark {state}"
    if mutation["type"] == "modify_labels":
        n = len(mutation.get("add", [])) + len(mutation.get("remove", []))
        return f"{label}: {n} label(s)"
    if mutation["type"] == "create_event":
        return f"{label}: {mutation.get('summary', '')}"
    if mutation["type"] == "create_task":
        return f"{label}: {mutation.get('title', '')}"
    return label


def _fuzzy_filter_contacts(contacts: list[dict], query: str, limit: int | None = None,
                           threshold: int = 60) -> list[dict]:
    """Shared client-side fuzzy filter used by both the Contacts tab's live
    search (AGENTS.md P1 M5) and ComposeModal's To-field autocomplete.
    Filters the already-fetched `contacts` list (never re-queries Google —
    see the Contacts tab entry in AGENTS.md), scoring each contact's
    "name email" text against `query` via rapidfuzz.fuzz.partial_ratio.
    Empty query returns the input list unchanged (optionally truncated).
    """
    query = query.strip()
    if not query:
        return contacts[:limit] if limit else list(contacts)
    scored = []
    for c in contacts:
        target = f"{c.get('name','')} {c.get('email','')}".strip()
        if not target:
            continue
        score = fuzz.partial_ratio(query.lower(), target.lower())
        if score >= threshold:
            scored.append((score, c))
    scored.sort(key=lambda pair: -pair[0])
    result = [c for _, c in scored]
    return result[:limit] if limit else result


# rapidfuzz.fuzz.partial_ratio scores a short query against a much longer
# target by finding the single best-aligned window — with only 2-3
# characters to work with, that window matches unrelated text too easily
# (e.g. "cat" scores 66.7 against "Pay electric bill", comfortably clearing
# a threshold of 60). Contacts' target strings (name + email) are short
# enough that this rarely bites; threads/tasks' targets (subject+from+
# snippet, or title+notes) are not. Below this length, only an exact
# substring counts as a match — see _fuzzy_score.
_FUZZY_MIN_QUERY_LEN = 4


def _fuzzy_score(query_lower: str, target_lower: str, threshold: int) -> int | None:
    """None if `query_lower` doesn't match `target_lower` at all, else a
    score for ranking (higher = better). An exact substring always matches;
    below _FUZZY_MIN_QUERY_LEN characters that's the ONLY way to match, to
    avoid rapidfuzz.fuzz.partial_ratio's short-query false positives (see
    above)."""
    if query_lower in target_lower:
        return 100
    if len(query_lower) < _FUZZY_MIN_QUERY_LEN:
        return None
    score = fuzz.partial_ratio(query_lower, target_lower)
    return score if score >= threshold else None


def _fuzzy_filter_threads(threads: list[dict], query: str, limit: int | None = None,
                          threshold: int = 75) -> list[dict]:
    """Client-side live filter backing the Email pane's search box
    (`Input#email-search`). Filters the already-fetched `threads` list
    (self._threads_cache) — never re-queries Gmail per keystroke — matching
    each thread's "subject from snippet" text against `query` (see
    _fuzzy_score for the matching rule). Empty query returns the input list
    unchanged (optionally truncated)."""
    query = query.strip()
    if not query:
        return threads[:limit] if limit else list(threads)
    query_lower = query.lower()
    scored = []
    for th in threads:
        target = f"{th.get('subject','')} {th.get('from','')} {th.get('snippet','')}".strip()
        if not target:
            continue
        score = _fuzzy_score(query_lower, target.lower(), threshold)
        if score is not None:
            scored.append((score, th))
    scored.sort(key=lambda pair: -pair[0])
    result = [th for _, th in scored]
    return result[:limit] if limit else result


def _fuzzy_filter_tasks(tasks: list[dict], query: str, limit: int | None = None,
                        threshold: int = 75) -> list[dict]:
    """Client-side live filter backing the Tasks pane's search box
    (`Input#tasks-search`). Filters the already-fetched `tasks` list
    (self._tasks_cache) — never re-queries Google Tasks per keystroke —
    matching each task's "title notes" text against `query` (see
    _fuzzy_score for the matching rule). Empty query returns the input list
    unchanged (optionally truncated)."""
    query = query.strip()
    if not query:
        return tasks[:limit] if limit else list(tasks)
    query_lower = query.lower()
    scored = []
    for t in tasks:
        target = f"{t.get('title','')} {t.get('notes','')}".strip()
        if not target:
            continue
        score = _fuzzy_score(query_lower, target.lower(), threshold)
        if score is not None:
            scored.append((score, t))
    scored.sort(key=lambda pair: -pair[0])
    result = [t for _, t in scored]
    return result[:limit] if limit else result


def _fuzzy_filter_labels(labels: list[dict], query: str, threshold: int = 75) -> list[dict]:
    """Client-side live filter backing LabelPickerModal's search box — same
    _fuzzy_score idiom as _fuzzy_filter_threads/_fuzzy_filter_tasks above,
    scored against each label's full slash-path `name` (e.g. "Work/Projects"),
    not just its display leaf, so filtering by a parent category still
    matches its children. Empty query returns the input list unchanged."""
    query = query.strip()
    if not query:
        return list(labels)
    query_lower = query.lower()
    scored = []
    for l in labels:
        target = l.get("name", "")
        if not target:
            continue
        score = _fuzzy_score(query_lower, target.lower(), threshold)
        if score is not None:
            scored.append((score, l))
    scored.sort(key=lambda pair: -pair[0])
    return [l for _, l in scored]


def _fuzzy_filter_feeds(feeds: list[dict], query: str, threshold: int = 75) -> list[dict]:
    """Same _fuzzy_score idiom as _fuzzy_filter_labels, backing FeedPickerModal's
    search box. Scored against each feed's combined "Category — Title" label,
    so a query matches on either half."""
    query = query.strip()
    if not query:
        return list(feeds)
    query_lower = query.lower()
    scored = []
    for f in feeds:
        target = f["label"]
        score = _fuzzy_score(query_lower, target.lower(), threshold)
        if score is not None:
            scored.append((score, f))
    scored.sort(key=lambda pair: -pair[0])
    return [f for _, f in scored]


def _fuzzy_filter_events(events: list[dict], query: str, limit: int | None = None,
                         threshold: int = 75) -> list[dict]:
    """Client-side live filter backing the Events pane's search box
    (`Input#events-search`). Filters the already-fetched `self._events_cache`
    list — never re-queries Calendar per keystroke — matching each event's
    "summary description" text against `query` (see _fuzzy_score for the
    matching rule). Empty query returns the input list unchanged (optionally
    truncated)."""
    query = query.strip()
    if not query:
        return events[:limit] if limit else list(events)
    query_lower = query.lower()
    scored = []
    for e in events:
        target = f"{e.get('summary','')} {e.get('description','')}".strip()
        if not target:
            continue
        score = _fuzzy_score(query_lower, target.lower(), threshold)
        if score is not None:
            scored.append((score, e))
    scored.sort(key=lambda pair: -pair[0])
    result = [e for _, e in scored]
    return result[:limit] if limit else result


def _fuzzy_filter_drive_files(files: list[dict], query: str, limit: int | None = None,
                              threshold: int = 75) -> list[dict]:
    """Client-side live filter backing the Drive tab's search box
    (`Input#drive-search`). Filters `self._drive_files` — the CURRENT
    folder's listing only, not the whole Drive tree, and never re-queries
    Drive per keystroke — matching each file's name against `query` (see
    _fuzzy_score for the matching rule). Empty query returns the input list
    unchanged (optionally truncated)."""
    query = query.strip()
    if not query:
        return files[:limit] if limit else list(files)
    query_lower = query.lower()
    scored = []
    for f in files:
        target = (f.get("name") or "").strip()
        if not target:
            continue
        score = _fuzzy_score(query_lower, target.lower(), threshold)
        if score is not None:
            scored.append((score, f))
    scored.sort(key=lambda pair: -pair[0])
    result = [f for _, f in scored]
    return result[:limit] if limit else result


def _fuzzy_filter_news_entries(entries: list[dict], query: str, limit: int | None = None,
                               threshold: int = 75) -> list[dict]:
    """Client-side live filter backing the News tab's search box
    (`Input#news-search`). Filters the already-fetched combined-feed entry
    list — never re-fetches any feed per keystroke — matching each entry's
    "title summary" text against `query` (see _fuzzy_score for the matching
    rule). Empty query returns the input list unchanged (optionally
    truncated)."""
    query = query.strip()
    if not query:
        return entries[:limit] if limit else list(entries)
    query_lower = query.lower()
    scored = []
    for e in entries:
        target = f"{e.get('title','')} {e.get('summary','')}".strip()
        if not target:
            continue
        score = _fuzzy_score(query_lower, target.lower(), threshold)
        if score is not None:
            scored.append((score, e))
    scored.sort(key=lambda pair: -pair[0])
    result = [e for _, e in scored]
    return result[:limit] if limit else result


def _tab_label(text: str, num: int, ascii_mode: bool = False) -> str:
    digit = str(num) if ascii_mode else _SUPERSCRIPT[num]
    return f"{text} [dim]{digit}[/dim]"


# (tab_id, title text, tab number) — the source of truth _apply_ascii_mode()
# walks to relabel every main-tab's Tab widget live when the setting flips,
# and compose() below uses to build each TabPane's initial title. Keep in
# sync with the TabPane ids/order in compose() if a tab is ever added/reordered.
TAB_LABEL_SPECS: list[tuple[str, str, int]] = [
    ("tab-dashboard", "Dashboard", 1),
    ("tab-mail", "Mail", 2),
    ("tab-calendar", "Calendar", 3),
    ("tab-drive", "Drive", 4),
    ("tab-browser", "Browser", 5),
    ("tab-news", "News", 6),
    ("tab-navigation", "Navigation", 7),
    ("tab-contacts", "Contacts", 8),
    ("tab-settings", "Settings", 9),
]


def _slugify(s: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", s.strip()).strip("-").lower()
    return slug[:40] or "route"


def _nav_export_filename(result: "fetchers.RouteResult") -> str:
    return (f"route_{_slugify(result.origin)}_to_{_slugify(result.destination)}_"
            f"{dt.datetime.now():%Y%m%d-%H%M%S}.txt")


def _export_itinerary(result: "fetchers.RouteResult") -> Path:
    """Write a plain-text turn-by-turn itinerary to
    ``platformdirs.user_documents_dir()/google-tui/``. Runs synchronously on
    the main thread — it's a small local write, no worker needed."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / _nav_export_filename(result)
    lines = [
        f"Route: {result.origin} -> {result.destination}",
        f"Generated: {dt.datetime.now():%Y-%m-%d %H:%M}",
        f"Total distance: {result.distance_text}",
        f"Total duration: {result.duration_text}",
        "",
    ]
    lines += [f"{i}. {s.instruction}  ({s.distance_text}, {s.duration_text})"
              for i, s in enumerate(result.steps, start=1)]
    path.write_text("\n".join(lines) + "\n")
    return path


def _format_sender(raw: str, show_address: bool) -> str:
    """Render a raw "From" header for the list: full "Name <addr>" text only
    when the user opted in via Settings' "Show sender address in list"
    (default off); otherwise just the display name, falling back to the
    address itself when there's no name to show (e.g. bare "addr@x.com")."""
    if show_address:
        return raw
    name, addr = email.utils.parseaddr(raw)
    return name or addr


def _thread_label_chips(th: dict, labels_by_id: dict | None) -> str:
    """Comma-joined display names of a thread's applied *user* labels (not
    system ones like INBOX/UNREAD/CATEGORY_* — Gmail's own web/mobile UI
    doesn't show those as chips either, only custom labels), for the
    Email list's compact same-row labels column (kept a single line, unlike
    ThreadModal's own separate "Labels: …" line under the subject).
    `labels_by_id` is the app's `_labels_cache` reshaped to {id: label};
    None/empty (labels not loaded yet) means no chips."""
    if not labels_by_id:
        return ""
    ids = th.get("labelIds") or []
    names = sorted(
        _label_display_name(labels_by_id[lid]).strip()
        for lid in ids
        if lid in labels_by_id and labels_by_id[lid].get("type") != "system"
    )
    return ", ".join(names)


# Email-list row column layout. The mark, sender, chips and date columns are
# fixed width; the SUBJECT column is the flexible one that grows/shrinks with
# the terminal so the row fills the whole pane and the date column stays pinned
# to the right edge on any width (see GoogleTUI._email_list_width /
# _reflow_email_rows). At _EMAIL_ROW_DEFAULT_W the arithmetic reproduces the
# old fixed 36/50/20 layout exactly (subj_w == 50), so nothing shifts on a
# ~125-col terminal; wider terminals just give the subject more room instead of
# leaving dead space on the right.
_EMAIL_SENDER_W = 36
_EMAIL_CHIPS_W = 20
_EMAIL_DATE_W = 13   # len("07/20 04:05PM") -- strftime zero-pads, always 13
_EMAIL_SUBJ_MIN_W = 20
# 3 (unread mark + star mark + gap) + sender + 1 + subj + 1 + chips + 1 + date
# == width, so the fixed overhead around the flexible subject column is
# (the star column, ROADMAP P2 "star from the list", added the +1 over the
# original 2-char mark+gap prefix):
_EMAIL_ROW_FIXED_W = 3 + _EMAIL_SENDER_W + 1 + 1 + _EMAIL_CHIPS_W + 1 + _EMAIL_DATE_W
_EMAIL_ROW_DEFAULT_W = _EMAIL_ROW_FIXED_W + 50  # == 125; pre-responsive width


# Email-list DataTable columns (ROADMAP P3): the mark/From/Labels/Date columns
# are fixed width; Subject is the single flexible column (list_tables sizes it).
_EMAIL_TABLE_COLS = [("", 2), ("From", _EMAIL_SENDER_W), ("Subject", None),
                     ("Labels", _EMAIL_CHIPS_W), ("Date", _EMAIL_DATE_W)]


def _email_marks(th: dict) -> str:
    """The 2-cell leading marker column for an email summary row: an unread
    bullet (ROADMAP) followed by a ★ when the thread is starred (ROADMAP P2
    'star from the list'). A blank in either slot keeps the two columns aligned."""
    return (("•" if th.get("unread") else " ")
            + ("★" if "STARRED" in (th.get("labelIds") or []) else " "))


# Sentinel ids (not _mk_id — no underlying thread/event/file) that
# on_list_view_selected special-cases to a load-more action instead of
# opening a detail view.
# Sentinel row keys for the Email/Drive lists' "Load more" rows (each list's
# _rebuild_* appends one as an ordinary DataTable row when a next page exists
# and no search filter is active; the row-selected dispatch special-cases these
# keys to a load-more action instead of a detail view).
LOAD_MORE_EMAIL_ID = "load-more-email"
LOAD_MORE_DRIVE_ID = "load-more-drive"


def _child_tasks(task: dict, all_tasks: list[dict]) -> list[dict]:
    """Subtasks of `task` (P2, 2026-07-15). Google Tasks models a subtask as
    an ordinary task whose `parent` field points at another task's id in the
    SAME tasklist — `gauth.list_tasks` already returns that field on every
    item (it's a plain flat list, tagged with `_list` per item), so finding
    a task's children needs no extra API call: just filter the already-
    fetched list by `_list` + `parent`. `all_tasks` is normally the app's
    `self._tasks_cache` (every tasklist combined).
    """
    lid = task.get("_list")
    tid = task.get("id")
    return [t for t in all_tasks if t.get("_list") == lid and t.get("parent") == tid]


# Leading marker on a list row whose item is an offline create not yet synced
# to Google (see _merge_pending_* / _TEMP_ID_PREFIX). Purely informational — a
# normal-looking row would misleadingly read as "already saved".
_PENDING_MARK = "⏳ "


# --- Column-width constants for the DataTable list views ------------------
# Fixed columns' widths + each list's "default" content width (the fallback
# list_tables.rebuild_flat_table uses to size the one flexible column before
# the table has been laid out). DataTable measures cell width itself now, so
# the old per-row string builders (_cell_col/_truncate/_*_line) are gone.

_NEWS_DATE_W = 5    # _fmt_date's date half is "MM/DD"; clamp the rare
                    #   parse-failure fallback (a raw ISO string) to this too
_NEWS_FEED_W = 16   # feed_title column, padded inside its [...] brackets so
                    #   every title starts at the same column (the old row left
                    #   the feed name unpadded, so titles never lined up)
_NEWS_TITLE_MIN_W = 20
_NEWS_ROW_FIXED_W = _NEWS_DATE_W + 2 + (_NEWS_FEED_W + 2) + 1
_NEWS_ROW_DEFAULT_W = _NEWS_ROW_FIXED_W + 40


_CONTACT_NAME_W = 30
_CONTACT_ADDR_MIN_W = 20
_CONTACT_ROW_FIXED_W = _CONTACT_NAME_W + 1
_CONTACT_ROW_DEFAULT_W = _CONTACT_ROW_FIXED_W + 40


_DRIVE_ROW_DEFAULT_W = 40  # #drive-list-col is only 40% wide -> stays modest

# Google Drive's folder sentinel mimeType (mirrors drive_sources._GoogleBackend).
_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _with_is_folder(f: dict) -> dict:
    """Guarantee an `is_folder` key on a Drive file dict, deriving it from the
    folder mimeType when absent. Live listings already carry it (the backend's
    list_children sets it), but the `drive_listing` offline cache persists
    whatever shape was written: a cache saved by a version predating the field
    would KeyError('is_folder') the instant a row renders — an app-exiting
    worker crash seen in the wild right after upgrading with a warm cache.
    Normalizing here (mutates in place, only fills a missing key) makes every
    ingest path — cache restore, live refresh, folder nav, load-more —
    crash-proof and re-writes the healthy shape back to cache on next save."""
    if "is_folder" not in f:
        f["is_folder"] = f.get("mimeType") == _DRIVE_FOLDER_MIME
    return f


_TASK_ROW_DEFAULT_W = 50


_EVENT_WHEN_W = 8
_EVENT_ROW_FIXED_W = _EVENT_WHEN_W + 2  # right-justified "when" + 2-space gap
_EVENT_ROW_DEFAULT_W = _EVENT_ROW_FIXED_W + 34


def _event_when(e: dict) -> str:
    """An all-day event shows "all day"; a timed one shows just its local start
    time (the date is redundant on the today-scoped card)."""
    start = e.get("start", {})
    if start.get("date"):
        return "all day"
    return _fmt_date(start.get("dateTime", "")).split(" ")[-1].lower()


def _append_task_items(task_list, tasks, width: int = _TASK_ROW_DEFAULT_W,
                       by_cid: dict | None = None) -> None:
    """Populate the Dashboard's TASKS card, grouped by due status (Overdue /
    Due today / Upcoming / No due date / Done) with a dim header row before
    each non-empty group. Task rows keep their `k-<list>-<id>` widget id so the
    existing Space-toggle (action_toggle_task) and Enter-detail (TaskModal)
    handlers work unchanged; header rows carry no id, so a Space/Enter that
    lands on one is a harmless no-op (_selected_task returns None). Google
    Tasks' `due` is a date-only RFC3339 stamp at midnight UTC, so comparing the
    `YYYY-MM-DD` prefix against the local ISO date is the right granularity —
    the time component is never meaningful. Same extend()-once rationale as
    _append_email_items above."""
    today = dt.date.today().isoformat()
    groups: dict[str, list[dict]] = {"overdue": [], "today": [], "upcoming": [],
                                     "none": [], "done": []}
    for t in tasks:
        if t.get("status") == "completed":
            groups["done"].append(t)
            continue
        due = (t.get("due") or "")[:10]
        if not due:
            groups["none"].append(t)
        elif due < today:
            groups["overdue"].append(t)
        elif due == today:
            groups["today"].append(t)
        else:
            groups["upcoming"].append(t)
    headers = [("overdue", "OVERDUE"), ("today", "DUE TODAY"),
               ("upcoming", "UPCOMING"), ("none", "NO DUE DATE"), ("done", "DONE")]
    seen: set = set()
    rows: list[tuple[str, list]] = []
    for key, header in headers:
        grp = groups[key]
        if not grp:
            continue
        # Group header as a bold full-width row. Its key carries no "k-"
        # prefix, so a cursor that lands on it is a harmless no-op in every
        # task handler (_selected_task returns None) — same contract the old
        # id-less header ListItem had.
        rows.append((f"hdr-{key}", [Text(header, style="bold")]))
        for t in grp:
            cid = _unique_id(_mk_id("k", f"{t['_list']}-{t['id']}"), seen)
            box = "[x]" if t.get("status") == "completed" else "[ ]"
            pend = _PENDING_MARK if t.get("_pending") else ""
            rows.append((cid, [f"{pend}{box} {t.get('title', '')}"]))
            if by_cid is not None:
                by_cid[cid] = t
    list_tables.rebuild_flat_table(task_list, [("Tasks", None)], rows,
                                   flex_min=20, fallback_width=_TASK_ROW_DEFAULT_W)


def _todays_events(events: list[dict], tz: dt.tzinfo | None = None) -> list[dict]:
    """Filter an upcoming-events list down to just TODAY's (local date), for
    the Dashboard's TODAY card. Handles both all-day events (`start.date` /
    `end.date`, end exclusive per the Calendar API) and timed events
    (`start.dateTime`, compared in local time). Sorted all-day-first, then by
    start time. Malformed date strings are skipped rather than raising.
    `tz` overrides the OS-local timezone (config.toml's `timezone`, see
    app_config.py); defaults to OS-local when not given."""
    today = dt.datetime.now(tz).date() if tz else dt.date.today()
    out = []
    for e in events:
        start = e.get("start", {})
        if start.get("date"):  # all-day
            try:
                sd = dt.date.fromisoformat(start["date"])
                end = e.get("end", {}).get("date")
                ed = dt.date.fromisoformat(end) if end else sd + dt.timedelta(days=1)
            except Exception:
                continue
            if sd <= today < ed:
                out.append(e)
        elif start.get("dateTime"):
            try:
                sdt = dt.datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            except Exception:
                continue
            local = sdt.astimezone(tz) if sdt.tzinfo else sdt
            if local.date() == today:
                out.append(e)

    def _key(e):
        s = e.get("start", {})
        return (0, "") if s.get("date") else (1, s.get("dateTime", ""))
    return sorted(out, key=_key)


def _event_day(e: dict) -> int | None:
    s = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).day
    except Exception:
        return None


# ---- offline CREATE/DELETE reconciliation ----------------------------------
# The offline queue (self._pending_mutations) is the single source of truth for
# not-yet-synced changes. Rather than mutate the cached event/task lists and
# then have to un-mutate them on replay, every render overlays the queue onto
# the raw server/cache data: shown = server_data - pending_deletes +
# pending_creates. Because a create is removed from the queue the instant it
# replays (see _replay_pending_mutations_thread), its temp row simply stops
# being overlaid at the same moment the real row arrives in the next refresh —
# no window where both show, and nothing to persist separately (the queue
# already survives restarts via the "pending_mutation" cache category).

def _pending_event_creates(pending: dict) -> list[dict]:
    """Optimistic event dicts rebuilt from queued `create_event` mutations,
    shaped exactly like a Calendar event resource so the Events pane and the
    Month/Week grids render them with no special case."""
    out = []
    for m in pending.values():
        if m.get("type") != "create_event":
            continue
        if m.get("all_day"):
            start = {"date": m["start"]}
            end = {"date": m["end"]}
        else:
            start = {"dateTime": m["start"]}
            end = {"dateTime": m["end"]}
        out.append({"id": m["temp_id"], "summary": m.get("summary", ""),
                    "start": start, "end": end, "_pending": True})
    return out


def _pending_task_creates(pending: dict) -> list[dict]:
    """Optimistic task dicts rebuilt from queued `create_task` mutations,
    shaped like a Google Tasks resource (+ this app's `_list` tag). A create
    the user has since toggled complete carries `completed` on the mutation
    (see _toggle_pending_task); reflect that in the placeholder's status."""
    out = []
    for m in pending.values():
        if m.get("type") != "create_task":
            continue
        status = "completed" if m.get("completed") else "needsAction"
        out.append({"id": m["temp_id"], "title": m.get("title", ""),
                    "status": status, "parent": m.get("parent"),
                    "notes": "", "_list": m["list_id"], "_pending": True})
    return out


def _pending_deleted_task_keys(pending: dict) -> set:
    return {(m["list_id"], m["task_id"]) for m in pending.values()
            if m.get("type") == "delete_task"}


def _event_start_key(e: dict) -> str:
    s = e.get("start", {})
    return s.get("dateTime") or s.get("date") or ""


def _merge_pending_events(events: list[dict], pending: dict) -> list[dict]:
    """Server/cache events with queued offline creates overlaid. Re-sorted by
    start only when something was actually added, so the common (empty-queue)
    path returns the input untouched — no risk to the server's existing order."""
    creates = _pending_event_creates(pending)
    if not creates:
        return list(events)
    merged = list(events) + creates
    merged.sort(key=_event_start_key)
    return merged


def _merge_pending_tasks(tasks: list[dict], pending: dict) -> list[dict]:
    """Server/cache tasks with queued offline deletes removed and queued
    offline (sub)task creates added."""
    deleted = _pending_deleted_task_keys(pending)
    creates = _pending_task_creates(pending)
    if not deleted and not creates:
        return list(tasks)
    kept = [t for t in tasks if (t.get("_list"), t.get("id")) not in deleted]
    return kept + creates


def _event_date_obj(e: dict) -> dt.date | None:
    raw = _event_start_key(e)
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _event_in_month(e: dict, year: int, month: int) -> bool:
    d = _event_date_obj(e)
    return d is not None and d.year == year and d.month == month


def _event_in_week(e: dict, week_start: dt.date) -> bool:
    d = _event_date_obj(e)
    return d is not None and week_start <= d < week_start + dt.timedelta(days=7)


def _bg_cell(text: str, color: str | None) -> str | Text:
    """A single-event Week-view cell (hour or all-day row), background-colored
    by that event's `_color` (see gauth.events_between). Multi-event cells
    ("N events") don't call this -- there's no single color to attribute a
    combined cell to, so those stay plain text."""
    if not text or not color:
        return text
    styled = Text(text)
    styled.stylize(f"on {color}")
    return styled


def _is_previewable(mime: str) -> bool:
    return mime.startswith(_PREVIEWABLE_PREFIXES) or mime in _PREVIEWABLE_EXTRA


def _is_markdown_file(name: str, mime: str) -> bool:
    # mimetypes.guess_type (drive_sources._guess_mime) already resolves
    # ".md" to "text/markdown" on systems whose /etc/mime.types knows it,
    # but that registry lookup is platform-dependent -- the extension check
    # is the reliable half of this, ``mime`` just widens it for a Drive
    # file whose Google-assigned mimeType happens to already say so.
    return mime == "text/markdown" or name.lower().endswith((".md", ".markdown"))


# ---------------------------------------------------------------------------
# Browser tab glue (M2) — address classification + search-result linkifying.
# These live here (not render.py) because they're specific to this app's
# omnibox behavior / one opaque CLI's (hermes web search) output shape, not
# general protocol parsing. See ROADMAP M2 design notes.
# ---------------------------------------------------------------------------

_SCHEME_PREFIXES = ("http://", "https://", "gopher://", "gemini://", "ftp://")
_BARE_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+(:\d+)?(/\S*)?$"
)


def _classify_address(raw: str) -> tuple[str, str]:
    """Omnibox-style classification of Browser-tab address-bar input.

    -> (mode, target) where mode is 'http'|'gopher'|'gemini'|'ftp'|'sftp'|
    'search'. An explicit scheme always wins; a single dotted-word-with-no
    -space is treated as a bare domain and gets "https://" prepended;
    everything else (including any input containing a space) is a web
    search via ``fetchers.run_search``. ``search:`` is an explicit escape
    hatch for the rare case of wanting to search for literally
    "example.com". ftp/sftp don't fetch inline (see
    GoogleTUI._redirect_to_drive_source) — remote-filesystem browsing lives
    in the Drive tab now, not Browser.
    """
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return "http", raw
    if raw.startswith("gopher://"):
        return "gopher", raw
    if raw.startswith("gemini://"):
        return "gemini", raw
    if raw.startswith("ftp://"):
        return "ftp", raw
    if raw.startswith("sftp://"):
        # Previously fell through to the bare-domain regex below (which
        # requires no "://", so never matched) and got silently treated as
        # a literal web search for the whole URL string — a latent bug,
        # fixed as a side effect of adding real sftp:// support.
        return "sftp", raw
    if raw.startswith("search:"):
        return "search", raw[len("search:"):].strip()
    if " " not in raw and _BARE_DOMAIN_RE.match(raw):
        return "http", "https://" + raw
    return "search", raw


# Browser tab bookmarks list — per-protocol icon + color so folder contents
# are scannable at a glance (ROADMAP P3 "color-code bookmarks by protocol").
# Bookmark data itself now lives in Settings.browser_bookmarks, editable via
# the ListView built by GoogleTUI._bookmarks_render.
_BOOKMARK_PROTOCOL_STYLE = {
    "http": ("🌐", "cyan"), "https": ("🌐", "cyan"),
    "gopher": ("🕳", "magenta"),
    "gemini": ("♊", "green"),
    "ftp": ("📁", "yellow"),
    "sftp": ("🔒", "yellow"),
}


def _bookmark_scheme_style(url: str) -> tuple[str, str]:
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    # Fallback matches the app's actual theme foreground (#e0e0e0, textual-dark)
    # rather than the raw ANSI "white" keyword, which some terminals remap.
    return _BOOKMARK_PROTOCOL_STYLE.get(scheme, ("🔗", "#e0e0e0"))


# Bookmark list sort modes, cycled by the Browser tab's "S" key (M2 follow-up,
# 2026-07-22) -- "name" first since it's the most predictable default for a
# fresh/legacy list with no timestamps at all.
_BOOKMARK_SORT_CYCLE = ["name", "added", "used"]
_BOOKMARK_SORT_LABELS = {"name": "Name", "added": "Added", "used": "Used"}


def _sort_bookmark_entries(entries: list[dict], mode: str) -> list[dict]:
    """Sort a shallow copy of one bookmark-folder level for display. Missing
    added_at/last_opened_at (legacy entries, or folders that were never
    entered) sort as "" -- oldest/never-used, never an error."""
    if mode == "added":
        return sorted(entries, key=lambda e: e.get("added_at") or "", reverse=True)
    if mode == "used":
        return sorted(entries, key=lambda e: e.get("last_opened_at") or "", reverse=True)
    return sorted(entries, key=lambda e: (e.get("label") or e.get("url") or "").lower())


@dataclass
class BrowserHistoryEntry:
    """One frame of the Browser tab's in-memory (session-lifetime only —
    see ROADMAP M2) back/forward stack. Holds the already-fetched Document,
    not just the URL, so Back/Forward never re-fetches.
    """
    url: str
    document: render.Document | None
    scroll_y: float = 0.0


_SYSTEM_LABEL_ORDER = ["INBOX", "STARRED", "SENT", "DRAFT", "IMPORTANT"]


def _label_display_name(label: dict) -> str:
    name = label["name"]
    if label.get("type") == "system":
        return name.replace("_", " ").title()
    depth = name.count("/")
    leaf = name.rsplit("/", 1)[-1]
    return ("  " * depth) + leaf


def _initial_label_select_options(default_label_id: str) -> list[tuple[str, str]]:
    """Options for `#email-label-select` at compose time, before the real
    label list (with proper display names) has loaded from cache/API.
    Always includes All Mail/Inbox; also includes `default_label_id` itself
    as a raw-id placeholder option when it's a custom label, so the Select
    can validly hold a saved custom-label default instead of silently
    falling back to Inbox — `_apply_labels` replaces these placeholder
    options with the real display names once the label list arrives."""
    options = [("All Mail", "ALL"), ("Inbox", "INBOX")]
    if default_label_id not in ("ALL", "INBOX"):
        options.append((default_label_id, default_label_id))
    return options


_DRIVE_ADD_HOST_VALUE = "__add__"
_DRIVE_SOURCE_SEED_OPTIONS = [("Google Drive", "google"), ("+ Add remote host…", _DRIVE_ADD_HOST_VALUE)]


def _drive_source_select_options(key: bytes | None,
                                  active: "drive_sources.DriveBackend | None" = None) -> list[tuple[str, str]]:
    """Options for `#drive-source-select`: Google Drive, one per saved
    FTP/SSH host (value = the same source_key drive_sources.build_source's
    callers use), then the add-host sentinel. `active` (the currently
    connected backend, which may be an unsaved ephemeral connection made via
    "+ Add remote host…" with "Save this host" off) is included even if it
    has no saved entry, so the Select can validly display it."""
    options = [("Google Drive", "google")]
    seen = {"google"}
    for protocol, host, port in remote_creds.list_hosts(key):
        source_key = drive_sources.source_key_for(protocol, host, port)
        if source_key not in seen:
            options.append((f"{protocol}://{host}", source_key))
            seen.add(source_key)
    if active is not None and active.source_key not in seen:
        options.append((active.label, active.source_key))
    options.append(("+ Add remote host…", _DRIVE_ADD_HOST_VALUE))
    return options


def _split_source_key(source_key: str) -> tuple[str, str, int]:
    """"protocol:host:port" -> (protocol, host, port) -- the inverse of
    drive_sources.source_key_for. Doesn't handle a bare (unbracketed) IPv6
    host, which would itself contain extra colons -- out of scope for v1."""
    protocol, rest = source_key.split(":", 1)
    host, port_s = rest.rsplit(":", 1)
    return protocol, host, int(port_s)


def _label_select_options(labels: list[dict]) -> list[tuple[str, str]]:
    system = [l for l in labels if l.get("type") == "system"]
    user = [l for l in labels if l.get("type") != "system"]
    system.sort(key=lambda l: (_SYSTEM_LABEL_ORDER.index(l["name"])
                               if l["name"] in _SYSTEM_LABEL_ORDER else 99, l["name"]))
    user.sort(key=lambda l: l["name"])
    options = [("All Mail", "ALL")]
    options += [(_label_display_name(l), l["id"]) for l in system]
    options += [(_label_display_name(l), l["id"]) for l in user]
    return options


# Some terminals encode Alt+Arrow as a literal double-ESC sequence
# (``ESC ESC [ A/B/C/D``) instead of the CSI-with-modifier-parameter form
# (``ESC [ 1;3 A/B/C/D``). Confirmed by feeding both forms directly through
# Textual 8.2.8's XTermParser: the CSI-1;3 form correctly yields a single
# ``Key(key='alt+left', ...)`` event, but the double-ESC form hits a
# hardcoded ``process_alt=False`` in ``_xterm_parser.py`` (triggered when a
# second ESC interrupts the still-unresolved first escape sequence) and
# instead yields two INDEPENDENT bare events: ``Key('escape', ...)`` then
# ``Key('left', ...)``. The stray arrow half then either moves the address
# bar's text cursor (``Input`` binds bare left/right to cursor movement) or
# is silently dropped (``DocumentView`` has no bare-arrow binding) — this,
# not "focus swallowing the whole combo", is why Alt+Left/Right/Up/Down were
# reported as dead from some terminals. See ``GoogleTUI.on_key`` below for
# the compensation and CHANGELOG.md for the repro that found this.
_ESCAPE_ALT_ARROW_ACTIONS = {
    "left": "action_switch_left",
    "right": "action_switch_right",
    "up": "action_switch_up",
    "down": "action_switch_down",
}
# The two halves of one real escape sequence land in the same feed() call —
# effectively 0 elapsed wall-clock time — while two genuinely separate human
# keypresses (e.g. Escape to close a modal, then later an unrelated Left
# arrow) are always much further apart than this. 50ms comfortably separates
# the two cases without misfiring on real sequential keypresses.
_ESCAPE_ALT_ARROW_WINDOW = 0.05

# "/" (action_focus_search) reveals these pane/tab search bars on demand;
# Esc clears + re-hides them and hands focus back to the list/grid they
# filter — same "hidden until summoned" pattern as ThreadModal's
# #thread-search (see ThreadModal.action_focus_search/_hide_search). Keyed
# by the Input's id; value is (wrapping Horizontal's id, id of the widget to
# refocus on hide). cal-search's refocus target depends on which of the two
# Month/Week grids is active, so it's resolved in _hide_pane_search instead
# of hardcoded here.
_PANE_SEARCH_BARS: dict[str, tuple[str, str | None]] = {
    "email-search": ("email-bar", "email-list"),
    "tasks-search": ("tasks-bar", "task-list"),
    "events-search": ("events-bar", "event-list"),
    "cal-search": ("cal-search-bar", None),
    "drive-search": ("drive-search-bar", "drive-list"),
    "news-search": ("news-search-bar", "news-list"),
}


class GoogleTUI(App):
    CSS = """
    Screen { layout: vertical; }
    /* ModalScreen's own DEFAULT_CSS is "layout: vertical" with no alignment,
       so a dialog box smaller than the screen (width/height < 100%, e.g.
       #reauth-box, #onboarding-box, #snooze-box, ...) anchors top-left
       instead of floating in the middle of the darkened backdrop. Full-panel
       modals (bare `.pane`, no size override -- see the #hermes-popup-box
       note below) are unaffected: Container defaults to width/height 1fr,
       which still fills 100% of the ModalScreen regardless of align. */
    ModalScreen { align: center middle; }
    /* Same darkened backdrop (ModalScreen sets background: $background 60%)
       swallows the resting `.pane` border color ($panel-darken-2 below) on
       reduced-color terminals -- worse than dim, often invisible at 16
       colors or none at all. Every dialog box is the one thing on screen
       that's supposed to draw the eye, so give it the same $accent border
       `.pane-active` uses for "this is the focused one" -- reusing that
       existing visual language instead of inventing a new one. Scoped to
       ModalScreen descendants so the main screen's dashboard grid (where
       active/inactive panes are a real distinction) is untouched. */
    ModalScreen .pane { border: round $accent; }
    #main-tabs { height: 1fr; }
    /* Pin the whole TabbedContent chain to a definite height instead of
       leaning on Textual implicitly stretching the active TabPane to fill.
       Textual's own defaults make ContentSwitcher AND TabPane `height: auto`
       (size-to-content) -- 8.2.8 happens to stretch the shown pane to fill a
       1fr TabbedContent anyway, but that's version-dependent behavior, not a
       contract: a newer Textual (this repo doesn't pin the version) stops
       doing it, and then any `height: 1fr` content inside a pane -- most
       visibly the Dashboard's card Grid -- collapses to content height and
       won't grow when the terminal is made TALLER (width still tracks,
       because the width chain is definite; only the height chain was
       auto). These two `> ` (direct-child) rules make the height chain
       definite regardless of Textual version, so 1fr children resolve.
       Direct-child selectors deliberately DON'T reach the Settings tab's
       nested inner TabbedContent/TabPanes -- those keep their own auto
       height so their VerticalScroll content sizes normally. */
    #main-tabs > ContentSwitcher { height: 1fr; }
    #main-tabs > ContentSwitcher > TabPane { height: 1fr; }
    #main-tabs > ContentTabs { height: 1; background: $primary; }
    #main-tabs > ContentTabs Underline { display: none; }
    #main-tabs > ContentTabs Tab { color: $text; }
    #main-tabs > ContentTabs Tab.-active { background: $accent; color: $text; text-style: bold; }
    #body { height: 1fr; }
    #left { width: 65%; }
    #right { width: 1fr; border: round $panel-darken-1; padding: 0 1; }
    /* Dashboard card grid + full-width Hermes row (2026-07-17, grown
       2026-07-19 with the weather/stocks/word-of-day/picture-of-day cards;
       restructured 2026-07-23 into two independently-sized columns). Nesting:
       #dashboard-body (Vertical) > #dash-cards (Horizontal) > #dash-col-wide /
       #dash-col-narrow (each a Vertical stack of cards), with #hermes as
       #dashboard-body's second child, full width below both columns.
       Mail/News/Word-of-the-day/Picture-of-the-day want more room for prose
       (unread subjects, headlines, definitions) than Time/Tasks/Weather/
       Stocks' glanceable content needs, hence the 65/35 split rather than
       equal columns. Each column's cards are height:1fr among however many
       are ENABLED in that column (Settings -> Dashboard) -- no fixed row
       count, so any enable/disable combo tiles without a blank trailing gap
       (the same bug a fixed 5-row single grid hit in the old layout,
       2026-07-22). */
    #dashboard-body { height: 1fr; }
    /* #dash-cards takes all the vertical space HERMES doesn't. HERMES is
       height:auto so it's just its title + input bar (~a few rows) until the
       first question is asked -- #hermes-log starts `.hidden` and _hermes_submit
       reveals it, at which point HERMES grows by the log's fixed height and the
       card grid gives that space back. Keeps an empty response box from
       squatting ~1/5 of the tab before it has anything to show. */
    #dash-cards { height: 1fr; }
    #dash-col-wide { width: 65%; height: 1fr; }
    #dash-col-narrow { width: 1fr; height: 1fr; }
    #hermes { height: auto; }
    #dash-mail-list, #dash-news-list, #dash-weather-list, #dash-stocks-list,
    #dash-word-list, #dash-potd-list { height: 1fr; }
    /* CLOCK card: Textual's built-in Digits widget (big block-character
       numbers, no external dependency) for local time, then one plain-text
       line per Settings.clock_timezones entry underneath. Both auto-height --
       the card itself is height:1fr among its narrow-column siblings same as
       any other, it just won't use all of it (a clock has no reason to grow
       to fill leftover space the way a list does). */
    #dash-clock-digits { height: auto; }
    #dash-clock-zones { height: auto; }
    /* CALENDAR card: now its own dedicated, fully-interactive DataTable
       (cursor_type="cell" -- arrow keys move the highlighted day, Enter opens
       it, see _dash_cal_cell_selected), so it gets the same height:1fr
       treatment as any other single-content card. */
    #dash-calendar-grid { height: 1fr; }
    /* Label defaults to `width: auto` (size-to-content, one unwrapped line) --
       with no width to wrap against, a long WORD OF THE DAY definition or
       empty-state message ("Not available yet...") just renders as one long
       line and gets clipped at the card's edge instead of wrapping. These
       four cards' Labels are the only Dashboard content still built from
       plain multi-line strings (not a DataTable), so constrain them to the
       card's width to let Rich actually word-wrap. */
    #dash-weather-list Label, #dash-stocks-list Label,
    #dash-word-list Label, #dash-potd-list Label { width: 1fr; }
    /* TASKS/MAIL group-header + Email multi-select rows are styled per-cell on
       their DataTables now (bold Text / _email_sel_style), not via a CSS class. */
    #email-preview-meta { height: auto; border-bottom: solid $panel-darken-2; padding-bottom: 1; }
    .pane { height: 1fr; border: round $panel-darken-2; padding: 0 1; }
    .pane-active { border: round $accent; }
    .pane-title-row { height: 1; }
    .pane-title-text { text-style: bold; color: $accent; width: 1fr; }
    #email-label-select { height: 3; }
    #email-search, #tasks-search, #events-search { width: 1fr; }
    #email-list { height: 1fr; }
    /* Multi-select (ROADMAP P2/P3): a checked-for-bulk-action row is tinted by
       re-styling its cells (see _email_summary_cells / _email_sel_style) —
       DataTable has no per-row CSS class the way the old ListView row did. */
    #bulk-box { height: auto; }
    #bulk-box Button { width: 1fr; margin-top: 1; }
    #snooze-box { height: auto; }
    #snooze-box Vertical { height: auto; }
    #snooze-box Vertical Button { width: 1fr; margin-top: 1; }
    #snooze-box #sn-custom { margin-top: 1; }
    #att-box { height: auto; max-height: 90%; }
    #att-list { height: auto; max-height: 20; border: round $panel-darken-1; margin: 1 0; }
    #c-attach { width: 1fr; margin-right: 1; }
    #c-attach-list { height: auto; color: $text 75%; }
    #event-list, #task-list { height: 1fr; }
    /* Revealed (its `.hidden` removed) on the first question -- see
       _hermes_submit. Fixed height so the auto-height #hermes pane has a
       definite size to grow to (a 1fr child of an auto parent would collapse). */
    #hermes-log { height: 8; border: round $panel-darken-1; }
    #hermes-input { height: 3; }
    .muted { color: $text 75%; }
    .btnrow { height: 3; align: left middle; }
    #send-countdown { height: 1; color: $accent; text-style: bold; }
    .section { height: 1fr; border: round $panel-darken-2; padding: 0 1; }
    #cal-search { width: 1fr; }
    #cal-grid, #cal-week-grid { height: 1fr; }
    #drive-body { height: 1fr; }
    #drive-list-col { width: 40%; border: round $panel-darken-1; }
    #drive-preview-col { width: 1fr; border: round $panel-darken-1; padding: 0 1; }
    #drive-preview-meta { height: auto; border-bottom: solid $panel-darken-2; padding-bottom: 1; }
    #drive-preview-text { height: 1fr; }
    #drive-preview-doc { height: 1fr; }
    /* EventModal/TaskModal already show Summary/Title in the fixed-fields
       Static above; DocumentView's own auto-title bar would just repeat
       that (or show "(untitled)" for the common no-heading case). */
    #ev-desc #doc-title, #tk-desc #doc-title { display: none; }
    #drive-search { width: 1fr; }
    #browser-bar { height: 3; align: left middle; }
    #browser-mode { width: 10; color: $accent; text-style: bold; content-align: center middle; }
    #browser-url { width: 1fr; }
    #browser-status { width: auto; color: $text 75%; margin-left: 1; }
    #browser-bookmarks { height: 1fr; border: round $panel-darken-1; margin-bottom: 1; }
    #browser-doc { height: 1fr; border: round $panel-darken-1; padding: 0 1; }
    #news-search { width: 1fr; }
    #news-list { height: 1fr; }
    #nav-origin, #nav-destination { width: 1fr; margin-right: 1; }
    #nav-summary { color: $accent; text-style: bold; height: 1; margin: 1 0; }
    #nav-log { height: 1fr; border: round $panel-darken-1; }
    #thread-messages { height: 1fr; }
    #thread-search { margin-bottom: 1; }
    #thread-help { color: $text 75%; height: auto; margin-top: 1; link-style: none; }
    #labelpick-box { height: auto; max-height: 80%; }
    #labelpick-list { height: auto; max-height: 20; border: round $panel-darken-1; margin-bottom: 1; }
    #feedpick-box { height: auto; max-height: 80%; }
    #feedpick-list { height: auto; max-height: 20; border: round $panel-darken-1; margin-bottom: 1; }
    .thread-msg-header { color: $text 75%; text-style: bold; margin-top: 1; border-bottom: solid $panel-darken-2; }
    #help-bar { height: auto; background: $panel; padding: 0 1; }
    /* link-style: none removes the underline Textual draws on [@click]
       action links by default -- the context help row's clickable "Key Label"
       spans (bindings.apply_click_actions) are still clickable, just no
       longer underlined, so the whole row reads as one uniform hint strip. */
    #help-context { color: $text; link-style: none; }
    #help-global { color: $text 75%; }
    #settings-remote-hosts-list { height: 1fr; border: round $panel-darken-1; margin-bottom: 1; }
    .settings-row { height: 3; align: left middle; }
    .settings-row Label { width: auto; margin-right: 2; }
    #settings-nous-key { width: 40; margin-right: 2; }
    .hidden { display: none; }
    #settings-key-method { height: auto; margin: 1 0; }
    #settings-cache-info { margin-top: 1; }
    #settings-feed-list { height: 1fr; border: round $panel-darken-1; margin-bottom: 1; }
    #settings-dashboard-panes { height: auto; max-height: 12; border: round $panel-darken-1; margin-top: 1; }
    #settings-feed-url { width: 1fr; margin-right: 2; }
    #settings-google-cse-key, #settings-google-cse-id, #settings-searxng-url { width: 40; margin-right: 2; }
    #settings-google-group, #settings-searxng-group { height: auto; }
    #settings-routes-key { width: 40; margin-right: 2; }
    #contacts-search { width: 1fr; margin-right: 1; }
    #contacts-list { height: 1fr; }
    #drive-list { height: 1fr; }
    #c-to-suggestions { height: auto; max-height: 6; border: round $panel-darken-1; }
    #ett-notes { height: 8; border: round $panel-darken-1; margin-top: 1; }
    #ett-list { width: 1fr; margin-bottom: 1; }
    #unlock-box { height: auto; }
    #onboarding-box { width: 90%; height: 80%; }
    /* Ctrl+K quick-ask popup (HermesAskModal) -- deliberately NO explicit
       width/height on #hermes-popup-box: every other ModalScreen in this app
       (ContactModal, GeminiInputModal, TaskModal, ...) relies on bare `.pane`
       (height: 1fr, width defaults to fill) for a clean full-screen-panel
       render with no gap. A tried `width: 80%; height: 70%` here left an
       uncovered strip that visibly bled through to the screen underneath
       (confirmed via a side-by-side screenshot against GeminiInputModal,
       which has no such gap) -- reverted rather than chasing that down,
       since matching the established convention is simpler and proven-good.
    */
    #hermes-popup-log { height: 1fr; border: round $panel-darken-1; margin-top: 1; }
    #hermes-popup-input { dock: bottom; }
    /* The re-auth box grew a Copy URL / Save to file row; give it room to
       scroll rather than pushing the paste Input off the bottom of the screen
       on a short terminal. The URL itself is ~400 chars and must wrap, not be
       clipped — a clipped URL is an unusable URL. */
    #reauth-box { width: 90%; height: auto; max-height: 90%; overflow-y: auto; }
    #reauth-instructions-1 { margin-bottom: 1; }
    #reauth-url { width: 1fr; height: auto; color: $accent; margin: 1 0; }
    #reauth-copy-help { height: auto; margin-bottom: 1; }
    #reauth-instructions-2 { margin-bottom: 1; }
    #reauth-code-input { margin-bottom: 1; }
    #reauth-status { margin-top: 1; }
    #onboarding-scroll { height: 1fr; }
    #unlock-error { height: 1; }

    /* Settings -> General -> "ASCII-safe mode" (P2, 2026-07-15): _apply_ascii_mode()
       toggles the "ascii-border" class on these same containers rather than
       recompiling CSS at runtime — a plain class flip works fine here, and
       these rules' extra class in the selector gives them the specificity
       to override the round/solid declarations above regardless of
       declaration order (the .pane/.pane-active order still matters
       between these two rules, for the same reason it does above). */
    .pane.ascii-border, .section.ascii-border { border: ascii $panel-darken-2; }
    .pane-active.ascii-border { border: ascii $accent; }
    /* Dialog boxes keep the $accent "stand out" border in ASCII-safe mode
       too (just swapping round-corner box-drawing for plain +/-/| chars,
       same as every other .ascii-border rule here) -- this is the mode the
       "stand out" fix matters most for, so it can't regress back to
       $panel-darken-2 the moment ascii-border is toggled on. */
    ModalScreen .pane.ascii-border { border: ascii $accent; }
    #hermes-log.ascii-border { border: ascii $panel-darken-1; }
    #drive-list-col.ascii-border { border: ascii $panel-darken-1; }
    #drive-preview-col.ascii-border { border: ascii $panel-darken-1; }
    #right.ascii-border { border: ascii $panel-darken-1; }
    #browser-doc.ascii-border { border: ascii $panel-darken-1; }
    #nav-log.ascii-border { border: ascii $panel-darken-1; }
    #settings-feed-list.ascii-border { border: ascii $panel-darken-1; }
    #browser-bookmarks.ascii-border { border: ascii $panel-darken-1; }
    #settings-remote-hosts-list.ascii-border { border: ascii $panel-darken-1; }
    #c-to-suggestions.ascii-border { border: ascii $panel-darken-1; }
    #drive-preview-meta.ascii-border { border-bottom: ascii $panel-darken-2; }
    #email-preview-meta.ascii-border { border-bottom: ascii $panel-darken-2; }
    .thread-msg-header.ascii-border { border-bottom: ascii $panel-darken-2; }

    /* Narrow-terminal responsive layout (P2, 2026-07-15) -- see the
       NARROW_WIDTH_THRESHOLD comment above for the breakpoint mechanism.

       Drive tab: STACK list-over-preview rather than hide either one. Both
       are genuinely useful at once even at 80 columns (the list to keep
       browsing, the preview's who/what/where/when + text to actually read
       something) and there are only two of them, so a 60/40 height split
       still leaves each one usable in a 25-row terminal -- hiding the
       preview would leave Drive as a bare filename list with no way to see
       what's selected without opening it.
    */
    Screen.-narrow #drive-body { layout: vertical; height: 1fr; }
    Screen.-narrow #drive-list-col { width: 1fr; height: 60%; }
    Screen.-narrow #drive-preview-col { width: 1fr; height: 1fr; }

    /* "p" toggle (action_toggle_preview) -- manual override, on top of
       (not instead of) the narrow/normal stacking above: hiding the preview
       column lets #drive-list-col claim the full row/height in EITHER
       layout mode. The id+class selectors below out-specificity the bare
       id ones above regardless of source order, in both branches. */
    #drive-preview-col.drive-preview-hidden { display: none; }
    #drive-list-col.drive-list-full { width: 1fr; }
    Screen.-narrow #drive-list-col.drive-list-full { height: 1fr; }

    /* Dashboard tab: HIDE the inactive pane instead of stacking. Events/
       Tasks/Hermes stacked 3-high already squeezes an already-scarce 25
       rows; showing exactly ONE pane full width/full height (whichever is
       "active" -- Tab/arrows already track that via
       _focus_dash_pane, this just also hides the rest when narrow) keeps
       the primary content dominant instead of squeezed. See
       GoogleTUI._apply_narrow_layout, which toggles this class. */
    .narrow-hidden { display: none; }
    /* ...and let whichever of #dash-col-wide/#dash-col-narrow holds the one
       still-visible card (the rest are .narrow-hidden'd, including -- via
       _apply_narrow_layout -- the OTHER column itself when it holds none of
       the active pane) take the full tab width instead of its non-narrow
       65%/35% share. When HERMES is the active pane, _apply_narrow_layout
       .narrow-hidden's #dash-cards itself (both columns are irrelevant then)
       so HERMES -- normally the auto-height bottom slice -- gets the whole
       tab instead of an empty grid hogging most of it. */
    Screen.-narrow #dash-col-wide, Screen.-narrow #dash-col-narrow { width: 1fr; }

    /* Settings -> Dashboard checklist (2026-07-18): a disabled card is
       hidden the same way (display: none) regardless of narrow/normal --
       independent of .narrow-hidden above (a card can be both, or just
       this one; either alone is enough to hide it). See
       GoogleTUI._apply_dashboard_panes_enabled. */
    .dash-pane-disabled { display: none; }

    /* Mail tab: Email's list+preview split stacks (doesn't hide) when
       narrow, same rationale as Drive's list+preview column -- a preview
       is still genuinely useful at 80 columns. Only relevant when the "p"
       toggle has the preview visible at all; #right's own
       .email-preview-hidden class (added alongside the toggle) still wins
       over this when the preview is off, same source-order trick already
       used for Drive's toggle above. */
    Screen.-narrow #body { layout: vertical; height: 1fr; }
    Screen.-narrow #left { width: 1fr; height: 60%; }
    Screen.-narrow #right { width: 1fr; height: 1fr; }

    /* "p" toggle (action_toggle_preview) -- hidden by default (Settings.
       email_preview_default_visible). Same id+class-after-narrow-block
       source-order trick as Drive's toggle above. */
    #right.email-preview-hidden { display: none; }
    #left.email-list-full { width: 1fr; }
    Screen.-narrow #left.email-list-full { height: 1fr; }
    """

    # Screen.-narrow / Screen.-normal, applied automatically by Textual on
    # every resize (Screen._on_resize) -- see the NARROW_WIDTH_THRESHOLD
    # comment above. Drives the Drive-tab CSS above with no Python code;
    # GoogleTUI.on_resize below uses the same threshold for the Mail-tab
    # active-pane-hide logic a CSS selector can't express on its own.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (NARROW_WIDTH_THRESHOLD, "-normal")]

    # Generated from google_tui/bindings.py — the single source of truth for
    # this app's keymap (see that module's docstring).
    BINDINGS = bindings.bindings_for_scope("global")

    def __init__(self):
        super().__init__()
        self.active = 0
        self._dash_active = "dash-today"  # which DASH_PANE_IDS card is focused on tab-dashboard
        self.app_config: AppConfig = load_config()
        # Tab/Shift+Tab cycle order for the Dashboard's cards -- DASH_PANE_IDS
        # unless config.toml's pane_order customizes it (see app_config.py).
        # Only reorders the cycle, never the fixed visual grid position/
        # DASH_ADJACENCY, which stay exactly as authored regardless.
        self._dash_cycle_ids: list[str] = self._resolve_dash_cycle_ids()
        # Enabled Dashboard cards, library order, filtered/defaulted from
        # Settings.dashboard_panes_enabled by _apply_dashboard_panes_enabled
        # (called from on_mount, and again whenever the Settings -> Dashboard
        # checklist changes) -- empty here only until that first runs.
        self._dash_enabled_ids: list[str] = []
        # Thread ids checked for a multi-select bulk action (ROADMAP P2). A
        # CSS class on the ListItem shows the check; the set is the source of
        # truth, pruned to what's loaded on every re-render (see
        # _apply_email_list_async) so a stale id can't linger in it.
        self._email_selected: set[str] = set()
        self._tasklists = []
        now = dt.datetime.now()
        self._cal_year, self._cal_month = now.year, now.month
        self._cal_by_day: dict[int, list[dict]] = {}
        self._cal_week_cells: dict[tuple[int, int], list[dict]] = {}
        self._cal_week_allday: dict[int, list[dict]] = {}
        # Calendar tab "/" jump-to-next-match state (find-next over the grid,
        # mirrors ThreadModal._find). _cal_search_matches is the last query's
        # ordered (row, col) hit list; a repeat-Enter of the SAME query
        # advances _cal_search_pos through it — see _cal_find.
        self._cal_search_matches: list[tuple[int, int]] = []
        self._cal_search_pos: int = -1
        today = dt.date.today()
        self._cal_week_start = today - dt.timedelta(days=today.weekday())
        self._drive_folder_id = "root"
        self._drive_path = "/"
        # Ancestor (folder_id, path) pairs from root down to (but not
        # including) the current folder — lets "up" navigate to the actual
        # parent instead of always reloading root. Pushed on descend, popped
        # on ascend; reset to [] whenever _apply_drive_files loads path "/",
        # which covers both the explicit root case and the full-refresh
        # reset-to-root in _apply_live_refresh.
        self._drive_folder_stack: list[tuple[str, str]] = []
        self._drive_files: list[dict] = []
        # "p" toggle (action_toggle_preview) — visible by default;
        # #drive-preview-col already fits (stacked below the list) even on a
        # narrow terminal per the CSS comment below, so there's no width-based
        # reason to default it off.
        self._drive_preview_visible = True
        # Drive's pagination cursor for the page AFTER the currently-shown
        # folder listing, or None if there isn't one — set by
        # _fetch_drive_files (always reflects whichever folder was fetched
        # most recently), read by action_load_more_drive and the "Load
        # more" row _rebuild_drive_table appends when it's not None. Same
        # shape as _email_next_page_token.
        self._drive_next_page_token: str | None = None
        # Drive preview is debounced (see _drive_on_highlight) and memoised for
        # the session: highlighting a row costs a metadata round-trip + a file
        # download, so we neither fire one per arrow keypress nor re-fetch a
        # row the cursor has already visited.
        self._drive_preview_timer = None
        self._drive_preview_gen = 0
        self._drive_preview_cache: dict[str, tuple[str, str, bool]] = {}
        # Which source (Google Drive / a saved FTP or SSH host) the Drive tab
        # is currently browsing -- lazily defaults to Google Drive on first
        # access (drive_backend property below), same lazy-init idiom as
        # self.svc, so there's no startup-ordering dependency on when
        # self.svc itself becomes available.
        self._drive_backend: "drive_sources.DriveBackend | None" = None
        # cid -> normalized item dict for the CURRENTLY RENDERED #drive-list
        # rows, rebuilt every _rebuild_drive_table call. Needed because
        # _mk_id's sanitizing (non-alnum/-/_ -> "-") is lossy for FTP/SSH ids
        # (full remote paths, e.g. "/a/b.txt" and "/a-b.txt" collide) -- a
        # plain cid[2:] reversal, safe enough for Google's near-alnum opaque
        # ids, is NOT safe in general. Look files up by cid via this dict
        # instead everywhere a row's real item is needed.
        self._drive_items_by_cid: dict[str, dict] = {}
        # cid -> domain dict for the currently-rendered Tasks / Calendar-events
        # rows, so _reflow_task_rows / _reflow_event_rows can re-render each row
        # in place at the new width on resize (same role _news_by_cid /
        # _contacts_by_cid / _drive_items_by_cid play for their lists).
        self._tasks_by_cid: dict[str, dict] = {}
        self._events_by_cid: dict[str, dict] = {}
        self.settings: Settings = load_settings()
        self._current_label_id = self.settings.default_label_id
        # Full Gmail label list from the last _apply_labels call — backs both
        # the Email pane's folder Select and ThreadModal's "L" label picker.
        self._labels_cache: list[dict] = []
        self._cache: Cache | None = None
        self._online = False
        # Ctrl+R debounce state — see REFRESH_COOLDOWN_SECONDS / action_refresh.
        self._last_manual_refresh: float = 0.0
        # Offline mutation queue (Reply/Reply All/Forward/New-compose send,
        # task-toggle including subtasks, Mark Unread, Trash/Archive/Labels):
        # {uuid: mutation_dict}, persisted
        # under Cache category "pending_mutation" so a queue survives an app
        # restart while still offline. Loaded from cache in _load_from_cache;
        # written/removed by _enqueue_mutation / _replay_pending_mutations_thread.
        self._pending_mutations: dict[str, dict] = {}
        # sub_title's "Connecting…"/"Synced HH:MM"/"Offline (cached HH:MM)"
        # text, WITHOUT the "· N queued" suffix — see _render_sub_title,
        # which combines this with len(self._pending_mutations) and is the
        # only place that should assign self.sub_title directly from here on.
        self._status_base: str = ""
        self._loading_modal: LoadingModal | None = None
        self._news_by_cid: dict[str, dict] = {}
        # Full combined-feed entry list from the last _apply_news_data call —
        # backs the News tab's search filter (Input#news-search) the same
        # way self._threads_cache/self._tasks_cache back Email/Tasks search.
        self._news_entries_cache: list[dict] = []
        # Dashboard NEWS card: cid -> entry lookup (distinct dn- id prefix from
        # the News tab's n- ids, so the same entry can live in both lists
        # without a widget-id collision), plus a rotation offset the on_mount
        # interval advances so the card cycles through more than its 5 visible
        # headlines over time. See _populate_dash_news / _rotate_dash_news.
        self._dash_news_by_cid: dict[str, dict] = {}
        self._dash_news_offset: int = 0
        # Dashboard WORD OF THE DAY / PICTURE OF THE DAY cards (2026-07-19):
        # each card shows one item with an outbound link, so unlike dn-/dm-
        # there's no per-row id->entry map to build -- just the one fetched
        # dict, read back by on_list_view_selected's dw-open/dp-open branches
        # to open the link. See _apply_dashboard_extras_async / fetchers.
        # fetch_word_of_day / fetchers.fetch_wiki_potd.
        self._word_of_day: dict | None = None
        self._wiki_potd: dict | None = None
        # WEATHER card's resolved default location, cached for the app's
        # lifetime once looked up -- see _resolve_weather_location. None
        # until the first refresh needs it (the caller's IP isn't going to
        # move mid-session, so one GeoIP lookup per launch is enough).
        self._geoip_location: str | None = None
        self._browser_history: list[BrowserHistoryEntry] = []
        self._browser_hist_pos: int = -1
        self._browser_tofu: fetchers.GeminiTofuStore | None = None
        # Mirrors whatever key self._cache was last constructed/rekeyed with
        # (None if encrypt-at-rest is off) — kept alongside it so
        # remote_creds.py can reuse the SAME derived key without
        # re-deriving/re-prompting, without remote_creds needing to know
        # anything about Cache/UnlockModal.
        self._encrypt_key: bytes | None = None
        # True once the Browser tab's first-activation-this-session logic
        # (auto-navigate home, or just render the bookmarks list) has run —
        # see on_tabbed_content_tab_activated.
        self._browser_started: bool = False
        # Bookmarks are nested (folders can contain bookmarks or more
        # folders — see Settings.browser_bookmarks). These track which list
        # is currently shown in the "#browser-bookmarks" ListView, mirroring
        # Drive's folder-stack idiom (_drive_folder_stack) but over plain
        # Python lists instead of fetched folder ids, since there's nothing
        # to fetch. Reset to the root list whenever "B" is pressed.
        self._bookmark_current_list: list[dict] = self.settings.browser_bookmarks
        self._bookmark_parent_stack: list[list[dict]] = []
        # Sorted copy of _bookmark_current_list built by _bookmarks_render;
        # "bm-<i>" row keys index into this, not _bookmark_current_list
        # itself (see _bookmarks_render's docstring).
        self._bookmark_sorted_list: list[dict] = self._bookmark_current_list
        # See _ESCAPE_ALT_ARROW_ACTIONS above / on_key below: timestamp of the
        # most recent bare "escape" Key event, used to detect the terminals
        # that encode Alt+Arrow as a double-ESC sequence Textual's parser
        # can't combine into a single "alt+<dir>" event on its own.
        self._pending_escape_time: float | None = None
        # Email pane's Space-to-expand (inline snippet preview, not the full
        # ThreadModal — see AGENTS.md's Email-pane NOTE). Naturally resets on
        # every list repopulate (refresh/label change); no persistence needed.
        self._expanded_thread_ids: set[str] = set()
        self._threads_cache: dict[str, dict] = {}
        # Gmail's pagination cursor for the page AFTER the currently-shown
        # threads, or None if there isn't one — set by _fetch_mail_data /
        # _refresh_email_for_label, read by action_load_more_email and the
        # "Load more" row _apply_email_list_async/_apply_mail_data_async
        # append when it's not None (see gauth.list_threads's page_token).
        self._email_next_page_token: str | None = None
        # Events pane's "Load more" (see action_load_more_events): unlike
        # Gmail's cursor-based pagination, Calendar's events.list is a plain
        # date-range query, so "more" just means a bigger window — bumped by
        # _EVENTS_WINDOW_STEP_DAYS each time, with no natural end (there's
        # always a further-out week to ask for), so this has no next-token
        # equivalent to track — only how wide the window currently is.
        self._events_window_days: int = _EVENTS_WINDOW_STEP_DAYS
        # Full per-message fetch backing the Space-expand preview once a
        # thread has >1 message (see _toggle_thread_expand) — keyed by
        # thread_id, populated lazily on first expand, kept for the rest of
        # the session (naturally reset on app restart, same as the caches
        # above it).
        self._thread_full_cache: dict[str, list[dict]] = {}
        # Email preview pane ("p", action_toggle_preview) -- Outlook-style:
        # hidden by default (session state seeded from the persisted
        # Settings.email_preview_default_visible), live-updates on highlight
        # while visible via the same debounce-timer + generation-counter
        # pattern as Drive's preview column (_drive_preview_timer/_gen).
        # Reuses self._thread_full_cache above for memoization -- no
        # separate cache dict needed, it's already the right shape
        # (gauth.get_thread's per-thread message list).
        self._email_preview_visible = self.settings.email_preview_default_visible
        self._email_preview_timer = None
        self._email_preview_gen = 0
        self._nav_last_result: "fetchers.RouteResult | None" = None
        # Contacts tab (P1 M5) — lazy-fetched: contacts change rarely, so
        # (unlike mail/calendar/drive/news) they're NOT pulled on every
        # startup/Ctrl+R, only once, the first time the Contacts tab is
        # activated (see on_tabbed_content_tab_activated). ComposeModal's
        # To-field autocomplete reads self._contacts_cache directly.
        self._contacts_cache: list[dict] = []
        self._contacts_by_cid: dict[str, dict] = {}
        # Settings-tab list row-key -> value maps (DataTable rows, ROADMAP P3).
        self._feeds_by_cid: dict[str, str] = {}
        self._remote_hosts_by_cid: dict[str, tuple] = {}
        self._contacts_fetch_started = False
        self._contacts_search_timer = None
        self._email_search_timer = None
        self._tasks_search_timer = None
        self._events_search_timer = None
        self._drive_search_timer = None
        self._news_search_timer = None
        # F12 hands the mouse back to the terminal so its native click-drag
        # selection works (see action_toggle_mouse).
        self._mouse_released = False
        # True when the current Google token is missing/invalid (checked via
        # _google_creds_ok()) — gates the Contacts pane between rendering
        # (possibly stale) contacts and a single "not connected" notice
        # pointing at Settings, instead of dumping stale/blank-name rows.
        self._contacts_auth_broken = False
        # Narrow-terminal responsive layout (P2, 2026-07-15) -- see
        # NARROW_WIDTH_THRESHOLD. Kept as an explicit bool (rather than
        # re-deriving it from self.size everywhere) since _apply_narrow_layout
        # and the help-bar wrap helper both need to read it, and self.size
        # isn't meaningful until the first Resize event arrives anyway.
        self._narrow = False
        # Toast de-duplication (see notify()). Maps a normalized message key to
        # the monotonic time it was last shown, so a burst of identical toasts
        # — e.g. one revoked Google token surfacing once per data source on a
        # single refresh — collapses to a single, longer-dwelling toast.
        self._recent_toasts: dict[str, float] = {}

    # How long a toast stays on screen when the caller doesn't specify.
    # Textual's own default is 5s, which is too quick to read a stack of
    # errors before they scroll off — bumped so a burst is actually legible.
    _NOTIFY_DEFAULT_TIMEOUT = 6.0
    _NOTIFY_ERROR_TIMEOUT = 10.0
    # Within this window, a repeat of the same (normalized) message is
    # swallowed instead of stacking another identical toast.
    _NOTIFY_DEDUP_WINDOW = 8.0

    def notify(self, message: str, *, title: str = "", severity: str = "information",
               timeout: float | None = None, markup: bool = True) -> None:
        """Every notify() call in this app — including from ModalScreens,
        which proxy through Widget.notify -> self.app.notify — funnels
        through here, so this is the one place that can catch all of them
        without touching 20+ call sites. Toasts are ephemeral (easy to miss,
        and worse when a bug fires the same one twice); error/warning ones
        also get a durable line in LOG_FILE so a missed toast isn't gone
        for good.

        This funnel also (a) gives toasts a longer default dwell time so a
        burst is readable, and (b) de-duplicates: a revoked/expired Google
        token surfaces once per data source (mail/labels/calendar/drive) on a
        single refresh — six near-identical error toasts flashing by. Those
        all carry the same OAuth signature, so they're rewritten to one clear
        re-auth message and then collapsed to a single toast.
        """
        # Collapse the OAuth-revoked fan-out (Refresh/Labels/Calendar/Drive
        # error: ... invalid_grant ...) into one actionable message so the
        # dedup below merges them instead of showing six variants.
        if severity == "error" and _is_google_auth_error(message):
            message = (
                "Google sign-in expired and couldn't refresh automatically. "
                "Go to Settings (F8) -> General -> 'Re-authorize Google "
                "account' to fix this."
            )

        if severity in ("error", "warning"):
            _logger.log(logging.ERROR if severity == "error" else logging.WARNING, message)

        # De-duplicate identical toasts fired within a short window. The log
        # line above still records every occurrence; only the on-screen toast
        # is suppressed, so nothing is actually lost.
        key = f"{severity}\x00{title}\x00{message}"
        now = time.monotonic()
        last = self._recent_toasts.get(key)
        if last is not None and now - last < self._NOTIFY_DEDUP_WINDOW:
            self._recent_toasts[key] = now
            return
        self._recent_toasts[key] = now
        # Opportunistically drop stale keys so this dict can't grow unbounded
        # over a long-running session.
        if len(self._recent_toasts) > 64:
            cutoff = now - self._NOTIFY_DEDUP_WINDOW
            self._recent_toasts = {
                k: t for k, t in self._recent_toasts.items() if t >= cutoff
            }

        if timeout is None:
            timeout = (self._NOTIFY_ERROR_TIMEOUT if severity in ("error", "warning")
                       else self._NOTIFY_DEFAULT_TIMEOUT)
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)

    def _handle_exception(self, error: Exception) -> None:
        """Every unhandled exception in the app — from a message handler OR
        a worker (run_worker defaults to exit_on_error=True, and most gauth
        calls in this file go through one) — reaches this single method
        before Textual tears down the screen and exits. Previously that
        traceback only ever reached the terminal itself: fine if you're
        watching, gone the moment the pane closed or the session was piped
        through something that swallowed it (confirmed: piping through
        `tee` alone was enough to lose one). Logging it here first means a
        crash always leaves a full traceback in LOG_FILE no matter what was
        capturing the terminal at the time.
        """
        _logger.error("Unhandled exception -- app exiting", exc_info=error)
        super()._handle_exception(error)

    # ---- data layer ----
    @cached_property
    def svc(self):
        return gauth.services()

    @property
    def drive_backend(self) -> "drive_sources.DriveBackend":
        if self._drive_backend is None:
            self._drive_backend = drive_sources.GoogleDriveSource(self.svc)
        return self._drive_backend

    # ---- pane helpers (Mail tab) ----
    def _pane_title_row(self, text: str, *, text_id: str | None = None) -> Horizontal:
        # Used to also render a "pane-title-num" Alt+<digit> badge; removed
        # 2026-07-23 along with the Alt+<digit> pane-jump shortcuts themselves
        # (confirmed unreachable over SSH through some terminals). text_id
        # lets a caller re-target the title Label later (e.g. the Hermes
        # card's title tracks Settings.ai_provider live -- see
        # _update_hermes_labels).
        return Horizontal(Label(text, id=text_id, classes="pane-title-text"), classes="pane-title-row")

    def _main_tabs(self) -> TabbedContent:
        return self.query_one("#main-tabs", TabbedContent)

    def _focus_pane(self, idx: int) -> None:
        """Mail tab now has exactly one pane (Email) -- idx is always 0.
        Kept as a method (not inlined) so call sites (on_mount, the Mail-tab
        activation branch) don't need to change shape."""
        self.active = idx % len(PANE_IDS)
        try:
            self.query_one("#email").add_class("pane-active")
        except Exception:
            pass
        try:
            self.query_one("#email-list").focus()
        except Exception:
            pass
        self._apply_narrow_layout()
        self._update_help_bar()

    def _focus_dash_pane(self, pane_id: str) -> None:
        """Dashboard-tab counterpart to _focus_pane, for its cards (see
        DASH_PANE_IDS and the compose() comment on tab-dashboard). Takes the
        pane's id directly (not an index
        into DASH_PANE_IDS) so callers never need to reason about position
        within the ENABLED subset -- falls back to the first enabled card
        (or "hermes" if literally none are, which _apply_dashboard_panes_
        enabled already prevents) if asked to focus a disabled/unknown one."""
        if pane_id not in self._dash_enabled_ids:
            pane_id = self._dash_enabled_ids[0] if self._dash_enabled_ids else "hermes"
        self._dash_active = pane_id
        for pid in DASH_PANE_IDS:
            try:
                self.query_one(f"#{pid}").remove_class("pane-active")
            except Exception:
                pass
        try:
            self.query_one(f"#{pane_id}").add_class("pane-active")
        except Exception:
            pass
        # No entry for "dash-clock" -- it's a purely decorative display (a
        # Digits widget + a Static of timezone lines), nothing meaningful to
        # focus. A missing key here raises inside the try/except below, so
        # it's silently a no-op (leaves focus wherever it already was).
        targets = {"dash-today": "#event-list", "dash-calendar": "#dash-calendar-grid",
                   "tasks": "#task-list",
                   "dash-mail": "#dash-mail-list", "dash-news": "#dash-news-list",
                   "dash-weather": "#dash-weather-list", "dash-stocks": "#dash-stocks-list",
                   "dash-word": "#dash-word-list", "dash-potd": "#dash-potd-list",
                   "hermes": "#hermes-input"}
        try:
            self.query_one(targets[pane_id]).focus()
        except Exception:
            pass
        self._apply_narrow_layout()
        self._update_help_bar()

    # ---- narrow-terminal responsive layout (P2, 2026-07-15) ----
    # See the NARROW_WIDTH_THRESHOLD / HORIZONTAL_BREAKPOINTS comments above
    # for the overall mechanism. This method handles only the part CSS
    # can't: which single Dashboard-tab pane should be visible depends on
    # runtime state (self._dash_active), not just terminal width. The Mail
    # tab no longer needs anything here -- Email is its only content, and
    # the preview column's visibility is governed purely by the "p" toggle
    # (action_toggle_preview), not narrow state -- same CSS-only stacking
    # Drive's list+preview column already uses when narrow.
    def _apply_narrow_layout(self) -> None:
        """When narrow, show only the active Dashboard pane full width/full
        height; when not narrow, restore the normal always-stacked layout.
        Safe to call any time (pane switch, resize, startup) — a no-op query
        failure (e.g. called before compose() has mounted anything) is
        swallowed the same way _apply_ascii_mode's widget lookups are.
        """
        try:
            self.query_one("#dashboard-body")
        except Exception:
            return
        narrow = self._narrow
        active_pane = self._dash_active if narrow else None
        for pid in DASH_PANE_IDS:
            try:
                self.query_one(f"#{pid}").set_class(narrow and pid != active_pane, "narrow-hidden")
            except Exception:
                pass
        # HERMES lives outside #dash-cards entirely (its own full-width
        # slice), so when it's the active narrow pane every card in
        # #dash-cards is hidden -- hide #dash-cards itself too, or its share
        # of the height would strand HERMES with a blank band above.
        # Otherwise (a card is active, or not narrow), #dash-cards stays
        # visible, and exactly ONE of its two columns does too: the one
        # holding the active pane. The other column's cards are all already
        # narrow-hidden individually (the loop above), but the COLUMN
        # Container itself still claims its non-narrow 65%/35% share unless
        # hidden too -- so the active pane's column gets the CSS override
        # below (`width: 1fr` while narrow) to fill the space the other
        # column just gave up.
        try:
            self.query_one("#dash-cards").set_class(narrow and active_pane == "hermes", "narrow-hidden")
        except Exception:
            pass
        active_col = _DASH_COL_OF.get(active_pane) if active_pane else None
        for col_id in ("dash-col-wide", "dash-col-narrow"):
            try:
                self.query_one(f"#{col_id}").set_class(narrow and col_id != active_col, "narrow-hidden")
            except Exception:
                pass

    def _resolve_dash_cycle_ids(self) -> list[str]:
        """config.toml's pane_order, filtered to real DASH_PANE_IDS entries
        (unknowns dropped) with any DASH_PANE_IDS id missing from it appended
        at the end (so nothing becomes unreachable) -- or DASH_PANE_IDS
        unchanged if pane_order isn't set. See app_config.py / config.toml.
        example. Only ever affects Tab/Shift+Tab cycle order and the Alt-
        digit-jump/"first enabled pane" fallback -- never the fixed visual
        grid position or DASH_ADJACENCY's Alt-arrow spatial navigation.
        """
        order = self.app_config.pane_order
        if not order:
            return list(DASH_PANE_IDS)
        known = set(DASH_PANE_IDS)
        resolved = [pid for pid in order if pid in known]
        dropped = [pid for pid in order if pid not in known]
        if dropped:
            _logger.warning("config.toml: pane_order has unknown id(s) %s -- ignoring them", dropped)
        resolved.extend(pid for pid in DASH_PANE_IDS if pid not in resolved)
        return resolved

    def _apply_dashboard_panes_enabled(self) -> None:
        """Settings -> Dashboard checklist (Settings.dashboard_panes_enabled)
        made real: toggles ``.dash-pane-disabled`` (display: none, same class-
        toggle idiom as narrow-hidden above) on each of the five card
        Containers, and recomputes self._dash_enabled_ids -- the library-
        ordered list every Tab-cycle / Alt-arrow-adjacency / Alt-digit-jump
        / help-scope lookup now walks instead of the fixed DASH_PANE_IDS.
        Filters defensively against DASH_PANE_IDS (a stale settings.json
        naming a since-removed card is just dropped, not an error) and never
        allows an empty result -- an empty Dashboard would leave Tab/Alt-
        arrows with nowhere to land -- falling back to ["hermes"] alone,
        the single broadest-purpose card. Called at startup (on_mount) and
        every time the checklist changes (on_selection_list_selected_changed).

        Also repaints the four external cards from whatever Cache has right
        now: newly enabling e.g. WEATHER had otherwise never been painted at
        all (compose() leaves it an empty ListView, no explanatory text) --
        _live_refresh_thread's own updates only ever touch a card that was
        ALREADY enabled when that refresh ran (see its _DASH_EXTRA_UNCHANGED
        gating). self._cache can be None here (called from on_mount before
        the encryption key -- and so Cache -- exists yet); the repaint is
        just skipped that once, same as _load_from_cache would be.
        """
        enabled = set(self.settings.dashboard_panes_enabled)
        self._dash_enabled_ids = [pid for pid in self._dash_cycle_ids if pid in enabled] or ["hermes"]
        for pid in DASH_PANE_IDS:
            try:
                self.query_one(f"#{pid}").set_class(pid not in self._dash_enabled_ids, "dash-pane-disabled")
            except Exception:
                pass
        if self._dash_active not in self._dash_enabled_ids:
            self._focus_dash_pane(self._dash_enabled_ids[0])
        else:
            self._apply_narrow_layout()
        if self._cache:
            self._apply_dashboard_extras(
                self._cache.get("weather", "current"), self._cache.get("stocks", "current"),
                self._cache.get("word_of_day", "today"), self._cache.get("wiki_potd", "today"))

    def _narrow_wrap(self, text: str) -> str:
        """Word-wrap help-bar text to the current terminal width when
        narrow, so long strings (HELP_GLOBAL_TEXT is 111 chars; the
        tab-settings context string is 132) don't get silently clipped
        mid-word at 80 columns. Static's own `height: auto` (its
        DEFAULT_CSS) already lets #help-bar grow to fit however many
        wrapped lines result — no extra CSS needed here. Left untouched
        above the threshold, where every current help string already fits
        on one line at the sizes this app was tested at before (140x44).
        """
        if not self._narrow or not text:
            return text
        width = max(20, self.size.width - 2)
        return "\n".join(textwrap.wrap(text, width=width))

    def on_resize(self, event: events.Resize) -> None:
        """Public resize hook (see AGENTS.md's note on `_on_xxx` vs `on_xxx`
        dispatch — this is the documented user-overridable one, distinct
        from Textual's own internal `_on_resize`, so there's no MRO
        collision to worry about here). Recomputes narrow-mode state on
        every resize, not just on a narrow/not-narrow transition, since the
        help-bar wrap width in _narrow_wrap depends on the exact width.
        """
        self._narrow = event.size.width < NARROW_WIDTH_THRESHOLD
        self._apply_narrow_layout()
        self._update_help_bar()
        self._update_help_global()
        # Re-flow the width-aware lists so their flexible column fills the new
        # width (and right-pinned fields stay pinned) -- the rows are plain
        # padded/truncated strings, so they don't otherwise re-wrap on resize.
        self._reflow_email_rows()
        self._reflow_news_rows()
        self._reflow_contact_rows()
        self._reflow_drive_rows()
        self._reflow_task_rows()
        self._reflow_event_rows()

    def _reflow_news_rows(self) -> None:
        # DataTable: rebuild through the current filter to re-flow the Title
        # column, rather than editing Labels in place.
        if getattr(self, "_news_entries_cache", None):
            self._refresh_news_list()

    def _reflow_contact_rows(self) -> None:
        # DataTable: re-render at the new width by rebuilding through the live
        # filter (recomputes the flex Email column), rather than editing Labels
        # in place the way the ListView _reflow_list_rows did.
        if getattr(self, "_contacts_cache", None) is not None:
            self._refresh_contacts_list()

    def _reflow_drive_rows(self) -> None:
        # DataTable: rebuild at the new width (recomputes the flex Name column)
        # rather than editing Labels in place. Guarded so a resize before the
        # first Drive load doesn't rebuild an empty listing over nothing.
        if getattr(self, "_drive_files", None) is not None:
            self._rebuild_drive_table()

    def _reflow_task_rows(self) -> None:
        if getattr(self, "_tasks_cache", None) is not None:
            self._refresh_task_list()

    def _reflow_event_rows(self) -> None:
        if getattr(self, "_events_cache", None) is not None:
            self._refresh_event_list()

    def _reflow_email_rows(self) -> None:
        # DataTable: rebuild to re-flow the Subject column at the new width
        # (recomputed by list_tables), rather than editing Labels in place.
        if getattr(self, "_threads_cache", None):
            self._rebuild_email_table()

    def _adjacent(self, direction: str) -> None:
        # Mail tab has nothing to move to now -- Email is its only pane.
        if self._main_tabs().active != "tab-dashboard":
            return
        # Walk DASH_ADJACENCY's FIXED grid-position map (unaffected by which
        # cards are enabled) in `direction` until landing on an enabled card
        # or running out of moves -- so a disabled card in between is
        # transparently skipped rather than a dead keypress. `seen` bounds
        # the walk (DASH_ADJACENCY has no cycles today, but costs nothing to
        # guard against one being introduced later).
        target_id = DASH_ADJACENCY.get(self._dash_active, {}).get(direction)
        seen: set[str] = set()
        while target_id and target_id not in self._dash_enabled_ids and target_id not in seen:
            seen.add(target_id)
            target_id = DASH_ADJACENCY.get(target_id, {}).get(direction)
        if target_id and target_id in self._dash_enabled_ids:
            self._focus_dash_pane(target_id)

    # ---- help bar ----
    def _context_help_scope(self) -> str:
        tab = self._main_tabs().active
        return f"pane:{self._dash_active}" if tab == "tab-dashboard" else f"tab:{tab}"

    def _context_help_text(self) -> str:
        """Plain (non-clickable) context help text. Used both as the basis
        for narrow-mode line wrapping (see _narrow_wrap_help) — wrapping
        must be computed against the VISIBLE width, not one inflated by
        invisible [@click=...] markup tags — and, for the Browser tab, as
        the one place its "S Sort: <mode>" suffix gets appended, since the
        current sort mode is app state, not something bindings.CONTEXT_HELP's
        static dict can hold."""
        scope = self._context_help_scope()
        text = bindings.CONTEXT_HELP.get(scope, "")
        if scope == "tab:tab-browser":
            mode_label = _BOOKMARK_SORT_LABELS.get(self.settings.browser_bookmark_sort, "Name")
            text += f"   S Sort: {mode_label}"
        return bindings.ascii_safe(text) if self.settings.ascii_mode else text

    def _narrow_wrap_help(self) -> str:
        """Like _narrow_wrap, but keeps the context help row's shortcuts
        clickable (bindings.apply_click_actions — the same affordance
        ThreadModal's help bar has) even when narrow. Line breaks are
        computed from the PLAIN text first, then each already-wrapped line
        gets its "Key Label" spans turned into action links — doing it in
        that order means wrap width isn't thrown off by markup tags that
        render as zero width. A span that happens to straddle a wrap
        boundary is just left plain on that occasion (rare, and harmless: it
        already wraps mid-phrase today without clickability).

        The non-narrow fast path applies click actions directly to the
        already-built `plain` text (rather than re-deriving it via
        bindings.help_markup, which re-fetches straight from the static
        CONTEXT_HELP dict) so the Browser tab's dynamic "S Sort: <mode>"
        suffix survives here too, not just in the wrapped case.
        """
        scope = self._context_help_scope()
        plain = self._context_help_text()
        if not plain:
            return ""
        if not self._narrow:
            return bindings.apply_click_actions(plain, scope)
        width = max(20, self.size.width - 2)
        lines = textwrap.wrap(plain, width=width)
        return "\n".join(bindings.apply_click_actions(line, scope) for line in lines)

    def _update_help_bar(self) -> None:
        try:
            self.query_one("#help-context").update(self._narrow_wrap_help())
        except Exception:
            pass

    def _update_help_global(self) -> None:
        try:
            text = bindings.ascii_safe(HELP_GLOBAL) if self.settings.ascii_mode else HELP_GLOBAL
            self.query_one("#help-global", Static).update(self._narrow_wrap(text))
        except Exception:
            pass

    # ---- ASCII-safe mode (Settings -> General, P2 2026-07-15) ----
    # Selectors for every container this app gives a "round" (or, for the
    # two border-bottom-only rules, "solid") box-drawing border — see the
    # CSS block above. Toggling the "ascii-border" class on each live-swaps
    # them to the plain +/-/| "ascii" Textual border style via the paired
    # CSS rules below (".pane.ascii-border" etc.), instead of walking
    # widgets and poking `.styles.border` directly — this way Textual's own
    # cascade/specificity resolves the swap, and it's just as live.
    _ASCII_BORDER_SELECTORS = (
        ".pane", ".pane-active", ".section", "#hermes-log", "#drive-list-col",
        "#drive-preview-col", "#right", "#browser-doc", "#nav-log", "#settings-feed-list",
        "#c-to-suggestions", "#drive-preview-meta", "#email-preview-meta", ".thread-msg-header",
        "#browser-bookmarks", "#settings-remote-hosts-list",
    )

    def _apply_ascii_mode(self) -> None:
        """Apply (or revert) Settings.ascii_mode to every surface it
        touches: tab-number glyphs, box-drawing borders, and the two
        persistent help-bar Statics (per-tab/pane context help is rebuilt
        fresh on every tab/pane switch via _update_help_bar, so it doesn't
        need a separate refresh here). Safe to call at any time — startup
        (to apply whatever was loaded from settings.json) and live from the
        Settings switch.
        """
        ascii_mode = self.settings.ascii_mode
        try:
            main_tabs = self._main_tabs()
            for tab_id, text, num in TAB_LABEL_SPECS:
                try:
                    main_tabs.get_tab(tab_id).label = _tab_label(text, num, ascii_mode)
                except Exception:
                    pass
        except Exception:
            pass
        for selector in self._ASCII_BORDER_SELECTORS:
            for widget in self.query(selector):
                widget.set_class(ascii_mode, "ascii-border")
        self._update_help_global()
        self._update_help_bar()

    # ---- compose ----
    def compose(self) -> ComposeResult:
        yield GtHeader()
        # Falls back to "tab-dashboard" for a stale/unknown saved value (e.g.
        # a since-removed tab id) rather than raising -- TabbedContent's
        # `initial` has no such fallback of its own.
        start_tab = (self.settings.default_start_tab
                     if self.settings.default_start_tab in TAB_ORDER else "tab-dashboard")
        with TabbedContent(id="main-tabs", initial=start_tab):
            # The real Google-native Dashboard (`2026-07-17`, ROADMAP P4),
            # restructured 2026-07-23 into two independent columns (wide left
            # / narrow right -- see _DASH_WIDE_IDS/_DASH_NARROW_IDS): a WIDE
            # column for prose-heavy cards (MAIL unread threads, NEWS
            # headlines, WORD OF THE DAY / PICTURE OF THE DAY), a NARROW
            # column for glanceable ones (CLOCK, CALENDAR, TODAY -- three
            # separate cards, split the same day from one briefly-combined
            # "TIME" card once it turned out cramming a navigable calendar
            # and today's events into one card made moving between dates
            # awkward -- plus TASKS, WEATHER, STOCKS), with ASK HERMES
            # full-width below both. Reuses #event-list/#task-list in place
            # (so their Space-toggle / Enter-detail handlers are unchanged);
            # the #events-search/#tasks-search bars stay in-DOM (hidden) so
            # the "/" filter path (action_focus_search -> _show_pane_search)
            # still works. No per-card digit badge/shortcut -- see
            # _pane_title_row's docstring and bindings.py's GLOBAL_ACTIONS
            # comment for why.
            with TabPane(_tab_label("Dashboard", 1, self.settings.ascii_mode), id="tab-dashboard"):
                # dashboard-body is a Vertical of two parts: the two-column
                # card area (#dash-cards) on top, HERMES full-width below.
                # HERMES used to be an in-grid cell with column-span:2 on a
                # fixed 2x5 grid, which only tiled cleanly when ALL 8
                # non-Hermes cards were enabled -- disable any single one and
                # a whole grid row was left empty (2026-07-22 bug report).
                # Splitting Hermes out entirely (and, since then, splitting
                # the remaining cards into two independent per-column stacks
                # instead of one grid) means each column just uses however
                # many rows its OWN enabled cards need, with Hermes always
                # full width regardless of any of that.
                with Vertical(id="dashboard-body"):
                    with Horizontal(id="dash-cards"):
                        with Vertical(id="dash-col-wide"):
                            with Container(id="dash-mail", classes="pane"):
                                yield self._pane_title_row("MAIL  (unread, enter=open)")
                                yield DataTable(id="dash-mail-list", cursor_type="row", zebra_stripes=True)
                            with Container(id="dash-news", classes="pane"):
                                yield self._pane_title_row("NEWS  (top headlines)")
                                yield DataTable(id="dash-news-list", cursor_type="row", zebra_stripes=True)
                            with Container(id="dash-word", classes="pane"):
                                yield self._pane_title_row("WORD OF THE DAY  (enter=open)")
                                yield ListView(id="dash-word-list")
                            with Container(id="dash-potd", classes="pane"):
                                yield self._pane_title_row("PICTURE OF THE DAY  (enter=open)")
                                yield ListView(id="dash-potd-list")
                        with Vertical(id="dash-col-narrow"):
                            with Container(id="dash-clock", classes="pane"):
                                yield self._pane_title_row("CLOCK")
                                yield Digits(id="dash-clock-digits")
                                yield Static(id="dash-clock-zones")
                            with Container(id="dash-calendar", classes="pane"):
                                # NOT "([/] month, ...)" -- Rich markup parses
                                # a literal "[/]" as an (invalid, unmatched)
                                # closing tag and raises MarkupError; Label
                                # defaults to markup=True and this text has
                                # no actual styling to lose, so just avoid the
                                # bracket-slash-bracket sequence entirely.
                                yield self._pane_title_row(
                                    "CALENDAR  (prev/next month, enter=detail)")
                                yield DataTable(id="dash-calendar-grid", cursor_type="cell",
                                                zebra_stripes=False)
                            with Container(id="dash-today", classes="pane"):
                                yield self._pane_title_row("TODAY  (events, enter=detail)")
                                with Horizontal(id="events-bar", classes="btnrow hidden"):
                                    yield Input(placeholder="Search events (summary/description)… (/)",
                                                id="events-search")
                                yield DataTable(id="event-list", cursor_type="row", zebra_stripes=True)
                            with Container(id="tasks", classes="pane"):
                                yield self._pane_title_row("TASKS  (space=done, enter=detail)")
                                with Horizontal(id="tasks-bar", classes="btnrow hidden"):
                                    yield Input(placeholder="Search tasks (title/notes)… (/)", id="tasks-search")
                                yield DataTable(id="task-list", cursor_type="row", zebra_stripes=True)
                            with Container(id="dash-weather", classes="pane"):
                                yield self._pane_title_row("WEATHER")
                                yield ListView(id="dash-weather-list")
                            with Container(id="dash-stocks", classes="pane"):
                                yield self._pane_title_row("STOCKS")
                                yield ListView(id="dash-stocks-list")
                    with Container(id="hermes", classes="pane"):
                        yield self._pane_title_row(self._hermes_ask_title(), text_id="hermes-pane-title")
                        # Starts hidden; _hermes_submit reveals it on the first
                        # question so an empty response box doesn't take space.
                        yield RichLog(id="hermes-log", markup=False, wrap=True, classes="hidden")
                        yield Input(placeholder=f"Ask {ask.display_name(self.settings.ai_provider)} "
                                                 f"about your Google stuff…", id="hermes-input")
            with TabPane(_tab_label("Mail", 2, self.settings.ascii_mode), id="tab-mail"):
                with Horizontal(id="body"):
                    with Vertical(id="left"):
                        with Container(id="email", classes="pane"):
                            yield self._pane_title_row("EMAIL  (threads)")
                            yield Select(
                                _initial_label_select_options(self.settings.default_label_id),
                                value=self.settings.default_label_id,
                                allow_blank=False, id="email-label-select",
                                classes="hidden",
                            )
                            with Horizontal(id="email-bar", classes="btnrow hidden"):
                                yield Input(placeholder="Search email (subject/from/snippet)… (/)",
                                            id="email-search")
                            yield DataTable(id="email-list", cursor_type="row", zebra_stripes=True)
                    # Preview pane ("p" toggles, action_toggle_preview) -- hidden
                    # by default (Settings.email_preview_default_visible),
                    # live-updates as the highlight moves while visible. See
                    # _email_on_highlight / _apply_email_preview_visibility.
                    with VerticalScroll(id="right"):
                        yield Static(id="email-preview-meta")
                        yield DocumentView(id="email-preview-doc")
            with TabPane(_tab_label("Calendar", 3, self.settings.ascii_mode), id="tab-calendar"):
                with Container(id="calendar-section", classes="section"):
                    yield Label("CALENDAR", classes="pane-title-text")
                    with Horizontal(id="cal-search-bar", classes="btnrow hidden"):
                        yield Input(placeholder="Jump to event (summary/description), Enter for next… (/)",
                                    id="cal-search")
                    with TabbedContent(id="cal-tabs"):
                        with TabPane("Month", id="cal-tab-month"):
                            yield DataTable(id="cal-grid")
                        with TabPane("Week", id="cal-tab-week"):
                            yield DataTable(id="cal-week-grid")
            with TabPane(_tab_label("Drive", 4, self.settings.ascii_mode), id="tab-drive"):
                with Container(id="drive-section", classes="section"):
                    # Seeded with just Google Drive + the add-host sentinel at
                    # compose time -- self._encrypt_key isn't set yet this
                    # early (needed to decrypt any saved hosts), so saved
                    # FTP/SSH entries are filled in by
                    # _refresh_drive_source_select() once unlock completes,
                    # same "compose seeds a placeholder, a later _apply_*
                    # repopulates it" pattern #email-label-select uses.
                    yield Select(_DRIVE_SOURCE_SEED_OPTIONS, value="google",
                                 allow_blank=False, id="drive-source-select")
                    yield Label("/", id="drive-path", classes="muted")
                    with Horizontal(id="drive-search-bar", classes="btnrow hidden"):
                        yield Input(placeholder="Search this folder (name)… (/)", id="drive-search")
                    with Horizontal(id="drive-body"):
                        with Vertical(id="drive-list-col"):
                            yield DataTable(id="drive-list", cursor_type="row", zebra_stripes=True)
                        with VerticalScroll(id="drive-preview-col"):
                            yield Static(id="drive-preview-meta")
                            yield RichLog(id="drive-preview-text", markup=False, wrap=True)
                            yield DocumentView(id="drive-preview-doc", classes="hidden")
            with TabPane(_tab_label("Browser", 5, self.settings.ascii_mode), id="tab-browser"):
                with Container(id="browser-section", classes="section"):
                    with Horizontal(id="browser-bar"):
                        yield Static("WEB", id="browser-mode")
                        yield TabCyclingInput(placeholder="URL, or type to search…", id="browser-url")
                        yield Button("Go", id="browser-go")
                        yield Static("", id="browser-status")
                    yield BookmarksTable(id="browser-bookmarks", cursor_type="row", zebra_stripes=True)
                    yield DocumentView(id="browser-doc")
            with TabPane(_tab_label("News", 6, self.settings.ascii_mode), id="tab-news"):
                with Container(id="news-section", classes="section"):
                    yield Label("NEWS  (all subscribed feeds, newest first)", classes="pane-title-text")
                    with Horizontal(id="news-search-bar", classes="btnrow hidden"):
                        yield Input(placeholder="Search entries (title/summary)… (/)", id="news-search")
                    yield DataTable(id="news-list", cursor_type="row", zebra_stripes=True)
            with TabPane(_tab_label("Navigation", 7, self.settings.ascii_mode), id="tab-navigation"):
                with Container(id="navigation-section", classes="section"):
                    yield Label("NAVIGATION  (origin -> destination, turn-by-turn)", classes="pane-title-text")
                    with Horizontal(id="nav-bar", classes="btnrow"):
                        yield Input(placeholder="Origin address", id="nav-origin")
                        yield Input(placeholder="Destination address", id="nav-destination")
                        yield Button("Go", id="nav-go")
                    yield Static("", id="nav-status", classes="muted")
                    yield Static("", id="nav-summary")
                    yield RichLog(id="nav-log", markup=False, wrap=True)
                    with Horizontal(id="nav-actions", classes="btnrow"):
                        yield Button("Export itinerary to file", id="nav-export")
            with TabPane(_tab_label("Contacts", 8, self.settings.ascii_mode), id="tab-contacts"):
                with Container(id="contacts-section", classes="section"):
                    yield Label("CONTACTS", classes="pane-title-text")
                    with Horizontal(id="contacts-bar", classes="btnrow"):
                        yield Input(placeholder="Search contacts (name or email)…", id="contacts-search")
                        yield Button("Refresh", id="contacts-refresh")
                    yield DataTable(id="contacts-list", cursor_type="row", zebra_stripes=True)
            with TabPane(_tab_label("Settings", 9, self.settings.ascii_mode), id="tab-settings"):
                with Container(id="settings-section", classes="section"):
                    yield Label("SETTINGS", classes="pane-title-text")
                    with TabbedContent(id="settings-tabs"):
                        with TabPane("General", id="settings-tab-general"):
                            with VerticalScroll(id="settings-general-scroll"):
                                yield Label("Startup", classes="pane-title-text")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Start on tab")
                                    yield Select(
                                        _STARTUP_TAB_CHOICES,
                                        value=(self.settings.default_start_tab
                                               if self.settings.default_start_tab in TAB_ORDER
                                               else "tab-dashboard"),
                                        allow_blank=False, id="settings-default-tab",
                                    )
                                yield Label("Google account", classes="pane-title-text")
                                yield Button("Re-authorize Google account", id="settings-reauth-google")
                                yield Static(
                                    "Shows a URL to open in any browser, on any device — no console "
                                    "commands, no browser needed on this machine. Use this if a tab "
                                    "shows an auth error, if you just added a new scope (e.g. Contacts), "
                                    "or proactively before your token expires (Google expires test-user "
                                    "tokens ~weekly — see SETUP.md §4).",
                                    id="settings-reauth-note", classes="muted",
                                )
                                yield Label("Offline queue", classes="pane-title-text")
                                with Horizontal(classes="btnrow"):
                                    yield Button("View queued actions", id="settings-view-queue")
                                yield Static(self._queue_info_text(), id="settings-queue-info", classes="muted")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Show sender address in list")
                                    yield Switch(value=self.settings.show_sender_address,
                                                 id="settings-show-sender-address-switch")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Show preview pane by default (Mail tab, \"p\" to toggle)")
                                    yield Switch(value=self.settings.email_preview_default_visible,
                                                 id="settings-email-preview-default-switch")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Quote original message in replies")
                                    yield Switch(value=self.settings.quote_on_reply,
                                                 id="settings-quote-on-reply-switch")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Check for updates on launch")
                                    yield Switch(value=self.settings.check_for_updates,
                                                 id="settings-update-check-switch")
                                yield Static(
                                    f"Currently running {updater.describe()}. On launch, fast-forwards "
                                    "this checkout to origin and restarts. Skipped automatically if you "
                                    "have uncommitted changes. Also skippable with --no-update.",
                                    id="settings-update-note", classes="muted",
                                )
                                yield Label("Display", classes="pane-title-text")
                                with Horizontal(classes="settings-row"):
                                    yield Label("ASCII-safe mode (for limited terminals)")
                                    yield Switch(value=self.settings.ascii_mode,
                                                 id="settings-ascii-mode-switch")
                                yield Static(
                                    "Swaps round box borders, superscript tab numbers, arrow glyphs, "
                                    "and curly quotes/dashes/bullets for plain-ASCII equivalents — for "
                                    "plain vt100 terminals or older SSH clients that mangle Unicode. "
                                    "Takes effect immediately.",
                                    id="settings-ascii-mode-note", classes="muted",
                                )
                                with Horizontal(classes="settings-row"):
                                    yield Label("Encrypt local cache at rest")
                                    yield Switch(value=self.settings.encrypt_at_rest, id="settings-encrypt-switch")
                                key_method_classes = "" if self.settings.encrypt_at_rest else "hidden"
                                with RadioSet(id="settings-key-method", classes=key_method_classes):
                                    yield RadioButton(
                                        _KEY_METHOD_LABELS["passphrase"],
                                        value=(self.settings.key_method == "passphrase"),
                                        id="rb-passphrase",
                                    )
                                    yield RadioButton(
                                        _KEY_METHOD_LABELS["keyfile"],
                                        value=(self.settings.key_method == "keyfile"),
                                        id="rb-keyfile",
                                    )
                                yield Label("Local cache", classes="pane-title-text")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Keep cached data for")
                                    yield Select(
                                        _RETENTION_CHOICES,
                                        value=_nearest_choice(_RETENTION_CHOICES,
                                                              self.settings.cache_retention_days),
                                        allow_blank=False, id="settings-cache-retention")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Limit cache size to")
                                    yield Select(
                                        _CACHE_SIZE_CHOICES,
                                        value=_nearest_choice(_CACHE_SIZE_CHOICES,
                                                              self.settings.cache_max_mb),
                                        allow_blank=False, id="settings-cache-max")
                                yield Static(
                                    "Limits are applied on launch and whenever you change them. "
                                    "Evicting is safe: everything here is a copy of something "
                                    "Google still has, and anything dropped is re-fetched the "
                                    "next time you open it. Items are aged by when they were "
                                    "last seen, so mail still in your inbox never expires.",
                                    id="settings-cache-note", classes="muted")
                                with Horizontal(classes="btnrow"):
                                    yield Button("Apply limits now", id="settings-prune-cache")
                                    yield Button("Clear local cache now", id="settings-clear-cache")
                                yield Static("", id="settings-cache-info", classes="muted")
                        with TabPane("Browser", id="settings-tab-browser"):
                            with VerticalScroll(id="settings-browser-scroll"):
                                yield Label("Browser", classes="pane-title-text")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Home page (H)")
                                    yield Input(value=self.settings.browser_home_url,
                                                placeholder="https://www.google.com",
                                                id="settings-browser-home-url")
                                    yield Button("Save", id="settings-save-browser-home")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Start page (first Browser-tab visit each session)")
                                    yield Select(
                                        _BROWSER_START_PAGE_CHOICES,
                                        value=self.settings.browser_start_page,
                                        allow_blank=False, id="settings-browser-start-page",
                                    )
                                yield Static(
                                    "Saved remote hosts (FTP/SSH — added from the Drive tab's "
                                    "source picker, \"+ Add remote host…\"):",
                                    classes="muted",
                                )
                                yield DataTable(id="settings-remote-hosts-list",
                                                cursor_type="row", zebra_stripes=True)
                                with Horizontal(classes="btnrow"):
                                    yield Button("Remove selected host", id="settings-remove-remote-host")
                        with TabPane("AI Provider", id="settings-tab-ai"):
                            with VerticalScroll(id="settings-ai-scroll"):
                                yield Label("AI provider (Ask pane)", classes="pane-title-text")
                                with RadioSet(id="settings-ai-provider"):
                                    for label, pid in ask.PROVIDER_CHOICES:
                                        yield RadioButton(
                                            label, value=(self.settings.ai_provider == pid),
                                            id=f"rb-provider-{pid}",
                                        )
                                with Horizontal(classes="settings-row"):
                                    yield Label("Nous API key")
                                    yield Input(
                                        value=self.settings.nous_api_key or "", password=True,
                                        placeholder="only needed for the Hermes provider",
                                        id="settings-nous-key",
                                    )
                                with Horizontal(classes="settings-row"):
                                    yield Label("Hermes gateway URL")
                                    yield Input(
                                        value=self.settings.nous_base_url or "",
                                        placeholder="blank = Nous cloud API; or e.g. "
                                                     "http://127.0.0.1:8645/v1/chat/completions "
                                                     "(hermes proxy start)",
                                        id="settings-nous-base-url",
                                    )
                                yield Button("Save", id="settings-save-nous-key")
                        with TabPane("News Feeds", id="settings-tab-feeds"):
                            with VerticalScroll(id="settings-feeds-scroll"):
                                yield Label("News feeds (RSS/Atom)", classes="pane-title-text")
                                # Populated by _refresh_feed_list (on_mount +
                                # after every subscribe/unsubscribe).
                                yield DataTable(id="settings-feed-list",
                                                cursor_type="row", zebra_stripes=True)
                                with Horizontal(classes="settings-row"):
                                    yield Input(placeholder="https://example.com/feed.xml", id="settings-feed-url")
                                    yield Button("Add feed", id="settings-add-feed")
                                yield Button("Remove selected feed", id="settings-remove-feed")
                                yield Button("Browse popular feeds…", id="settings-browse-feeds")
                        with TabPane("Search", id="settings-tab-search"):
                            with VerticalScroll(id="settings-search-scroll"):
                                yield Label("Web search provider (Browser tab)", classes="pane-title-text")
                                with RadioSet(id="settings-search-provider"):
                                    yield RadioButton(
                                        "Google", value=(self.settings.search_provider == "google"),
                                        id="rb-search-google",
                                    )
                                    yield RadioButton(
                                        "DuckDuckGo", value=(self.settings.search_provider == "duckduckgo"),
                                        id="rb-search-duckduckgo",
                                    )
                                    yield RadioButton(
                                        "SearXNG", value=(self.settings.search_provider == "searxng"),
                                        id="rb-search-searxng",
                                    )
                                google_group_classes = "" if self.settings.search_provider == "google" else "hidden"
                                with Vertical(id="settings-google-group", classes=google_group_classes):
                                    with Horizontal(classes="settings-row"):
                                        yield Label("Google Custom Search API key")
                                        yield Input(
                                            value=self.settings.google_cse_api_key or "", password=True,
                                            id="settings-google-cse-key",
                                        )
                                    with Horizontal(classes="settings-row"):
                                        yield Label("Search Engine ID (cx)")
                                        yield Input(
                                            value=self.settings.google_cse_id or "",
                                            id="settings-google-cse-id",
                                        )
                                searxng_group_classes = "" if self.settings.search_provider == "searxng" else "hidden"
                                with Vertical(id="settings-searxng-group", classes=searxng_group_classes):
                                    with Horizontal(classes="settings-row"):
                                        yield Label("SearXNG instance URL")
                                        yield Input(
                                            value=self.settings.searxng_url or "",
                                            placeholder="https://searx.example.org",
                                            id="settings-searxng-url",
                                        )
                                yield Button("Save search settings", id="settings-save-search")
                        with TabPane("Navigation", id="settings-tab-navigation"):
                            with VerticalScroll(id="settings-navigation-scroll"):
                                yield Label("Routes API (Navigation tab)", classes="pane-title-text")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Routes API key")
                                    yield Input(value=self.settings.routes_api_key or "", password=True,
                                                id="settings-routes-key")
                                yield Button("Save", id="settings-save-routes")
                                yield Static("Requires Cloud Billing linked to your Google Cloud project "
                                              "(SETUP.md §6) -- free up to 10,000 calls/month.",
                                              id="settings-routes-note", classes="muted")
                        with TabPane("Dashboard", id="settings-tab-dashboard"):
                            with VerticalScroll(id="settings-dashboard-scroll"):
                                yield Label("Card configuration", classes="pane-title-text")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Weather location")
                                    yield Input(
                                        value=self.settings.weather_location or "",
                                        placeholder="blank = auto-detect (GeoIP, or Portland, ME)",
                                        id="settings-weather-location",
                                    )
                                with Horizontal(classes="settings-row"):
                                    yield Label("Stock symbols")
                                    yield Input(
                                        value=", ".join(self.settings.stock_symbols),
                                        placeholder="e.g. AAPL, MSFT, GOOG",
                                        id="settings-stock-symbols",
                                    )
                                yield Button("Save card settings", id="settings-save-dashboard-cards")
                                yield Static(
                                    "Weather (Open-Meteo) and stock quotes (Yahoo Finance) need no "
                                    "API key or account. Weather defaults to a location guessed from "
                                    "your IP (or Portland, ME if that fails) until you set one above; "
                                    "stocks default to GOOG, MSFT, AAPL -- edit either field and Save "
                                    "to change them, or clear the symbols field to turn STOCKS off. "
                                    "Word of the day and Wikipedia's picture of the day need no "
                                    "configuration at all. All cards are enabled by default; toggle "
                                    "any off below.",
                                    id="settings-dashboard-config-note", classes="muted")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Clock timezones (below the big local time)")
                                    yield Input(
                                        value=", ".join(self.settings.clock_timezones),
                                        placeholder="e.g. UTC, America/Denver",
                                        id="settings-clock-timezones",
                                    )
                                yield Label("Dashboard cards", classes="pane-title-text")
                                yield Static(
                                    "Choose which cards appear on the Dashboard tab's card grid + "
                                    "Hermes row. At least one card must stay enabled.",
                                    id="settings-dashboard-note", classes="muted")
                                yield SelectionList(
                                    *[(PANE_TITLES[pid], pid, pid in self.settings.dashboard_panes_enabled)
                                      for pid in DASH_PANE_IDS],
                                    id="settings-dashboard-panes")
        with Vertical(id="help-bar"):
            yield Static("", id="help-context")
            yield Static(HELP_GLOBAL, id="help-global")

    # ---- startup: resolve encryption key, then cache-first load + background sync ----
    def on_mount(self) -> None:
        _logger.info("google-tui %s starting", updater.describe())
        # Don't rely on the initial Resize event having already reached
        # on_resize by this point (ordering isn't guaranteed relative to
        # on_mount) — read the size directly so a launch straight into an
        # 80x25 terminal starts in narrow mode instead of only fixing itself
        # on the first actual resize.
        self._narrow = self.size.width < NARROW_WIDTH_THRESHOLD
        # Applies Settings.dashboard_panes_enabled (Settings -> Dashboard) --
        # must run before anything reads self._dash_active/_dash_enabled_ids,
        # since it's what first populates _dash_enabled_ids and can move
        # _dash_active off its __init__ default if that card's disabled.
        # Also must run before _activate_tab below, in case the startup tab
        # IS the Dashboard (_activate_tab's "tab-dashboard" branch reads
        # _dash_enabled_ids via _focus_dash_pane).
        self._apply_dashboard_panes_enabled()
        # TabbedContent.TabActivated never fires for the tab active from
        # mount (see _activate_tab's docstring) -- run its setup explicitly
        # for whatever Settings.default_start_tab resolved to, instead of
        # the old hardcoded "always focus Mail's email-list" call this
        # replaced.
        self._activate_tab(self._main_tabs().active)
        # Dashboard NEWS card rotation — cycles the visible headline window
        # (see _rotate_dash_news, which no-ops while the card is focused or
        # when there aren't enough entries to rotate).
        self.set_interval(self._DASH_NEWS_ROTATE_SECONDS, self._rotate_dash_news)
        # CLOCK card — ticks every second regardless of which pane is
        # focused (unlike the NEWS rotation above, there's no reason to pause
        # it just because the card is being looked at). Call once immediately
        # so it isn't blank for the first second.
        self._update_dash_clock()
        self.set_interval(1.0, self._update_dash_clock)
        # Periodic auto-refresh (config.toml's refresh_interval_minutes, see
        # app_config.py) — off by default; no such loop existed before this.
        if self.app_config.refresh_interval_minutes:
            self.set_interval(self.app_config.refresh_interval_minutes * 60, self._periodic_refresh)
        self._apply_email_preview_visibility()  # applies Settings.email_preview_default_visible
        self._apply_ascii_mode()  # applies whatever Settings.ascii_mode loaded from disk; also updates the help bar
        self._update_hermes_labels()  # applies whatever Settings.ai_provider loaded from disk
        problems = self._diagnose_setup()
        if problems:
            self.push_screen(OnboardingWizardModal(self, problems), self._on_onboarding_result)
        else:
            self._continue_startup()

    def _diagnose_setup(self) -> list[str]:
        problems = []
        try:
            gauth.get_credentials()
        except Exception:
            problems.append("google")
        if not ask.any_provider_reachable(nous_api_key=self.settings.nous_api_key,
                                           nous_base_url=self.settings.nous_base_url):
            problems.append("ai")
        return problems

    def _google_creds_ok(self) -> bool:
        """Cheap, local-first check (same call as _diagnose_setup's "google"
        check) for whether the current token is present/valid — used to gate
        the Contacts pane between showing data and a "not connected" notice."""
        try:
            gauth.get_credentials()
            return True
        except Exception:
            return False

    def _google_auth_broken_detail(self) -> str | None:
        """Distinguishes a dead refresh_token (missing, or revoked/expired by
        Google — both raise RefreshError from Credentials.refresh()) from a
        generic network/API hiccup, so _live_refresh_thread can show ONE
        clear, actionable message instead of the same cryptic RefreshError
        string repeated once per data source — mail/labels/calendar/drive
        each independently touch `self.svc`/gauth.services(). Returns the
        exception detail if this IS an auth problem; None for anything else
        (including no problem at all), which leaves the existing per-section
        try/except in _live_refresh_thread to report whatever it actually
        was, same as before this check existed.
        """
        try:
            gauth.get_credentials()
            return None
        except RefreshError as e:
            return str(e)
        except Exception:
            return None

    def _notify_reauth_needed(self, detail: str) -> None:
        self.notify(
            "Google sign-in expired and couldn't refresh automatically "
            f"({detail}). Go to Settings (F8) -> General -> "
            "'Re-authorize Google account' to fix this.",
            severity="error", timeout=12,
        )

    def _on_onboarding_result(self, _result) -> None:
        self.call_after_refresh(self._continue_startup)

    def _continue_startup(self) -> None:
        if self.settings.encrypt_at_rest and self.settings.key_method == "passphrase":
            self.push_screen(UnlockModal(self.settings, mode="unlock"), self._on_startup_unlock_result)
        else:
            key = read_or_create_keyfile() if self.settings.encrypt_at_rest else None
            self._start_after_unlock(key)

    def _on_startup_unlock_result(self, key: bytes | None) -> None:
        # push_screen's callback fires BEFORE the modal is actually popped off
        # the stack (confirmed in Textual's Screen.dismiss: callback runs,
        # THEN pop_screen()) — so anything touching #email-list etc. here
        # would hit the same "wrong screen" NoMatches gotcha as the
        # LoadingModal dismissal below. call_after_refresh defers past it.
        reset = key is None
        if reset:
            self.settings = Settings()
            save_settings(self.settings)
        self.call_after_refresh(self._start_after_unlock, key, reset)

    def _start_after_unlock(self, key: bytes | None, reset: bool = False) -> None:
        self._encrypt_key = key
        self._cache = Cache(key)
        self._browser_tofu = fetchers.GeminiTofuStore(self._cache)
        if reset:
            self._cache.clear_all()
        # Compose-time couldn't decrypt any saved FTP/SSH hosts (no key yet)
        # -- now that self._encrypt_key is real, repopulate the Drive-tab
        # source picker with whatever's actually saved.
        self._refresh_drive_source_select()
        # Enforce the retention window / size cap once per launch, quietly and
        # off-thread. Deliberately AFTER _load_from_cache paints: pruning is
        # housekeeping, and it should never be the thing standing between you
        # and your inbox appearing on screen.
        self._prune_cache(announce=False)
        had_data = self._load_from_cache()
        if not had_data:
            # true first run — nothing cached yet, nothing to show
            self._loading_modal = LoadingModal()
            self.push_screen(self._loading_modal)
        self._status_base = "Connecting…"
        self._render_sub_title()
        # thread=True: the gauth/googleapiclient calls below are blocking
        # (synchronous httplib2), so fetching on a worker THREAD keeps the
        # asyncio event loop free to actually paint the loading/connecting
        # state instead of freezing the whole app until the fetch completes.
        self.run_worker(self._live_refresh_thread, thread=True, exclusive=True)

    def _load_from_cache(self) -> bool:
        # A prior session may have been closed while still offline with
        # queued mutations — restore them so they're not silently lost, and
        # so they still get replayed once this session reconnects.
        self._pending_mutations = self._cache.get_all("pending_mutation")
        thread_summaries = list(self._cache.get_all(f"thread_summary:{self._current_label_id}").values())
        inbox_summaries = (thread_summaries if self._current_label_id == "INBOX"
                           else list(self._cache.get_all("thread_summary:INBOX").values()))
        events = list(self._cache.get_all("event").values())
        tasks = list(self._cache.get_all("task").values())
        tasklists = list(self._cache.get_all("tasklist").values())
        had_mail = bool(thread_summaries or events or tasks)
        if had_mail:
            self._apply_mail_data(thread_summaries, events, tasks, tasklists, inbox_summaries)

        labels = list(self._cache.get_all("label").values())
        if labels:
            self._apply_labels(labels)

        month_key = f"{self._cal_year:04d}-{self._cal_month:02d}"
        self._apply_cal_month(self._cache.get("cal_month", month_key) or [])
        week_key = self._cal_week_start.isoformat()
        self._apply_cal_week(self._cache.get("cal_week", week_key) or [])

        drive_files = self._cache.get("drive_listing", "root") or []
        self._apply_drive_files(drive_files, "root", "/")

        feed_entries = list(self._cache.get_all("feed_entry").values())
        if feed_entries:
            self._apply_news_data(feed_entries)

        # Contacts: read whatever's cached from a prior session so the tab
        # isn't empty offline, but do NOT set self._contacts_fetch_started —
        # that flag gates the lazy live fetch triggered by first activating
        # the Contacts tab (see on_tabbed_content_tab_activated), which is
        # independent of what cache happened to have on disk.
        contacts = list(self._cache.get_all("contact").values())
        if contacts:
            self._apply_contacts_data(contacts)

        # WEATHER/STOCKS/WORD OF THE DAY/PICTURE OF THE DAY: single-row
        # categories (Cache.get, not get_all -- same "current"/"today" key
        # convention as cal_month/cal_week above), so a None here just means
        # "never successfully fetched", same as the initial in-session state.
        self._apply_dashboard_extras(
            self._cache.get("weather", "current"), self._cache.get("stocks", "current"),
            self._cache.get("word_of_day", "today"), self._cache.get("wiki_potd", "today"))

        return had_mail or bool(drive_files)

    def _cached_thread_summaries(self, label_id) -> dict[str, dict]:
        """{thread_id: cached summary row} for a label — the revalidation input
        to gauth.list_threads (see its `known` arg). Safe to call from a worker
        thread; Cache is lock-guarded."""
        if not self._cache:
            return {}
        try:
            return self._cache.get_all(f"thread_summary:{label_id}")
        except Exception:
            return {}

    def _write_mail_cache(self, label_id, threads, events, tasks, tasklists, inbox_threads=None) -> None:
        if not self._cache:
            return
        self._cache.put_many(f"thread_summary:{label_id}", {t["threadId"]: t for t in threads})
        if inbox_threads is not None and label_id != "INBOX":
            self._cache.put_many("thread_summary:INBOX", {t["threadId"]: t for t in inbox_threads})
        self._cache.put_many("event", {e["id"]: e for e in events})
        self._cache.put_many("task", {f"{t['_list']}-{t['id']}": t for t in tasks})
        self._cache.put_many("tasklist", {tl["id"]: tl for tl in tasklists})

    def _resolve_weather_location(self) -> str:
        """WEATHER card's effective location when Settings.weather_location
        is unset: a one-time GeoIP guess (fetchers.fetch_geoip_location),
        cached in self._geoip_location for the rest of the app's lifetime,
        falling back to _DEFAULT_WEATHER_LOCATION if the lookup fails (no
        network, offline, upstream down, etc). Never raises -- the WEATHER
        card being enabled by default shouldn't cost a startup error toast
        just because GeoIP is unreachable.
        """
        if self._geoip_location is not None:
            return self._geoip_location
        try:
            self._geoip_location = fetchers.fetch_geoip_location()
        except Exception as e:
            _logger.warning("GeoIP location lookup failed, defaulting to %s: %s",
                             _DEFAULT_WEATHER_LOCATION, e)
            self._geoip_location = _DEFAULT_WEATHER_LOCATION
        return self._geoip_location

    def _live_refresh_thread(self) -> None:
        # WEATHER/STOCKS/WORD OF THE DAY/PICTURE OF THE DAY: none of these
        # touch Google at all, so they run before (and regardless of) the
        # auth_broken check below -- no reason a broken Google token should
        # blank cards that have nothing to do with it. Each is separately
        # gated on _dash_enabled_ids so a disabled card doesn't cost a
        # network round trip every refresh; stocks is additionally gated on
        # having symbols to fetch (an empty list would just 400) -- weather
        # always has a location now (Settings.weather_location, or
        # _resolve_weather_location's GeoIP/Portland-ME fallback), so it has
        # no such gate.
        weather = stocks = word_of_day = wiki_potd = _DASH_EXTRA_UNCHANGED
        try:
            if "dash-weather" in self._dash_enabled_ids:
                loc = self.settings.weather_location or self._resolve_weather_location()
                weather = fetchers.fetch_weather(loc)
                self._cache.put("weather", "current", weather)
        except Exception as e:
            self.call_from_thread(self.notify, f"Weather error: {e}", severity="error")
        try:
            if "dash-stocks" in self._dash_enabled_ids and self.settings.stock_symbols:
                stocks = fetchers.fetch_stocks(self.settings.stock_symbols)
                self._cache.put("stocks", "current", stocks)
        except Exception as e:
            self.call_from_thread(self.notify, f"Stock quotes error: {e}", severity="error")
        # Word/Picture of the day are decorative, unconfigured extras that a
        # failed fetch leaves showing their cached content (word_of_day stays
        # _DASH_EXTRA_UNCHANGED, so _apply_dashboard_extras keeps the card).
        # Their upstreams (Merriam-Webster, Wikimedia) intermittently 403/429
        # on rate-limiting/bot-detection — not an app fault and not actionable
        # by the user — so a quiet warning log, NOT a red error toast logged at
        # ERROR every refresh (which was the dominant noise in the app log).
        try:
            if "dash-word" in self._dash_enabled_ids:
                word_of_day = fetchers.fetch_word_of_day()
                self._cache.put("word_of_day", "today", word_of_day)
        except Exception as e:
            _logger.warning("Word of the day fetch failed (keeping cached): %s", e)
        try:
            if "dash-potd" in self._dash_enabled_ids:
                wiki_potd = fetchers.fetch_wiki_potd()
                self._cache.put("wiki_potd", "today", wiki_potd)
        except Exception as e:
            _logger.warning("Picture of the day fetch failed (keeping cached): %s", e)
        self.call_from_thread(self._apply_dashboard_extras, weather, stocks, word_of_day, wiki_potd)

        auth_broken = self._google_auth_broken_detail()
        if auth_broken:
            self.call_from_thread(self._notify_reauth_needed, auth_broken)
            self.call_from_thread(
                self._apply_live_refresh, False, None, None, None, None, None, None)
            return
        # Resurface due snoozes BEFORE the mail fetch below, so the fetched
        # inbox already includes any thread whose remind-at just passed.
        self._resurface_due_snoozes()
        mail = cal_month = cal_week = drive_files = labels = news_entries = None
        ok = True
        try:
            mail = self._fetch_mail_data()
            self._write_mail_cache(*mail)
        except Exception as e:
            ok = False
            self.call_from_thread(self.notify, f"Refresh error: {e}", severity="error")
        try:
            labels = gauth.list_labels(self.svc)
            self._cache.put_many("label", {l["id"]: l for l in labels})
        except Exception as e:
            ok = False
            self.call_from_thread(self.notify, f"Labels error: {e}", severity="error")
        try:
            # Fetched once and shared below -- month + week both querying
            # calendarList().list() separately would be a redundant extra
            # API call per refresh for no benefit (the list rarely changes).
            calendars = gauth.list_calendars(self.svc)
        except Exception as e:
            ok = False
            self.call_from_thread(self.notify, f"Calendars error: {e}", severity="error")
            calendars = [{"id": "primary", "backgroundColor": "#039BE5", "selected": True}]
        try:
            cal_month = self._fetch_cal_month(calendars=calendars)
            self._cache.put("cal_month", f"{self._cal_year:04d}-{self._cal_month:02d}", cal_month)
        except Exception as e:
            ok = False
            self.call_from_thread(self.notify, f"Calendar error: {e}", severity="error")
        try:
            cal_week = self._fetch_cal_week(calendars=calendars)
            self._cache.put("cal_week", self._cal_week_start.isoformat(), cal_week)
        except Exception as e:
            ok = False
            self.call_from_thread(self.notify, f"Calendar error: {e}", severity="error")
        try:
            drive_files = self._fetch_drive_files("root")
            self._cache.put("drive_listing", "root", drive_files)
        except Exception as e:
            ok = False
            self.call_from_thread(self.notify, f"Drive error: {e}", severity="error")
        try:
            # Not folded into the `ok` flag above: `ok` drives the
            # Synced/Offline header, which is specifically about GOOGLE
            # reachability (AGENTS.md §1a) — feed URLs are unrelated
            # third-party sites, and per-feed failures are already reported
            # individually inside _fetch_news_data.
            news_entries = self._fetch_news_data()
            self._write_news_cache(news_entries)
        except Exception as e:
            self.call_from_thread(self.notify, f"News error: {e}", severity="error")
        self.call_from_thread(
            self._apply_live_refresh, ok, mail, cal_month, cal_week, drive_files, labels, news_entries)

    def _apply_live_refresh(self, ok: bool, mail, cal_month, cal_week, drive_files, labels, news_entries=None) -> None:
        # Dismiss the modal FIRST: self.query_one(...) below resolves against
        # the currently active screen, and while LoadingModal is on top of
        # the stack, the base screen's widgets (#email-list etc.) aren't
        # reachable that way (raises NoMatches).
        if self._loading_modal is not None:
            try:
                self._loading_modal.dismiss()
            except Exception:
                pass
            self._loading_modal = None
        if mail is not None:
            _, threads, events, tasks, tasklists, inbox_threads = mail
            self._apply_mail_data(threads, events, tasks, tasklists, inbox_threads)
        if labels is not None:
            self._apply_labels(labels)
        if cal_month is not None:
            self._apply_cal_month(cal_month)
        if cal_week is not None:
            self._apply_cal_week(cal_week)
        if drive_files is not None:
            self._apply_drive_files(drive_files, "root", "/")
        if news_entries is not None:
            self._apply_news_data(news_entries)
        self._online = ok
        now = dt.datetime.now().strftime("%H:%M")
        self._status_base = f"Synced {now}" if ok else f"Offline (cached {now})"
        self._render_sub_title()
        if ok and self._pending_mutations:
            self.run_worker(self._replay_pending_mutations_thread, thread=True,
                            exclusive=True, group="mutation-replay")

    def _render_sub_title(self) -> None:
        n = len(self._pending_mutations)
        suffix = f" · {n} queued" if n else ""
        self.sub_title = self._status_base + suffix
        try:
            self.query_one("#settings-queue-info", Static).update(self._queue_info_text())
        except Exception:
            pass  # Settings tab not mounted yet (e.g. called before compose())

    def _queue_info_text(self) -> str:
        n = len(self._pending_mutations)
        if not n:
            return "No queued offline actions."
        return f"{n} queued offline action{'s' if n != 1 else ''} — will send/apply once reconnected."

    # ---- offline mutation queue (Reply/Reply All/Forward/New-compose send,
    # task-toggle incl. subtasks, Mark Unread, Trash/Archive/Labels) — see
    # self._pending_mutations' __init__ NOTE.
    def _enqueue_mutation(self, mutation: dict) -> str:
        key = str(uuid.uuid4())
        mutation = {**mutation, "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        self._pending_mutations[key] = mutation
        if self._cache:
            self._cache.put("pending_mutation", key, mutation)
        self._render_sub_title()
        return key

    # ---- offline CREATE/DELETE overlay (see _merge_pending_* module docs) ----
    def _reconcile_events(self, events: list[dict]) -> list[dict]:
        return _merge_pending_events(events, self._pending_mutations)

    def _reconcile_tasks(self, tasks: list[dict]) -> list[dict]:
        return _merge_pending_tasks(tasks, self._pending_mutations)

    def _enqueue_event_create(self, title: str, start, end, all_day: bool,
                              description: str = "") -> None:
        """Queue an offline New-Event. `start`/`end` are date/datetime objects
        from CreateEventModal; stored as ISO strings so the mutation is plain-
        JSON (the queue is persisted to the cache as JSON). `description` is
        used by the Email → Event flow (a link + snippet back to the thread)."""
        self._enqueue_mutation({
            "type": "create_event", "temp_id": _new_temp_id(),
            "summary": title, "all_day": all_day, "description": description,
            "start": start.isoformat(), "end": end.isoformat(),
        })
        self.notify("Offline — queued, will create once reconnected.")

    def _enqueue_task_create(self, list_id: str, title: str, parent: str | None,
                             notes: str | None = None) -> str:
        temp_id = _new_temp_id()
        self._enqueue_mutation({
            "type": "create_task", "temp_id": temp_id,
            "list_id": list_id, "title": title, "parent": parent, "notes": notes,
        })
        self.notify("Offline — queued, will add once reconnected.")
        return temp_id

    def _enqueue_task_delete(self, list_id: str, task_id: str) -> None:
        """Queue an offline (sub)task delete. If the target is itself a
        not-yet-synced offline create (temp id), just cancel that create — the
        task never reached Google, so there is nothing to delete server-side
        (ROADMAP: 'a DELETE's target might itself be a queued CREATE'). For a
        real task, also cancel any queued child-creates parented to it: the
        server delete cascades to children, so replaying those creates would
        only 404 or resurrect orphans under a since-deleted parent."""
        if _is_temp_id(task_id):
            if self._cancel_pending_create_task(list_id, task_id):
                return
            # No matching create (e.g. a real id that merely looks temp) —
            # fall through and queue a normal delete rather than swallow it.
        self._cancel_pending_child_creates(list_id, task_id)
        self._enqueue_mutation({"type": "delete_task", "list_id": list_id, "task_id": task_id})

    def _cancel_pending_create_task(self, list_id: str, temp_id: str) -> bool:
        for key, m in list(self._pending_mutations.items()):
            if (m.get("type") == "create_task" and m.get("list_id") == list_id
                    and m.get("temp_id") == temp_id):
                self._cancel_mutation(key)
                # A subtask created then deleted offline may itself have been
                # some other queued create's parent — clean those up too.
                self._cancel_pending_child_creates(list_id, temp_id)
                return True
        return False

    def _cancel_pending_child_creates(self, list_id: str, parent_id: str) -> None:
        for key, m in list(self._pending_mutations.items()):
            if (m.get("type") == "create_task" and m.get("list_id") == list_id
                    and m.get("parent") == parent_id):
                self._cancel_mutation(key)

    def _toggle_pending_task(self, list_id: str, temp_id: str, done: bool) -> bool:
        """Toggle-complete on a task that's still only a queued offline create:
        there is no server task to PATCH, so record the desired completion on
        the create mutation itself. _replay_one_mutation applies it right after
        the insert; _pending_task_creates reflects it in the placeholder now."""
        for key, m in self._pending_mutations.items():
            if (m.get("type") == "create_task" and m.get("list_id") == list_id
                    and m.get("temp_id") == temp_id):
                m["completed"] = done
                if self._cache:
                    self._cache.put("pending_mutation", key, m)
                self._render_sub_title()
                return True
        return False

    def _cancel_mutation(self, key: str) -> None:
        """Drop a queued mutation (PendingMutationsModal's Delete action)
        without ever sending it. Does NOT undo any optimistic local update
        the mutation already applied when it was queued — task-toggle's
        in-place status flip, trash/archive's cache pop, Mark Unread's
        unread-flag flip — since none of those record the pre-mutation state
        needed to revert them. Cancelling only stops the eventual real API
        call; the local view may keep showing the action as "already done"
        until the next full refresh reconciles it against the server."""
        mutation = self._pending_mutations.pop(key, None)
        if mutation is None:
            return
        if self._cache:
            self._cache.delete("pending_mutation", key)
        self._render_sub_title()

    def _replay_one_mutation(self, mutation: dict) -> None:
        t = mutation["type"]
        if t in ("reply", "reply_all"):
            # `key in mutation else None`: a mutation queued before those
            # fields existed lacks the keys entirely, so it falls back to
            # reply_to's header-derived defaults (None). A NEW mutation always
            # carries all four together, so an explicitly-cleared Cc ("") is
            # preserved rather than being coalesced back to the derived value.
            gauth.reply_to(self.svc, mutation["thread_id"], mutation["body"],
                           reply_all=(t == "reply_all"),
                           to=mutation.get("to"),
                           cc=(mutation["cc"] if "cc" in mutation else None),
                           bcc=(mutation["bcc"] if "bcc" in mutation else None),
                           subject=(mutation["subject"] if "subject" in mutation else None),
                           attachments=mutation.get("attachments") or None)
        elif t == "forward":
            gauth.forward(self.svc, mutation["thread_id"], mutation["to"],
                          body_prefix=mutation["body"] + "\n",
                          cc=mutation.get("cc") or None, bcc=mutation.get("bcc") or None,
                          subject=mutation.get("subject") or None,
                          attachments=mutation.get("attachments") or None)
        elif t == "new":
            gauth.send_message(self.svc, to=mutation["to"], subject=mutation["subject"],
                               body=mutation["body"], cc=mutation.get("cc") or None,
                               bcc=mutation.get("bcc") or None,
                               attachments=mutation.get("attachments") or None)
        elif t == "draft":
            gauth.create_draft(self.svc, to=mutation.get("to", ""),
                               subject=mutation.get("subject", ""), body=mutation.get("body", ""),
                               cc=mutation.get("cc") or None, bcc=mutation.get("bcc") or None,
                               thread_id=mutation.get("thread_id"),
                               attachments=mutation.get("attachments") or None)
        elif t == "toggle_task":
            gauth.set_task_status(self.svc, mutation["list_id"], mutation["task_id"], mutation["done"])
        elif t == "mark_unread":
            gauth.mark_unread(self.svc, mutation["thread_id"])
        elif t == "trash":
            gauth.trash_thread(self.svc, mutation["thread_id"])
        elif t == "archive":
            gauth.archive_thread(self.svc, mutation["thread_id"])
        elif t == "modify_labels":
            gauth.modify_labels(self.svc, mutation["thread_id"],
                               add=mutation.get("add"), remove=mutation.get("remove"))
        elif t == "create_event":
            all_day = mutation.get("all_day", False)
            if all_day:
                start: object = dt.date.fromisoformat(mutation["start"])
                end: object = dt.date.fromisoformat(mutation["end"])
            else:
                start = dt.datetime.fromisoformat(mutation["start"])
                end = dt.datetime.fromisoformat(mutation["end"])
            gauth.create_event(self.svc, mutation["summary"], start, end, all_day=all_day,
                               description=mutation.get("description", ""))
        elif t == "create_task":
            created = gauth.create_task(self.svc, mutation["list_id"], mutation["title"],
                                        notes=mutation.get("notes"),
                                        parent=mutation.get("parent"))
            # If the user toggled this not-yet-synced task complete while still
            # offline (see _toggle_pending_task), apply that now that it has a
            # real id — insert() can't set completion status in one call.
            if mutation.get("completed") and created.get("id"):
                gauth.set_task_status(self.svc, mutation["list_id"], created["id"], True)
        elif t == "delete_task":
            gauth.delete_task(self.svc, mutation["list_id"], mutation["task_id"])

    def _replay_pending_mutations_thread(self) -> None:
        """Runs after _apply_live_refresh sees a successful reconnect. Oldest
        first (by created_at, since dict insertion order isn't necessarily
        queue order once items persisted across a restart get reloaded from
        Cache.get_all — see _load_from_cache). A 404 (_is_not_found_error)
        means the target was deleted server-side while offline: drop it,
        nothing to apply. Any other failure leaves it queued for the next
        successful reconnect rather than losing it — but keeps trying the
        REST of the queue rather than giving up on the first failure, since
        one thread/task going missing says nothing about the others.
        """
        items = sorted(self._pending_mutations.items(), key=lambda kv: kv[1].get("created_at", ""))
        sent = dropped = 0
        for key, mutation in items:
            try:
                self._replay_one_mutation(mutation)
            except Exception as e:
                if not _is_not_found_error(e):
                    continue  # keep queued; try again next reconnect
                dropped += 1
                self.call_from_thread(
                    self.notify,
                    f"Skipped a queued {_PENDING_MUTATION_LABELS.get(mutation['type'], mutation['type'])}: "
                    "its target no longer exists.", severity="warning")
            else:
                sent += 1
            self._pending_mutations.pop(key, None)
            if self._cache:
                self._cache.delete("pending_mutation", key)
        if sent or dropped:
            self.call_from_thread(self._render_sub_title)
        if sent:
            parts = [f"sent {sent} queued item{'s' if sent != 1 else ''}"]
            if dropped:
                parts.append(f"skipped {dropped}")
            self.call_from_thread(self.notify, ", ".join(parts).capitalize())
            # Some queued items changed real server state (a sent reply, a
            # completed task) — refresh so the UI shows the real result
            # instead of whatever optimistic/cached state was showing.
            self.call_from_thread(
                lambda: self.run_worker(self._refresh_all_thread, thread=True, exclusive=True))

    # ---- refresh ----
    def _fetch_mail_data(self):
        label_id = self._current_label_id
        label_ids = None if label_id in (None, "ALL") else [label_id]
        # Hand list_threads what we already have. It revalidates each listed
        # thread's historyId against the cached row and only refetches the ones
        # that actually changed — a refresh where nothing moved costs a single
        # API call instead of re-pulling all 80 thread summaries.
        known = self._cached_thread_summaries(label_id)
        threads, next_page_token = gauth.list_threads(self.svc, max_results=80, label_ids=label_ids,
                                                       known=known)
        # The Dashboard MAIL card always means Inbox, regardless of what
        # label the Email tab itself is browsing — fetch that separately
        # when the two diverge (same revalidate-against-cache trick, just
        # keyed on "INBOX" instead of whatever `label_id` is).
        if label_id == "INBOX":
            inbox_threads = threads
        else:
            inbox_known = self._cached_thread_summaries("INBOX")
            inbox_threads, _ = gauth.list_threads(self.svc, max_results=80, label_ids=["INBOX"],
                                                   known=inbox_known)
        # Plain attribute write from a worker thread — safe (CPython attribute
        # assignment is atomic) and consistent with how self._cache/self._online
        # are already touched off-thread elsewhere in this file. Read by
        # action_load_more_email / _apply_email_list_async to know whether a
        # "Load more" row belongs at the bottom of the Email pane.
        self._email_next_page_token = next_page_token
        # self._events_window_days, not a literal 21: once Load More has
        # widened the window this session, an ordinary refresh (Ctrl+R,
        # startup) must keep showing that same wider window, not snap back.
        events = gauth.list_events(self.svc, days=self._events_window_days)
        tasklists = gauth.list_tasklists(self.svc)
        tasks = []
        for tl in tasklists:
            for t in gauth.list_tasks(self.svc, tl["id"], show_completed=True):
                tasks.append({**t, "_list": tl["id"]})
        return label_id, threads, events, tasks, tasklists, inbox_threads

    # ---- labels (folders) ----
    def _apply_labels(self, labels: list[dict]) -> None:
        self._labels_cache = labels
        try:
            select = self.query_one("#email-label-select", Select)
        except Exception:
            return
        options = _label_select_options(labels)
        valid_values = {v for _, v in options}
        value = self._current_label_id if self._current_label_id in valid_values else "ALL"
        select.set_options(options)
        select.value = value
        self._current_label_id = value

    def on_select_changed(self, event: Select.Changed) -> None:
        # Constructing a Select with value=... fires Select.Changed once on
        # mount even though nothing was actually changed by the user
        # (confirmed empirically with a standalone Textual pilot) — the
        # Settings tab is composed at startup with both cache Selects preset
        # to the current setting, so without this guard every launch fired
        # two spurious "No cache limits set" toasts before the user touched
        # anything. Comparing against the already-saved value filters out
        # that mount-time echo while still catching every real user change
        # (which always differs from what's currently saved).
        if event.select.id == "settings-cache-retention":
            if int(event.value) == self.settings.cache_retention_days:
                return
            self.settings.cache_retention_days = int(event.value)
            save_settings(self.settings)
            self._prune_cache()  # apply immediately — a limit you have to
            return               # remember to trigger isn't a limit
        if event.select.id == "settings-cache-max":
            if int(event.value) == self.settings.cache_max_mb:
                return
            self.settings.cache_max_mb = int(event.value)
            save_settings(self.settings)
            self._prune_cache()
            return
        if event.select.id == "settings-browser-start-page":
            if event.value == self.settings.browser_start_page:
                return
            self.settings.browser_start_page = event.value
            save_settings(self.settings)
            return
        if event.select.id == "settings-default-tab":
            if event.value == self.settings.default_start_tab:
                return
            self.settings.default_start_tab = event.value
            save_settings(self.settings)
            return
        if event.select.id == "drive-source-select":
            value = event.value
            if value == self.drive_backend.source_key:
                return  # mount-time echo, or re-selecting the active source
            if value == _DRIVE_ADD_HOST_VALUE:
                self.push_screen(RemoteHostModal(), self._on_remote_host_modal_result)
                return
            self._drive_switch_source(value)
            return
        if event.select.id != "email-label-select":
            return
        new_id = event.value
        if new_id == self._current_label_id:
            return
        self._current_label_id = new_id
        self.settings.default_label_id = new_id
        save_settings(self.settings)
        if self._cache:
            cached = list(self._cache.get_all(f"thread_summary:{new_id}").values())
            self._apply_email_list(cached)
        if self._online:
            self.run_worker(self._refresh_email_for_label, thread=True, exclusive=True, group="mail-apply")

    def _refresh_email_for_label(self) -> None:
        label_id = self._current_label_id
        label_ids = None if label_id in (None, "ALL") else [label_id]
        known = self._cached_thread_summaries(label_id)
        # One retry before giving up: the errors seen in practice here
        # (IncompleteRead, SSL record-layer failures, read timeouts) are
        # transient network blips, not real failures — worth one quiet
        # retry before bothering the user with an error.
        for attempt in range(2):
            try:
                threads, next_page_token = gauth.list_threads(
                    self.svc, max_results=80, label_ids=label_ids, known=known)
                break
            except Exception:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                self.call_from_thread(
                    self.notify,
                    "Couldn't refresh mail — still showing the cached list.",
                    severity="warning")
                return
        # Switching labels means "Load more" (if any) now applies to
        # THIS label's next page, not whatever label was active before.
        self._email_next_page_token = next_page_token
        if self._cache:
            self._cache.put_many(f"thread_summary:{label_id}", {t["threadId"]: t for t in threads})
        self.call_from_thread(self._apply_email_list, threads)

    def action_load_more_email(self) -> None:
        if not self._email_next_page_token:
            self.notify("No more messages to load.", severity="warning")
            return
        if not self._online:
            self.notify("Can't load more messages while offline.", severity="warning")
            return
        self.run_worker(self._load_more_email_thread, thread=True, exclusive=True, group="mail-loadmore")

    def _load_more_email_thread(self) -> None:
        label_id = self._current_label_id
        label_ids = None if label_id in (None, "ALL") else [label_id]
        token = self._email_next_page_token
        try:
            new_threads, next_page_token = gauth.list_threads(
                self.svc, max_results=80, label_ids=label_ids,
                known=self._cached_thread_summaries(label_id), page_token=token)
        except Exception as e:
            self.call_from_thread(self.notify, f"Load more error: {e}", severity="error")
            return
        self._email_next_page_token = next_page_token
        if self._cache:
            self._cache.put_many(f"thread_summary:{label_id}", {t["threadId"]: t for t in new_threads})
        if not new_threads:
            self.call_from_thread(self.notify, "No more messages.", severity="warning")
            # Still re-apply: next_page_token may have gone None, which drops
            # the "Load more" row even though no NEW threads came back.
        # Dict, not list concat: a thread landing on both pages (should be
        # rare, but not impossible if it was already cached under a
        # different label) must not become two ListItems with the same
        # _mk_id — that's a DuplicateIds crash, not just a visual dupe.
        # Dict preserves each key's ORIGINAL position even when overwritten,
        # so this can't silently reorder the list either.
        merged = {t["threadId"]: t for t in self._threads_cache.values()}
        for t in new_threads:
            merged[t["threadId"]] = t
        self.call_from_thread(self._apply_email_list, list(merged.values()))

    def _labels_by_id(self) -> dict:
        return {l["id"]: l for l in self._labels_cache}

    def _apply_email_list(self, threads) -> None:
        """Set the thread cache from `threads` and repaint #email-list. Now a
        synchronous DataTable rebuild (clear is immediate) — the old awaited
        worker + generation counter existed only for ListView.clear()'s
        AwaitRemove and DuplicateIds-on-reinsert; DataTable row keys can't
        collide the same way. Every caller runs on the main thread."""
        self._threads_cache = {t["threadId"]: t for t in threads}
        # Drop any selection ids no longer among the loaded threads so a bulk
        # action can't target a thread that's since left the list.
        self._email_selected &= set(self._threads_cache)
        self._rebuild_email_table()

    def _apply_mail_data(self, threads, events, tasks, tasklists, inbox_threads=None) -> None:
        """Full mail-data apply (email + TODAY + TASKS + Dashboard MAIL) — all
        four are DataTables now, so this is fully synchronous (the last await
        that kept it a worker was #email-list.clear()). Runs on the main
        thread via call_from_thread from the fetch workers."""
        self._tasklists = tasklists
        self._threads_cache = {t["threadId"]: t for t in threads}
        self._email_selected &= set(self._threads_cache)
        self._rebuild_email_table()

        # Caches stay RAW (server/cache data only); the offline queue is
        # overlaid at render time so a not-yet-synced create/delete shows
        # immediately and disappears the instant it replays — see the
        # _merge_pending_* module docs.
        self._events_cache = events
        self._fill_today_events(events)

        self._tasks_cache = tasks
        disp_tasks = self._reconcile_tasks(tasks)
        try:
            tasks_query = self.query_one("#tasks-search", Input).value
        except Exception:
            tasks_query = ""
        visible_tasks = _fuzzy_filter_tasks(disp_tasks, tasks_query) if tasks_query.strip() else disp_tasks
        self._tasks_by_cid = {}
        _append_task_items(self.query_one("#task-list", DataTable), visible_tasks,
                           by_cid=self._tasks_by_cid)

        self._populate_dash_mail(inbox_threads if inbox_threads is not None else threads)

    # ---- email list as a DataTable (ROADMAP P3) ----
    def _email_sel_style(self) -> str:
        """Rich style string for a multi-select-checked row's cells. Derived
        from the active theme (accent background, background-colored text) so
        it adapts to light/dark; falls back to reverse video if the theme API
        isn't available."""
        try:
            t = self.current_theme
            return f"{t.background} on {t.accent} bold"
        except Exception:
            return "reverse bold"

    def _email_summary_cells(self, th: dict, selected: bool, show_addr: bool,
                             labels: dict, sel_style: str) -> list:
        style = sel_style if selected else ""
        mark = _email_marks(th)
        frm = _format_sender(th["from"], show_addr)
        count = f"  ({th['count']})" if th["count"] > 1 else ""
        subj = (th["subject"] or "(no subject)") + count
        chips = _thread_label_chips(th, labels)
        date = _fmt_email_date(th.get("date", ""))
        return [Text(mark, style=style), Text(frm, style=style), Text(subj, style=style),
                Text(chips, style=style), Text(date, style=style)]

    def _email_message_cells(self, m: dict, show_addr: bool) -> list:
        frm = _format_sender((m.get("from") or "").strip(), show_addr)
        snippet = (m.get("body") or "").strip().replace("\n", " ")
        dim = "dim italic"
        return [Text("", style=dim), Text("↳ " + frm, style=dim),
                Text(snippet, style=dim), Text("", style=dim), Text("", style=dim)]

    def _expanded_message_rows(self, th: dict, show_addr: bool) -> list:
        """Rows revealed under an expanded thread: one per message. A single-
        message thread synthesizes one row from its snippet (no fetch); a
        multi-message thread uses the fetched full bodies once cached, else a
        transient 'Loading…' row (see _toggle_thread_expand / _fetch_thread_preview)."""
        tid = th["threadId"]
        if th.get("count", 1) <= 1:
            msgs = [{"from": th.get("from", ""), "body": (th.get("snippet") or "").strip()}]
        else:
            cached = self._thread_full_cache.get(tid)
            if cached is None:
                return [(f"t-{tid}::loading",
                         [Text(""), Text(""), Text("Loading messages…", style="dim italic"),
                          Text(""), Text("")])]
            msgs = cached
        return [(f"t-{tid}::m{i}", self._email_message_cells(m, show_addr))
                for i, m in enumerate(msgs)]

    def _email_visible_threads(self) -> list[dict]:
        threads = list(self._threads_cache.values())
        try:
            query = self.query_one("#email-search", Input).value
        except Exception:
            query = ""
        return _fuzzy_filter_threads(threads, query) if query.strip() else threads

    def _rebuild_email_table(self) -> None:
        """Repaint #email-list from _threads_cache through the current search
        filter, honoring _expanded_thread_ids (reveal message rows) and
        _email_selected (tint), plus the 'Load more' sentinel. Preserves the
        row cursor across the rebuild where the same key still exists."""
        try:
            table = self.query_one("#email-list", DataTable)
        except Exception:
            return
        prev = list_tables.current_row_key(table)
        show_addr = self.settings.show_sender_address
        labels = self._labels_by_id()
        sel_style = self._email_sel_style()
        try:
            query = self.query_one("#email-search", Input).value
        except Exception:
            query = ""
        rows: list[tuple[str, list]] = []
        for th in self._email_visible_threads():
            tid = th["threadId"]
            rows.append((f"t-{tid}",
                         self._email_summary_cells(th, tid in self._email_selected,
                                                   show_addr, labels, sel_style)))
            if tid in self._expanded_thread_ids:
                rows.extend(self._expanded_message_rows(th, show_addr))
        if self._email_next_page_token and not query.strip():
            rows.append((LOAD_MORE_EMAIL_ID,
                         [Text(""), Text(""), Text("↓ Load more messages…", style="dim"),
                          Text(""), Text("")]))
        list_tables.rebuild_flat_table(table, _EMAIL_TABLE_COLS, rows,
                                       flex_min=_EMAIL_SUBJ_MIN_W, fallback_width=_EMAIL_ROW_DEFAULT_W)
        if prev:
            try:
                table.move_cursor(row=table.get_row_index(prev))
            except Exception:
                pass

    def _thread_id_from_key(self, key: str | None) -> str | None:
        """Thread id behind an #email-list row key — a summary row `t-<tid>` or
        a revealed message/loading row `t-<tid>::…`. None for the load-more
        sentinel or an empty table."""
        if not key or not key.startswith("t-"):
            return None
        tid = key[2:].split("::", 1)[0]
        return tid or None

    def _restyle_email_row(self, tid: str) -> None:
        """Re-render just one thread's summary row cells to reflect its current
        multi-select state — cheaper than a full rebuild, and keeps the cursor
        put (DataTable has no per-row CSS class, so this is how the tint moves)."""
        try:
            table = self.query_one("#email-list", DataTable)
            row = table.get_row_index(f"t-{tid}")
        except Exception:
            return
        th = self._threads_cache.get(tid)
        if not th:
            return
        cells = self._email_summary_cells(th, tid in self._email_selected,
                                          self.settings.show_sender_address,
                                          self._labels_by_id(), self._email_sel_style())
        for col, val in enumerate(cells):
            try:
                table.update_cell_at(Coordinate(row, col), val)
            except Exception:
                pass

    def _refresh_all_thread(self) -> None:
        """Post-write refresh (task toggled, mail sent) — MUST run with
        thread=True.

        This was previously an `async def` worker, which meant every blocking
        `gauth.*` call below ran ON the event loop: `_fetch_mail_data()` alone
        is a Gmail thread list + a Calendar list + a Tasks list per tasklist,
        and the whole UI (keystrokes, repaints, the spinner) was frozen solid
        for the duration. Same fetch-off-thread / apply-on-main split as
        `_live_refresh_thread` — see AGENTS.md's fetch/apply-split NOTE.
        """
        try:
            mail = self._fetch_mail_data()
        except Exception as e:
            self.call_from_thread(self.notify, f"Refresh error: {e}", severity="error")
            return
        _, threads, events, tasks, tasklists, inbox_threads = mail
        self._write_mail_cache(*mail)
        self.call_from_thread(self._apply_mail_data, threads, events, tasks, tasklists, inbox_threads)
        try:
            labels = gauth.list_labels(self.svc)
            if self._cache:
                self._cache.put_many("label", {l["id"]: l for l in labels})
            self.call_from_thread(self._apply_labels, labels)
        except Exception:
            pass
        self.call_from_thread(
            self.notify,
            f"Refreshed: {len(threads)} threads, {len(events)} events, {len(tasks)} tasks")

    # ---- tab switching ----
    def _goto_tab(self, tab_id: str) -> None:
        if self._main_tabs().active != tab_id:
            self._main_tabs().active = tab_id

    def action_goto_tab_dashboard(self): self._goto_tab("tab-dashboard")
    def action_goto_tab_mail(self):     self._goto_tab("tab-mail")
    def action_goto_tab_calendar(self): self._goto_tab("tab-calendar")
    def action_goto_tab_drive(self):    self._goto_tab("tab-drive")
    def action_goto_tab_browser(self):  self._goto_tab("tab-browser")
    def action_goto_tab_news(self):     self._goto_tab("tab-news")
    def action_goto_tab_navigation(self): self._goto_tab("tab-navigation")
    def action_goto_tab_settings(self): self._goto_tab("tab-settings")
    def action_goto_tab_contacts(self): self._goto_tab("tab-contacts")

    def _cycle_tab(self, step: int) -> None:
        current = self._main_tabs().active
        idx = TAB_ORDER.index(current) if current in TAB_ORDER else 0
        self._goto_tab(TAB_ORDER[(idx + step) % len(TAB_ORDER)])

    def action_cycle_tab(self):      self._cycle_tab(1)
    def action_cycle_tab_back(self): self._cycle_tab(-1)

    def _cycle_settings_tab(self, step: int) -> None:
        tabs = self.query_one("#settings-tabs", TabbedContent)
        current = tabs.active
        idx = SETTINGS_TAB_ORDER.index(current) if current in SETTINGS_TAB_ORDER else 0
        tabs.active = SETTINGS_TAB_ORDER[(idx + step) % len(SETTINGS_TAB_ORDER)]

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id != "main-tabs":
            return
        self._activate_tab(event.tabbed_content.active)

    def _activate_tab(self, tab_id: str) -> None:
        """Per-tab "just became visible" setup (focus the tab's natural
        input, kick a first-visit fetch, etc). Textual only fires
        TabbedContent.TabActivated on an actual switch — never for whichever
        tab is active from the start (ContentSwitcher's `_on_mount` sets its
        initial child's `display` directly, bypassing the reactive watcher
        that normally fires this) — so on_mount calls this explicitly too,
        for whatever Settings.default_start_tab resolved to. Confirmed
        2026-07-22 the hard way: on_mount used to unconditionally focus
        #email-list (assuming Mail was always the startup tab, back when it
        was hardcoded), which — now that the startup tab is configurable —
        actively fought a *different* tab's initial activation: focusing a
        widget inside a TabPane makes TabbedContent auto-switch to that pane
        (TabbedContent._on_tab_pane_focused), so starting on Dashboard while
        still unconditionally focusing Mail's email-list silently flipped
        the active tab back to Mail right after mount, before Dashboard's
        grid ever got a real (non-initial) layout pass — the actual cause of
        a "Dashboard cards not filling the screen" report that turned out to
        have nothing to do with CSS or terminal resize handling at all.
        """
        if tab_id == "tab-dashboard":
            self._focus_dash_pane(self._dash_active)
        elif tab_id == "tab-mail":
            self._focus_pane(self.active)
            self._apply_email_preview_visibility()
        elif tab_id == "tab-calendar":
            self.query_one("#cal-grid").focus()
        elif tab_id == "tab-drive":
            self.query_one("#drive-list").focus()
            self._apply_drive_preview_visibility()
            # Preview the already-highlighted row on entry. The old ListView
            # fired this at startup (no tab guard); on_data_table_row_highlighted
            # now suppresses off-tab highlights (resize rebuilds re-emit them),
            # so kick the preview here instead when the tab actually opens.
            cid = list_tables.current_row_key(self.query_one("#drive-list", DataTable))
            if cid:
                self._drive_on_highlight(cid)
        elif tab_id == "tab-browser":
            if not self._browser_started:
                self._browser_started = True
                if self.settings.browser_start_page == "home":
                    url = self.settings.browser_home_url or "https://www.google.com"
                    try:
                        self.query_one("#browser-url", Input).value = url
                    except Exception:
                        pass
                    self._browser_navigate(url, push_history=True)
                else:
                    self._bookmarks_render()
            # Focus the bookmarks table (not the URL bar) whenever it's the
            # visible view -- otherwise arrow/Home/End/PageUp/Down never
            # reach DataTable's own cursor bindings, since Input consumes
            # them as caret movement first. Same "hidden" check
            # action_browser_show_bookmarks already uses. Checked AFTER the
            # first-visit block above, since that's what decides whether the
            # bookmarks table or the page view ends up visible.
            try:
                bookmarks_visible = "hidden" not in self.query_one("#browser-bookmarks").classes
            except Exception:
                bookmarks_visible = False
            if bookmarks_visible:
                self.query_one("#browser-bookmarks").focus()
            else:
                self.query_one("#browser-url").focus()
        elif tab_id == "tab-news":
            self.query_one("#news-list").focus()
        elif tab_id == "tab-navigation":
            self.query_one("#nav-origin").focus()
        elif tab_id == "tab-settings":
            self._update_settings_cache_info()
            self._refresh_remote_hosts_list()
            self._refresh_feed_list()
            self.query_one("#settings-encrypt-switch").focus()
        elif tab_id == "tab-contacts":
            self.query_one("#contacts-search").focus()
            if not self._contacts_fetch_started:
                if self._google_creds_ok():
                    self._contacts_fetch_started = True
                    self.run_worker(self._contacts_fetch_thread, thread=True, exclusive=True, group="contacts-fetch")
                else:
                    self._contacts_auth_broken = True
                    self._refresh_contacts_list()
        self._update_help_bar()

    def action_switch_left(self):
        active = self._main_tabs().active
        if active == "tab-browser":
            self._browser_back()
        elif active == "tab-settings":
            self._cycle_settings_tab(-1)
        elif active in ("tab-mail", "tab-drive"):
            self._focus_list_column(active)
        else:
            self._adjacent("left")

    def action_switch_right(self):
        active = self._main_tabs().active
        if active == "tab-browser":
            self._browser_forward()
        elif active == "tab-settings":
            self._cycle_settings_tab(1)
        elif active in ("tab-mail", "tab-drive"):
            self._focus_preview_column(active)
        else:
            self._adjacent("right")

    # Mail/Drive have no adjacent-pane grid (that's Dashboard's DASH_
    # ADJACENCY concept) but each still has a two-column list/preview
    # layout -- Alt+Left/Right moves focus between them, same idiom as
    # Dashboard's pane-to-pane movement, gated on the preview actually
    # being visible (the "p" toggle) so there's nothing to focus into
    # when it's hidden.
    def _focus_preview_column(self, active_tab: str) -> None:
        if active_tab == "tab-mail":
            if not self._email_preview_visible:
                self.notify("Preview pane is hidden — press \"p\" to show it.", severity="warning")
                return
            self.query_one("#email-preview-doc").focus()
        else:
            if not self._drive_preview_visible:
                self.notify("Preview pane is hidden — press \"p\" to show it.", severity="warning")
                return
            doc_widget = self.query_one("#drive-preview-doc")
            if not doc_widget.has_class("hidden"):
                doc_widget.focus()
            else:
                self.query_one("#drive-preview-text").focus()

    def _focus_list_column(self, active_tab: str) -> None:
        if active_tab == "tab-mail":
            self.query_one("#email-list").focus()
        else:
            self.query_one("#drive-list").focus()

    def action_switch_up(self):    self._adjacent("up")
    def action_switch_down(self):  self._adjacent("down")

    def action_browser_home(self) -> None:
        """H: jump the Browser tab to the configured home URL.

        Browser-tab-only, like ``[``/``]`` on the Calendar tab — a no-op
        everywhere else.
        """
        if self._main_tabs().active != "tab-browser":
            return
        url = self.settings.browser_home_url or "https://www.google.com"
        try:
            self.query_one("#browser-url", Input).value = url
        except Exception:
            pass
        self._browser_navigate(url, push_history=True)

    def action_browser_show_bookmarks(self) -> None:
        """B: (re)show the Browser tab's bookmarks list at the root folder,
        at any point in the session — not just before the first navigation.
        Browser-tab-only, same guard as action_browser_home.
        """
        if self._main_tabs().active != "tab-browser":
            return
        self._bookmark_parent_stack = []
        self._bookmark_current_list = self.settings.browser_bookmarks
        self._bookmarks_render()
        try:
            lv = self.query_one("#browser-bookmarks", DataTable)
            lv.remove_class("hidden")
            lv.focus()
        except Exception:
            pass

    def action_browser_bookmark_page(self) -> None:
        """Ctrl+B: save the Browser tab's currently-loaded URL as a new
        top-level bookmark (prompting for a label). Browser-tab-only.
        """
        if self._main_tabs().active != "tab-browser":
            return
        try:
            url = self.query_one("#browser-url", Input).value.strip()
        except Exception:
            url = ""
        if not url:
            self.notify("No page loaded to bookmark", severity="warning")
            return
        default_label = urllib.parse.urlparse(url).netloc or url
        self.push_screen(BookmarkLabelModal(default_label),
                          lambda label: self._browser_bookmark_save(url, label))

    def _browser_bookmark_save(self, url: str, label: str | None) -> None:
        if not label:
            return
        added_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self.settings.browser_bookmarks.append(
            {"type": "bookmark", "label": label, "url": url, "added_at": added_at})
        save_settings(self.settings)
        self.notify(f"Bookmarked: {label}")

    def on_key(self, event: events.Key) -> None:
        """Compensate for a terminal-encoding gap that swallows Alt+Arrow.

        See ``_ESCAPE_ALT_ARROW_ACTIONS``'s comment above for the confirmed
        root cause. ``GoogleTUI`` is earlier in the MRO than ``App``, so this
        runs before ``App._on_key`` (its ``_check_bindings`` walk is what
        would otherwise run e.g. the address bar's own bare-"left" cursor
        move) — ``event.prevent_default()`` stops that base handler from
        running at all, the same pattern as ``GtHeader._on_click`` (AGENTS.md
        §2's MRO-dispatch NOTE).
        """
        if event.key == "escape":
            focused = self.focused
            if isinstance(focused, Input) and focused.id in _PANE_SEARCH_BARS:
                self._hide_pane_search(focused.id)
                event.stop()
                event.prevent_default()
                return
            if isinstance(focused, Select) and focused.id == "email-label-select":
                self._hide_label_select()
                event.stop()
                event.prevent_default()
                return
            self._pending_escape_time = event.time
            return
        pending = self._pending_escape_time
        self._pending_escape_time = None
        action_name = _ESCAPE_ALT_ARROW_ACTIONS.get(event.key)
        if (action_name is not None and pending is not None
                and (event.time - pending) <= _ESCAPE_ALT_ARROW_WINDOW):
            event.stop()
            event.prevent_default()
            getattr(self, action_name)()

    def _cycle_dash_pane(self, step: int) -> None:
        """Tab/Shift+Tab (step +-1) over ENABLED cards only -- walks
        self._dash_enabled_ids, not the fixed DASH_PANE_IDS, so a disabled
        card is simply never a stop on the cycle."""
        ids = self._dash_enabled_ids
        if not ids:
            return
        i = ids.index(self._dash_active) if self._dash_active in ids else 0
        self._focus_dash_pane(ids[(i + step) % len(ids)])

    def action_cycle(self):
        tab = self._main_tabs().active
        if tab == "tab-dashboard":
            self._cycle_dash_pane(1)
        elif tab == "tab-browser":
            self._browser_toggle_focus()
        else:
            # Not our key to claim here -- let it fall through to Screen's
            # default tab -> app.focus_next binding (non-priority pass).
            raise SkipAction()

    def action_cycle_back(self):
        tab = self._main_tabs().active
        if tab == "tab-dashboard":
            self._cycle_dash_pane(-1)
        elif tab == "tab-browser":
            self._browser_toggle_focus()
        else:
            raise SkipAction()

    def action_refresh(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_manual_refresh
        if elapsed < REFRESH_COOLDOWN_SECONDS:
            wait = REFRESH_COOLDOWN_SECONDS - elapsed
            self.notify(f"Refreshed recently — wait {wait:.0f}s", severity="warning")
            return
        self._last_manual_refresh = now
        self._status_base = "Connecting…"
        self._render_sub_title()
        self.run_worker(self._live_refresh_thread, thread=True, exclusive=True)

    def _periodic_refresh(self) -> None:
        """Timer-driven counterpart to action_refresh (config.toml's
        refresh_interval_minutes, see app_config.py / on_mount's
        set_interval call). Skips silently while offline -- unlike a manual
        Ctrl+R, the user didn't just ask for this, so it shouldn't spam
        per-section "X error" toasts every interval when there's nothing to
        refresh anyway; the next successful refresh (manual or reconnect)
        picks the timer back up regardless."""
        if not self._online:
            return
        self._last_manual_refresh = time.monotonic()
        self.run_worker(self._live_refresh_thread, thread=True, exclusive=True)

    def action_help(self): self.push_screen(HelpModal())

    def action_hermes_popup(self) -> None:
        """Ctrl+K: pop up a quick-ask modal for the configured AI provider
        from ANY tab, without navigating to the Dashboard tab first. Dead
        (never dispatched) while another ModalScreen is already
        on top -- Textual truncates the binding-chain walk at a modal
        boundary (AGENTS.md §2), which is the right default here too: we
        don't want this stacking on top of e.g. ComposeModal or ThreadModal."""
        self.push_screen(HermesAskModal())

    def action_toggle_mouse(self) -> None:
        """Release/recapture the mouse (F12).

        While a TUI has mouse reporting enabled the terminal hands drag events
        to the app instead of drawing its own selection, which is why you can't
        just swipe over a URL and copy it the way you would in any other
        program. Turning reporting off hands the mouse back to the terminal:
        native click-drag selection and the terminal's own copy work exactly as
        they normally do, anywhere in the app. Clicking widgets stops working
        until you press F12 again — keyboard navigation is unaffected either way.

        (Textual's own Ctrl+C-on-a-selection copies via OSC 52, which is the
        nicer path when it works, but plenty of setups — macOS Terminal, tmux
        without `set-clipboard on`, locked-down SSH clients — silently drop it.
        This toggle needs no terminal cooperation at all.)
        """
        driver = self._driver
        # Private, driver-specific API: the real terminal drivers (Linux/Windows)
        # implement these, the headless/web ones don't — so feature-detect rather
        # than assume, and leave the flag alone if the toggle isn't available.
        disable = getattr(driver, "_disable_mouse_support", None)
        enable = getattr(driver, "_enable_mouse_support", None)
        if driver is None or disable is None or enable is None:
            self.notify("This terminal driver doesn't support releasing the mouse.",
                        severity="warning")
            return
        release = not self._mouse_released
        try:
            (disable if release else enable)()
        except Exception as e:
            self.notify(f"Couldn't toggle mouse support: {e}", severity="error")
            return
        self._mouse_released = release
        if release:
            self.notify(
                "Mouse released — select and copy text with your terminal as "
                "usual. Press F12 to give it back to the app.",
                timeout=6)
        else:
            self.notify("Mouse captured by the app again.")

    # ---- email reply/forward from lightbar ----
    def _selected_thread(self) -> str | None:
        return self._thread_id_from_key(
            list_tables.current_row_key(self.query_one("#email-list", DataTable)))

    def _email_thread_order(self) -> list[str]:
        """The thread ids currently shown in #email-list, in display order.
        Backs ThreadModal's Left/Right prev/next-message navigation so it can
        page through the same (possibly search-filtered) list the user is
        looking at, without reopening the modal. Summary rows only — revealed
        message rows (`t-<tid>::…`) and the load-more sentinel are skipped."""
        try:
            table = self.query_one("#email-list", DataTable)
        except Exception:
            return []
        return [rk.value[2:] for rk in table.rows
                if (rk.value or "").startswith("t-") and "::" not in rk.value]

    def action_reply(self):
        # Not gated by _require_online(): composing (and, if needed,
        # QUEUING the send) works offline — see ComposeModal.on_mount /
        # _send_now and the offline mutation queue.
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="reply"), self._on_compose_result)
    def action_reply_all(self):
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="reply_all"), self._on_compose_result)
    def action_forward(self):
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="forward"), self._on_compose_result)
    def action_compose_new(self):
        if self._main_tabs().active != "tab-mail":  # Mail tab is Email-only now
            return
        self._open_compose_new()

    def action_mark_unread(self) -> None:
        """Mark the highlighted Email-pane thread UNREAD again, from the list
        (no need to open it). Email pane only; no-op elsewhere. Runs the
        network write on a worker thread per the fetch/apply split, then
        refreshes so the • unread bullet reappears."""
        if self._main_tabs().active != "tab-mail":  # Mail tab is Email-only now
            return
        tid = self._selected_thread()
        if not tid:
            return
        if not self._online:
            self._enqueue_mutation({"type": "mark_unread", "thread_id": tid})
            summary = self._threads_cache.get(tid)
            if summary is not None:
                summary["unread"] = True
            self._apply_email_list(list(self._threads_cache.values()))
            self.notify("Offline — queued, will apply once reconnected.")
            return

        def _work() -> None:
            try:
                gauth.mark_unread(self.svc, tid)
            except Exception as e:
                self.call_from_thread(self.notify, f"Mark-unread error: {e}", severity="error")
                return
            # Reflect the new unread state in the cached summary so the •
            # bullet is right even before the full refresh lands, then refresh.
            summary = self._threads_cache.get(tid)
            if summary is not None:
                summary["unread"] = True
            self._refresh_all_thread()

        self.run_worker(_work, thread=True, exclusive=True)

    def _set_summary_starred(self, summary: dict, starred: bool) -> None:
        """Optimistically flip STARRED in a cached thread summary's labelIds
        so the ★ column in the list is right before the authoritative refresh
        lands. Kept sorted, matching gauth._thread_summary's own labelIds."""
        ids = set(summary.get("labelIds") or [])
        if starred:
            ids.add("STARRED")
        else:
            ids.discard("STARRED")
        summary["labelIds"] = sorted(ids)

    def action_star(self) -> None:
        """Star / unstar the highlighted Email-pane thread from the list
        (no need to open it). Toggles based on the cached summary's current
        STARRED state; STARRED is a system label modify_labels handles like
        any other. Email pane only; no-op elsewhere."""
        if self._main_tabs().active != "tab-mail":  # Mail tab is Email-only now
            return
        tid = self._selected_thread()
        if not tid:
            return
        summary = self._threads_cache.get(tid)
        starred = summary is not None and "STARRED" in (summary.get("labelIds") or [])
        add = [] if starred else ["STARRED"]
        remove = ["STARRED"] if starred else []
        verb = "Unstarred" if starred else "Starred"
        if not self._online:
            self._enqueue_mutation({"type": "modify_labels", "thread_id": tid,
                                    "add": add, "remove": remove})
            if summary is not None:
                self._set_summary_starred(summary, not starred)
                self._apply_email_list(list(self._threads_cache.values()))
            self.notify("Offline — queued, will apply once reconnected.")
            return

        def _work() -> None:
            try:
                gauth.modify_labels(self.svc, tid, add=add, remove=remove)
            except Exception as e:
                self.call_from_thread(self.notify, f"Star error: {e}", severity="error")
                return
            if summary is not None:
                self._set_summary_starred(summary, not starred)
            self.call_from_thread(self.notify, verb)
            self._refresh_all_thread()

        self.run_worker(_work, thread=True, exclusive=True)

    # Window (seconds) after a trash/archive during which Ctrl+Z will still
    # reverse it. Generous vs. Gmail's ~5s toast because a TUI user reaching
    # for undo isn't racing a disappearing toast — but bounded so a stale
    # Ctrl+Z minutes later doesn't resurrect a long-forgotten action.
    _UNDO_WINDOW_SECONDS = 60

    def _record_mail_undo(self, action: str, thread_id: str) -> None:
        """Remember the just-committed reversible mail action so action_undo
        can invert it. `action` is "trash" or "archive"; the inverse is
        untrash / re-add INBOX respectively (see action_undo)."""
        self._pending_undo = {"action": action, "thread_id": thread_id,
                              "ts": time.monotonic()}

    def action_undo(self) -> None:
        """Reverse the most recent trash/archive (Ctrl+Z). Issues the inverse
        API call rather than trying to hold the original back — the write has
        already committed by the time the ThreadModal closed. Online only:
        the inverse is a network write, and an offline trash/archive was only
        queued anyway (cancel it from the pending-actions view instead)."""
        undo = getattr(self, "_pending_undo", None)
        if not undo or time.monotonic() - undo["ts"] > self._UNDO_WINDOW_SECONDS:
            self._pending_undo = None
            self.notify("Nothing to undo")
            return
        if not self._online:
            self.notify("Undo needs a connection", severity="warning")
            return
        self._pending_undo = None  # consume it now so a double Ctrl+Z can't double-undo
        action, tid = undo["action"], undo["thread_id"]

        def _work() -> None:
            try:
                if action == "trash":
                    gauth.untrash_thread(self.svc, tid)
                else:  # archive
                    gauth.modify_labels(self.svc, tid, add=["INBOX"])
            except Exception as e:
                self.call_from_thread(self.notify, f"Undo failed: {e}", severity="error")
                return
            self.call_from_thread(self.notify,
                                  "Restored from Trash" if action == "trash"
                                  else "Moved back to Inbox")
            self._refresh_all_thread()

        self.run_worker(_work, thread=True, exclusive=True)

    # ---- Snooze (ROADMAP P2) ----
    def _snooze_tz(self):
        return self.app_config.tzinfo or dt.datetime.now().astimezone().tzinfo

    def action_snooze(self) -> None:
        """Snooze the highlighted thread until a chosen time ('z'): remove it
        from the Inbox now, resurface it later (see _resurface_due_snoozes).
        Online only — snooze both writes a label and persists a reminder, and
        an offline half of that would be confusing. Mail tab only."""
        if self._main_tabs().active != "tab-mail":
            return
        tid = self._selected_thread()
        if not tid:
            return
        if not self._online:
            self.notify("Snooze needs a connection", severity="warning")
            return
        self._snooze_target = tid
        self.push_screen(SnoozeModal(self._snooze_tz()), self._on_snooze_result)

    def _on_snooze_result(self, when) -> None:
        if when is None:
            return
        tid = getattr(self, "_snooze_target", None)
        if not tid:
            return

        def _work() -> None:
            try:
                gauth.modify_labels(self.svc, tid, remove=["INBOX"])
            except Exception as e:
                self.call_from_thread(self.notify, f"Snooze error: {e}", severity="error")
                return
            self.settings.snoozed[tid] = when.isoformat()
            save_settings(self.settings)
            self.call_from_thread(
                self.notify, f"Snoozed until {when.strftime('%a %m/%d %H:%M')}")
            self._refresh_all_thread()

        self.run_worker(_work, thread=True, exclusive=True)

    def _resurface_due_snoozes(self) -> None:
        """Re-add INBOX to any snoozed thread whose remind-at has passed, then
        drop it from the store. Runs on the refresh worker thread (called from
        _live_refresh_thread) just before the mail fetch. A thread that errors
        (e.g. deleted server-side) is dropped from the store anyway so it can't
        wedge the check every refresh."""
        snoozed = getattr(self.settings, "snoozed", None)
        if not snoozed:
            return
        now = dt.datetime.now(self._snooze_tz())
        due: list[str] = []
        for tid, when in list(snoozed.items()):
            try:
                resurface = dt.datetime.fromisoformat(when)
            except (ValueError, TypeError):
                due.append(tid)  # malformed value -> drop it
                continue
            if resurface.tzinfo is None:
                resurface = resurface.replace(tzinfo=now.tzinfo)
            if resurface <= now:
                due.append(tid)
        if not due:
            return
        for tid in due:
            try:
                gauth.modify_labels(self.svc, tid, add=["INBOX"])
            except Exception:
                pass  # drop from the store regardless — a 404 means it's gone
            snoozed.pop(tid, None)
        save_settings(self.settings)
        self.call_from_thread(self.notify, f"{len(due)} snoozed thread(s) back in Inbox")

    # ---- Multi-select bulk actions (ROADMAP P2) ----
    def action_select_thread(self) -> None:
        """Toggle the highlighted thread's membership in the bulk-action
        selection ('x', Gmail-style), tint its row, and advance the cursor so
        checking a run of threads is just repeated 'x'. Mail tab only."""
        if self._main_tabs().active != "tab-mail":
            return
        table = self.query_one("#email-list", DataTable)
        tid = self._selected_thread()
        if not tid:
            return
        if tid in self._email_selected:
            self._email_selected.discard(tid)
        else:
            self._email_selected.add(tid)
        # Re-tint just this row (no rebuild → cursor stays put), then advance so
        # checking a run of threads is repeated 'x'.
        self._restyle_email_row(tid)
        n = len(self._email_selected)
        self.notify(f"{n} selected" if n else "Selection cleared")
        table.action_cursor_down()

    def action_bulk_actions(self) -> None:
        """Open the bulk-action chooser ('X') for the current selection."""
        if self._main_tabs().active != "tab-mail":
            return
        if not self._email_selected:
            self.notify("Nothing selected — press x to check threads", severity="warning")
            return
        self.push_screen(BulkActionModal(len(self._email_selected)),
                         self._on_bulk_action_result)

    def _on_bulk_action_result(self, choice) -> None:
        if choice in ("archive", "trash"):
            self._bulk_archive_or_trash(choice)
        elif choice == "label":
            self._bulk_open_label_picker()

    def _bulk_archive_or_trash(self, kind: str) -> None:
        ids = list(self._email_selected)
        fn = gauth.archive_thread if kind == "archive" else gauth.trash_thread
        past = "Archived" if kind == "archive" else "Trashed"
        if not self._online:
            for tid in ids:
                self._enqueue_mutation({"type": kind, "thread_id": tid})
                self._threads_cache.pop(tid, None)
            self._email_selected.clear()
            self._apply_email_list(list(self._threads_cache.values()))
            self.notify(f"Offline — queued {len(ids)} {kind}(s), will apply once reconnected.")
            return

        def _work() -> None:
            errs = 0
            for tid in ids:
                try:
                    fn(self.svc, tid)
                except Exception:
                    errs += 1
            self._email_selected.clear()
            msg = f"{past} {len(ids) - errs} thread(s)"
            if errs:
                msg += f" ({errs} failed)"
            self.call_from_thread(self.notify, msg,
                                  severity="error" if errs else "information")
            self._refresh_all_thread()

        self.run_worker(_work, thread=True, exclusive=True)

    def _bulk_open_label_picker(self) -> None:
        labels = getattr(self, "_labels_cache", [])
        pickable = [l for l in labels
                    if l.get("type") != "system" and l.get("id") and l.get("name")]
        if not pickable:
            self.notify("No labels available to assign", severity="warning")
            return
        self.push_screen(LabelPickerModal(pickable, applied_ids=frozenset()),
                         self._on_bulk_label_result)

    def _on_bulk_label_result(self, add_ids) -> None:
        if not add_ids:
            return
        ids = list(self._email_selected)
        add = list(add_ids)
        if not self._online:
            for tid in ids:
                self._enqueue_mutation({"type": "modify_labels", "thread_id": tid, "add": add})
            self._email_selected.clear()
            self.notify(f"Offline — queued {len(add)} label(s) on {len(ids)} thread(s).")
            return

        def _work() -> None:
            errs = 0
            for tid in ids:
                try:
                    gauth.modify_labels(self.svc, tid, add=add)
                except Exception:
                    errs += 1
            self._email_selected.clear()
            msg = f"Applied {len(add)} label(s) to {len(ids) - errs} thread(s)"
            if errs:
                msg += f" ({errs} failed)"
            self.call_from_thread(self.notify, msg,
                                  severity="error" if errs else "information")
            self._refresh_all_thread()

        self.run_worker(_work, thread=True, exclusive=True)

    def _email_reference(self, tid: str) -> tuple[str, str]:
        """(subject, notes) for turning thread `tid` into a task or event,
        built from the cached thread summary so it works offline too. `notes`
        is the sender + snippet + a Gmail permalink back to the thread."""
        s = self._threads_cache.get(tid, {})
        subject = s.get("subject") or "(no subject)"
        permalink = f"https://mail.google.com/mail/u/0/#all/{tid}"
        parts = []
        if s.get("from"):
            parts.append(f"From: {s['from']}")
        if (s.get("snippet") or "").strip():
            parts.append(s["snippet"].strip())
        parts.append(permalink)
        return subject, "\n\n".join(parts)

    def _open_email_to_task(self, tid: str | None) -> None:
        if not tid:
            return
        if not self._tasklists:
            self.notify("No task lists available", severity="warning")
            return
        subject, notes = self._email_reference(tid)
        self.push_screen(EmailToTaskModal(self.svc, self._tasklists, subject, notes),
                         self._on_email_to_task_result)

    def _on_email_to_task_result(self, result) -> None:
        # "created" (online) refetches so the new task shows on the Dashboard
        # Tasks card; "queued" (offline) is already overlaid via the pending-
        # mutation reconcile, so just re-render the tasks from cache.
        if result == "created" and self._online:
            self.run_worker(self._refresh_all_thread, thread=True, exclusive=True)
        elif result == "queued":
            self._refresh_task_list()

    def _open_email_to_event(self, tid: str | None) -> None:
        if not tid:
            return
        subject, notes = self._email_reference(tid)
        self.push_screen(
            CreateEventModal(self.svc, dt.date.today(), default_title=subject,
                             description=notes),
            self._on_create_event_result)

    def action_email_to_task(self) -> None:
        """Create a Google Task from the highlighted Email-pane thread ('t').
        Mail tab only; no-op elsewhere."""
        if self._main_tabs().active != "tab-mail":
            return
        self._open_email_to_task(self._selected_thread())

    def action_email_to_event(self) -> None:
        """Create a Calendar event from the highlighted Email-pane thread
        ('e'). Mail tab only; no-op elsewhere."""
        if self._main_tabs().active != "tab-mail":
            return
        self._open_email_to_event(self._selected_thread())

    def action_focus_label_select(self) -> None:
        if self._main_tabs().active != "tab-mail":  # Mail tab is Email-only now
            return
        try:
            sel = self.query_one("#email-label-select", Select)
            sel.remove_class("hidden")
            sel.focus()
            sel.expanded = True
        except Exception:
            pass

    def _hide_label_select(self) -> None:
        """Esc counterpart to action_focus_label_select: collapses and
        re-hides the labels Select, then hands focus back to #email-list —
        same hidden-until-summoned pattern as _hide_pane_search."""
        try:
            sel = self.query_one("#email-label-select", Select)
        except Exception:
            return
        sel.expanded = False
        sel.add_class("hidden")
        try:
            self.query_one("#email-list").focus()
        except Exception:
            pass

    def action_focus_search(self) -> None:
        # Dispatched per active TAB, then (for Mail) per active PANE — see
        # AGENTS.md §2 for the tab/pane distinction. The Mail panes / Drive /
        # News below wire "/" onto a live ListView FILTER. The Calendar tab is
        # different: its Month/Week views are a fetched date GRID, not a list,
        # so "/" is a "jump to next matching day/hour-cell" (find-next), not a
        # filter — the Input is Enter-triggered (see on_input_submitted /
        # _cal_find), like ThreadModal's find-in-thread, not live-as-you-type.
        tab = self._main_tabs().active
        if tab == "tab-mail":
            self._show_pane_search("email-search")
        elif tab == "tab-dashboard":
            pane = self._dash_active
            if pane == "tasks":
                self._show_pane_search("tasks-search")
            elif pane == "dash-today":
                self._show_pane_search("events-search")
        elif tab == "tab-calendar":
            self._show_pane_search("cal-search")
        elif tab == "tab-drive":
            self._show_pane_search("drive-search")
        elif tab == "tab-news":
            self._show_pane_search("news-search")
        elif tab == "tab-contacts":
            # Contacts already has its own live fuzzy search
            # (_fuzzy_filter_contacts) auto-focused on tab activation
            # (on_tabbed_content_tab_activated) — this just makes it
            # reachable via "/" too, e.g. after Tab/arrow-keying focus away
            # to #contacts-list. Unlike the bars above, Contacts' search box
            # is never hidden — it's the tab's primary control, not a
            # summon-on-demand filter.
            self.query_one("#contacts-search", Input).focus()

    def _show_pane_search(self, search_id: str) -> None:
        """Reveal one of _PANE_SEARCH_BARS's hidden-by-default search bars
        and focus its Input. Paired with _hide_pane_search (Esc)."""
        bar_id, _ = _PANE_SEARCH_BARS[search_id]
        self.query_one(f"#{bar_id}").remove_class("hidden")
        self.query_one(f"#{search_id}", Input).focus()

    def _hide_pane_search(self, search_id: str) -> None:
        """Esc counterpart to _show_pane_search: clears the query (which
        re-runs the Input.Changed live-filter handlers back to the
        unfiltered list, same as backspacing to empty), re-hides the bar,
        and hands focus back to the list/grid it was filtering — mirrors
        ThreadModal._hide_search's find-in-thread pattern."""
        spec = _PANE_SEARCH_BARS.get(search_id)
        if spec is None:
            return
        bar_id, refocus_id = spec
        try:
            search = self.query_one(f"#{search_id}", Input)
        except Exception:
            return
        search.value = ""
        try:
            self.query_one(f"#{bar_id}").add_class("hidden")
        except Exception:
            pass
        if search_id == "cal-search":
            week = self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week"
            refocus_id = "cal-week-grid" if week else "cal-grid"
        if refocus_id:
            try:
                self.query_one(f"#{refocus_id}").focus()
            except Exception:
                pass

    def _require_online(self) -> bool:
        if not self._online:
            self.notify("Can't do that while offline", severity="warning")
            return False
        return True

    # ---- email: Space = lightweight inline expand (NOT the full thread-tree
    # UI — see ROADMAP's separate P2 "Threading depth" item). DataTable model
    # (ROADMAP P3): expanding a thread reveals one real row per message against
    # the full backing list (rebuild, since DataTable has no insert-at-index),
    # replacing the old tall multi-line Label. For a >1-message thread the full
    # thread is fetched (same gauth call as ThreadModal/Enter) and cached in
    # self._thread_full_cache so repeated collapse/expand doesn't re-fetch. ----
    def _toggle_thread_expand(self, thread_id: str) -> None:
        th = self._threads_cache.get(thread_id)
        if not th:
            return
        if thread_id in self._expanded_thread_ids:
            self._expanded_thread_ids.discard(thread_id)
            self._rebuild_email_table()
            return
        self._expanded_thread_ids.add(thread_id)
        # >1-message thread with no cached bodies yet: rebuild now (shows a
        # transient 'Loading…' row via _expanded_message_rows) and fetch.
        if th.get("count", 1) > 1 and thread_id not in self._thread_full_cache:
            self.run_worker(lambda: self._fetch_thread_preview(thread_id),
                             thread=True, exclusive=False, group="thread-preview")
        self._rebuild_email_table()

    def _fetch_thread_preview(self, thread_id: str) -> None:
        try:
            msgs = gauth.get_thread(self.svc, thread_id)
        except Exception:
            self.call_from_thread(self._apply_thread_preview_error, thread_id)
            return
        self.call_from_thread(self._apply_thread_preview, thread_id, msgs)

    def _apply_thread_preview(self, thread_id: str, msgs: list[dict]) -> None:
        self._thread_full_cache[thread_id] = msgs
        # Rebuild so the transient 'Loading…' row becomes the real message rows
        # (only if the thread is still expanded — the user may have collapsed it
        # while the fetch was in flight).
        if thread_id in self._expanded_thread_ids and thread_id in self._threads_cache:
            self._rebuild_email_table()

    def _apply_thread_preview_error(self, thread_id: str) -> None:
        th = self._threads_cache.get(thread_id)
        if not th or thread_id not in self._expanded_thread_ids:
            return
        # Fall back to a single synthesized row (the snippet + a hint to open
        # the full thread) so the expansion doesn't sit on 'Loading…' forever.
        snippet = (th.get("snippet") or "").strip()
        self._thread_full_cache[thread_id] = [{
            "from": th.get("from", ""),
            "body": (f"{snippet}  " if snippet else "")
                    + f"({th['count']} messages — press Enter for full thread)"}]
        self._rebuild_email_table()

    # ---- email preview pane ("p" / action_toggle_preview) ----
    # Outlook-style: hidden by default (Settings.email_preview_default_visible
    # seeds the session state), and while visible, live-updates as the
    # highlight bar moves -- same debounce-timer + generation-counter +
    # session-memoization shape as Drive's preview column
    # (_drive_on_highlight/_drive_start_preview, further below), reusing
    # self._thread_full_cache (already populated by Space-to-expand, see
    # _toggle_thread_expand above) instead of a second cache dict.
    def _toggle_email_preview(self) -> None:
        self._email_preview_visible = not self._email_preview_visible
        self._apply_email_preview_visibility()
        if self._email_preview_visible:
            tid = self._selected_thread()
            if tid:
                self._email_start_preview(tid)

    def _apply_email_preview_visibility(self) -> None:
        try:
            preview_col = self.query_one("#right")
            list_col = self.query_one("#left")
        except Exception:
            return
        hidden = not self._email_preview_visible
        preview_col.set_class(hidden, "email-preview-hidden")
        list_col.set_class(hidden, "email-list-full")

    def _email_on_highlight(self, key: str) -> None:
        # Hidden pane costs nothing: no timer, no fetch, while the user
        # arrows through mail with the preview off (the common case, since
        # it's off by default) -- mirrors Drive's debounce exactly, just
        # gated on visibility first. A revealed message row (t-<tid>::…)
        # previews its parent thread, same as its summary row.
        if not self._email_preview_visible:
            return
        thread_id = self._thread_id_from_key(key)
        if not thread_id:
            return
        if self._email_preview_timer is not None:
            self._email_preview_timer.stop()
        self._email_preview_timer = self.set_timer(
            _PREVIEW_DEBOUNCE, lambda: self._email_start_preview(thread_id))

    def _email_start_preview(self, thread_id: str) -> None:
        self._email_preview_timer = None
        self._email_preview_gen += 1
        gen = self._email_preview_gen
        cached = self._thread_full_cache.get(thread_id)
        if cached is not None:
            self._apply_email_preview(gen, thread_id, cached)
            return
        try:
            meta = self.query_one("#email-preview-meta", Static)
            meta.update("Loading…")
        except Exception:
            pass
        try:
            doc_view = self.query_one("#email-preview-doc", DocumentView)
            doc_view.document = render.parse_feed_entry("", "", base_url="", ascii_mode=self.settings.ascii_mode)
        except Exception:
            pass
        self.run_worker(lambda: self._email_preview_thread(gen, thread_id),
                        thread=True, exclusive=True, group="email-preview")

    def _cached_thread_body(self, thread_id: str) -> list[dict] | None:
        """The persistent thread_body row for thread_id, but only if its
        stamped historyId still matches the (also cached) thread summary --
        same revalidation ThreadModal._fetch_thread uses. Returns None if
        this thread was never opened before or the cached body is stale, so
        callers can tell "no current body cached" apart from "cache hit"."""
        summary = self._threads_cache.get(thread_id) or {}
        hid = str(summary.get("historyId") or "")
        if not (self._cache and hid):
            return None
        hit = self._cache.get("thread_body", thread_id)
        if hit and str(hit.get("historyId") or "") == hid:
            return hit.get("msgs")
        return None

    def _email_preview_thread(self, gen: int, thread_id: str) -> None:
        """MUST run with thread=True -- gauth.get_thread is an HTTPS round
        trip, same fetch/apply-split reasoning as _drive_preview_thread."""
        # _thread_full_cache is in-memory only (empty after a restart), but
        # the persistent thread_body cache (shared with ThreadModal) may
        # already have a current body -- e.g. from an earlier session, or
        # from opening the full thread before just arrowing back over it.
        # Checking it here, online or offline, avoids a needless refetch and
        # (offline) avoids falling back to a snippet-only view.
        cached_msgs = self._cached_thread_body(thread_id)
        if cached_msgs is not None:
            self._thread_full_cache[thread_id] = cached_msgs
            self.call_from_thread(self._apply_email_preview, gen, thread_id, cached_msgs)
            return
        if not self._online:
            self.call_from_thread(self._apply_email_preview_offline, gen, thread_id)
            return
        try:
            msgs = gauth.get_thread(self.svc, thread_id)
        except Exception as ex:
            self.call_from_thread(self._apply_email_preview_error, gen, thread_id, ex)
            return
        self._thread_full_cache[thread_id] = msgs
        summary = self._threads_cache.get(thread_id) or {}
        hid = str(summary.get("historyId") or "")
        if self._cache and hid:
            self._cache.put("thread_body", thread_id, {"historyId": hid, "msgs": msgs})
        self.call_from_thread(self._apply_email_preview, gen, thread_id, msgs)

    def _apply_email_preview(self, gen: int, thread_id: str, msgs: list[dict]) -> None:
        if gen != self._email_preview_gen:
            return  # highlight moved on; a newer preview owns the pane now
        if not msgs:
            return
        m = msgs[-1]  # latest message -- Enter/ThreadModal remains the way to see the full thread
        header = f"From: {m.get('from', '')}    Date: {m.get('date', '')}"
        if len(msgs) > 1:
            header += f"\n({len(msgs)} messages — press Enter for the full thread)"
        html_body = (m.get("html_body") or "").strip()
        text_body = m.get("body") or ""
        source = html_body if html_body else text_body
        doc = render.parse_feed_entry(m.get("subject", ""), source, base_url="",
                                       ascii_mode=self.settings.ascii_mode)
        try:
            self.query_one("#email-preview-meta", Static).update(header)
            self.query_one("#email-preview-doc", DocumentView).document = doc
        except Exception:
            pass

    def _apply_email_preview_error(self, gen: int, thread_id: str, error: Exception) -> None:
        if gen != self._email_preview_gen:
            return
        try:
            self.query_one("#email-preview-meta", Static).update(f"(preview error: {error})")
        except Exception:
            pass

    def _apply_email_preview_offline(self, gen: int, thread_id: str) -> None:
        if gen != self._email_preview_gen:
            return
        th = self._threads_cache.get(thread_id)
        snippet = (th.get("snippet") or "").strip() if th else ""
        text = snippet or "(offline — this thread hasn't been opened yet, so no cached body is available)"
        try:
            self.query_one("#email-preview-meta", Static).update("(offline — showing snippet)")
            self.query_one("#email-preview-doc", DocumentView).document = render.parse_feed_entry(
                "", text, base_url="", ascii_mode=self.settings.ascii_mode)
        except Exception:
            pass

    # ---- tasks ----
    def _selected_task(self) -> dict | None:
        cid = list_tables.current_row_key(self.query_one("#task-list", DataTable)) or ""
        if not cid.startswith("k-"):
            return None
        raw = cid[2:]  # "<list>-<id>"
        lid, _, tid = raw.rpartition("-")
        # Reconciled, not raw _tasks_cache: an offline-created (temp-id) task
        # is only in the overlay, but the user must still be able to select it
        # (e.g. to delete it, which cancels its queued create).
        for t in self._reconcile_tasks(getattr(self, "_tasks_cache", [])):
            if t.get("_list") == lid and t.get("id") == tid:
                return t
        return None

    def _refresh_task_list(self) -> None:
        # Debounced keystroke path for #tasks-search — re-renders from the
        # already-fetched self._tasks_cache, no Google Tasks call per keystroke.
        # Synchronous now: #task-list is a DataTable (clear is immediate), so
        # the old separate-group/generation-counter guard that kept a keystroke
        # from cancelling an in-flight _apply_mail_data_async mid-rebuild is no
        # longer needed — the task rebuild here doesn't touch the still-async
        # email/dash-mail clears in that worker.
        tasks = self._reconcile_tasks(self._tasks_cache)  # overlay offline creates/deletes
        try:
            query = self.query_one("#tasks-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_tasks(tasks, query) if query.strip() else tasks
        self._tasks_by_cid = {}
        _append_task_items(self.query_one("#task-list", DataTable), visible,
                           by_cid=self._tasks_by_cid)

    def action_toggle_task(self):
        t = self._selected_task()
        if not t:
            return
        done = t.get("status") != "completed"
        if not self._online:
            if _is_temp_id(t["id"]):
                # Task is itself a not-yet-synced offline create: record the
                # desired completion on the queued create rather than enqueue a
                # toggle against an id that doesn't exist server-side yet.
                self._toggle_pending_task(t["_list"], t["id"], done)
            else:
                self._enqueue_mutation(
                    {"type": "toggle_task", "list_id": t["_list"], "task_id": t["id"], "done": done})
            # Re-render from the queue overlay; a temp task is rebuilt fresh
            # each render (see _pending_task_creates), so the flipped state
            # comes back through _toggle_pending_task, not this local dict.
            self._refresh_task_list()
            self.notify("Offline — queued, will apply once reconnected.")
            return
        # Both the write AND the refresh that follows it are network calls, so
        # the whole sequence goes on a worker thread — `set_task_status` used
        # to run inline here, freezing the UI on an HTTPS round-trip for the
        # duration of a single keypress.
        self.run_worker(
            lambda: self._toggle_task_thread(t["_list"], t["id"], done),
            thread=True, exclusive=True, group="task-toggle")

    def _toggle_task_thread(self, list_id: str, task_id: str, done: bool) -> None:
        try:
            gauth.set_task_status(self.svc, list_id, task_id, done)
        except Exception as e:
            self.call_from_thread(self.notify, f"Task update failed: {e}", severity="error")
            return
        self._refresh_all_thread()

    # ---- events ----
    def _refresh_event_list(self) -> None:
        # Debounced keystroke path for #events-search — synchronous rebuild of
        # the TODAY card DataTable (see _refresh_task_list for why the old
        # separate-group/generation-counter guard is gone).
        self._fill_today_events(getattr(self, "_events_cache", []))

    _EVENT_COLS = [("When", 10), ("Summary", None)]

    def _fill_today_events(self, events) -> None:
        """Shared TODAY-card populate (used by the full mail-data apply and the
        #events-search debounce path). Filters to today, overlays offline
        creates, applies any "/" search filter, and shows a friendly empty
        state when there's nothing today (but not when a search simply had no
        matches — that would misread as "no events today"). No "Load more" row:
        the card is today-scoped, so there's nothing to paginate into.

        DataTable now: When + Summary columns. Each row keeps its `e-<id>` key
        so Enter still opens EventModal via _open_event_by_id."""
        table = self.query_one("#event-list", DataTable)
        disp = _todays_events(self._reconcile_events(events), tz=self.app_config.tzinfo)
        try:
            query = self.query_one("#events-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_events(disp, query) if query.strip() else disp
        self._events_by_cid = {}
        if visible:
            seen: set = set()
            rows: list[tuple[str, list]] = []
            for e in visible:
                cid = _unique_id(_mk_id("e", e["id"]), seen)
                self._events_by_cid[cid] = e
                pend = _PENDING_MARK if e.get("_pending") else ""
                rows.append((cid, [f"{pend}{_event_when(e)}", e.get("summary", "")]))
        elif not query.strip():
            rows = [("dash-empty-events", ["", "No events today 🎉"])]
        else:
            rows = []  # search with no matches: empty grid, not "no events"
        list_tables.rebuild_flat_table(table, self._EVENT_COLS, rows,
                                       flex_min=10, fallback_width=_EVENT_ROW_DEFAULT_W)

    # From/Date fixed, Subject flexible -- mirrors the Dashboard NEWS card's
    # (Feed, Headline) columns and the Mail tab's own From/Subject/.../Date
    # widths (_EMAIL_SENDER_W/_EMAIL_DATE_W), just narrower for this half-width
    # card. Replaced 2026-07-23: this used to be a single flex column holding
    # "{frm}   {subj}   {date_str}" as one hand-joined string -- DataTable only
    # measures/aligns discrete CELLS across real columns, not substrings
    # within one, so that rendered exactly like the old ListView's manual
    # padding (ragged, since names/subjects vary in length) despite already
    # being a DataTable.
    _DASH_MAIL_COLS = [("From", 14), ("Subject", None), ("Date", _EMAIL_DATE_W)]

    def _populate_dash_mail(self, threads) -> None:
        """Dashboard MAIL card: an unread count header (Enter jumps to the Mail
        tab) followed by up to six most-recent unread threads (Enter opens the
        thread). Row ids are `dm-open` (the header) and `dm-<threadId>`; see
        on_list_view_selected's dm- branch. markup=False on the subject rows
        for the same reason the News list uses it — subjects are arbitrary
        external text that Textual's markup parser would otherwise choke on."""
        unread = [t for t in threads if t.get("unread")]
        # A bold "unread count" header row (dm-open, Enter jumps to Mail) then
        # up to six unread threads, each a real (From, Subject, Date) row —
        # DataTable measures/aligns real columns, not substrings.
        rows: list[tuple[str, list]] = [
            ("dm-open", [Text(f"📬 {len(unread)} unread", style="bold"), "", ""])]
        seen: set = {"dm-open"}
        for t in unread[:6]:
            cid = _unique_id(_mk_id("dm", t["threadId"]), seen)
            frm = _format_sender(t.get("from", ""), False)
            subj = t.get("subject") or "(no subject)"
            date_str = _fmt_email_date(t.get("date", ""))
            rows.append((cid, [frm, subj, date_str]))
        list_tables.rebuild_flat_table(self.query_one("#dash-mail-list", DataTable),
                                       self._DASH_MAIL_COLS, rows, flex_min=20)

    def _apply_dashboard_extras(self, weather=_DASH_EXTRA_UNCHANGED, stocks=_DASH_EXTRA_UNCHANGED,
                                 word_of_day=_DASH_EXTRA_UNCHANGED, wiki_potd=_DASH_EXTRA_UNCHANGED) -> None:
        """Populates the four external Dashboard cards (WEATHER/STOCKS/WORD OF
        THE DAY/PICTURE OF THE DAY, ROADMAP P4, 2026-07-19). Each arg is
        either real data, explicit None (paint the card's empty state — what
        _load_from_cache and _apply_dashboard_panes_enabled pass, both doing
        a full repaint from whatever Cache currently has), or the
        _DASH_EXTRA_UNCHANGED default (leave this card exactly as painted —
        what _live_refresh_thread passes for a card whose fetch this round
        was skipped or failed, so a transient error doesn't blank an
        already-populated card). Dispatched through a properly-awaited
        worker, not inlined, for the same reason _apply_mail_data is
        (AGENTS.md's ListView.clear() NOTE): each card's rows reuse the same
        fixed ids every refresh, so a fire-and-forget clear+repopulate risks
        DuplicateIds if a later refresh's insert races the prior one's
        removal."""
        self.run_worker(
            self._apply_dashboard_extras_async(weather, stocks, word_of_day, wiki_potd),
            exclusive=True, group="dashboard-extras-apply")

    async def _apply_dashboard_extras_async(self, weather, stocks, word_of_day, wiki_potd) -> None:
        if weather is not _DASH_EXTRA_UNCHANGED:
            await self.query_one("#dash-weather-list").clear()
            self._fill_dash_weather(weather)
        if stocks is not _DASH_EXTRA_UNCHANGED:
            await self.query_one("#dash-stocks-list").clear()
            self._fill_dash_stocks(stocks)
        if word_of_day is not _DASH_EXTRA_UNCHANGED:
            self._word_of_day = word_of_day
            await self.query_one("#dash-word-list").clear()
            self._fill_dash_word(word_of_day)
        if wiki_potd is not _DASH_EXTRA_UNCHANGED:
            self._wiki_potd = wiki_potd
            await self.query_one("#dash-potd-list").clear()
            self._fill_dash_potd(wiki_potd)

    def _fill_dash_weather(self, weather: dict | None) -> None:
        lst = self.query_one("#dash-weather-list")
        if not weather:
            lst.append(ListItem(Label("Not available yet — check back after the next refresh"),
                                 id="dash-weather-empty"))
            return
        wind = weather.get("wind_mph")
        wind_str = f"{wind:.0f} mph" if isinstance(wind, (int, float)) else "—"
        lines = [
            f"📍 {weather.get('location', '')}",
            f"🌡  {_fmt_deg(weather.get('temp_f'))}  {weather.get('condition', '')}",
            f"↕  H {_fmt_deg(weather.get('high_f'))} / L {_fmt_deg(weather.get('low_f'))}",
            f"💨 Wind {wind_str}",
        ]
        lst.append(ListItem(Label("\n".join(lines), markup=False), id="dash-weather-info"))

    def _fill_dash_stocks(self, stocks: list[dict] | None) -> None:
        lst = self.query_one("#dash-stocks-list")
        if not stocks:
            msg = ("Add symbols in Settings → Dashboard to enable"
                   if not self.settings.stock_symbols else "No quotes available")
            lst.append(ListItem(Label(msg), id="dash-stocks-empty"))
            return
        for q in stocks:
            change, pct = q.get("change", 0.0), q.get("change_pct", 0.0)
            arrow = "▲" if change > 0 else "▼" if change < 0 else "•"
            lst.append(ListItem(
                Label(f"{q.get('symbol', ''):<6} ${q.get('price', 0.0):.2f}  "
                      f"{arrow} {change:+.2f} ({pct:+.2f}%)", markup=False),
                id=_mk_id("ds", q.get("symbol", ""))))

    def _fill_dash_word(self, word_of_day: dict | None) -> None:
        lst = self.query_one("#dash-word-list")
        if not word_of_day:
            lst.append(ListItem(Label("Not available yet — check back after the next refresh"),
                                 id="dash-word-empty"))
            return
        definition = (word_of_day.get("definition") or "")[:200]
        lst.append(ListItem(
            Label(f"{word_of_day.get('word', '')}\n{definition}", markup=False), id="dw-open"))

    def _fill_dash_potd(self, wiki_potd: dict | None) -> None:
        lst = self.query_one("#dash-potd-list")
        if not wiki_potd:
            lst.append(ListItem(Label("Not available yet — check back after the next refresh"),
                                 id="dash-potd-empty"))
            return
        description = (wiki_potd.get("description") or "")[:200]
        lst.append(ListItem(
            Label(f"{wiki_potd.get('title', '')}\n{description}", markup=False), id="dp-open"))

    def _highlighted_event_id(self) -> str | None:
        cid = list_tables.current_row_key(self.query_one("#event-list", DataTable)) or ""
        return cid[2:] if cid.startswith("e-") else None

    def _open_dash_list_row(self, cid: str) -> None:
        """Enter/Space on a Dashboard MAIL or NEWS mini-card row (both
        DataTables). dm-open jumps to the Mail tab; dm-<threadId> opens the
        thread; dn-<cid> opens the news entry — the same actions the old
        on_list_view_selected dm-/dn- branches performed."""
        if cid == "dm-open":
            self._goto_tab("tab-mail")
        elif cid.startswith("dm-"):
            tid = cid[3:]
            self.push_screen(ThreadModal(self.svc, tid, thread_ids=[tid], index=0),
                              self._on_thread_modal_result)
        elif cid.startswith("dn-"):
            entry = self._dash_news_by_cid.get(cid)
            if entry:
                self.push_screen(NewsEntryModal(entry))

    def _open_event_by_id(self, eid: str) -> None:
        # Reconciled so an offline-created (temp-id) event can be opened too.
        for e in self._reconcile_events(getattr(self, "_events_cache", [])):
            if e.get("id") == eid:
                self.push_screen(EventModal(e))
                return

    # ---- contextual space ----
    def action_context_space(self) -> None:
        tab = self._main_tabs().active
        if tab == "tab-news":
            cid = list_tables.current_row_key(self.query_one("#news-list", DataTable))
            entry = self._news_by_cid.get(cid or "")
            if entry:
                self.push_screen(NewsEntryModal(entry))
            return
        if tab == "tab-contacts":
            cid = list_tables.current_row_key(self.query_one("#contacts-list", DataTable))
            if cid and cid.startswith("ct-"):
                self._open_contact_detail(cid)
            return
        if tab == "tab-mail":
            tid = self._selected_thread()
            if tid:
                self._toggle_thread_expand(tid)
            return
        if tab != "tab-dashboard":
            return
        pane = self._dash_active
        if pane == "tasks":
            self.action_toggle_task()
        elif pane == "dash-today":
            eid = self._highlighted_event_id()
            if eid:
                self._open_event_by_id(eid)
        elif pane == "dash-calendar":
            # Space mirrors Enter here too (same reasoning as dash-mail/
            # dash-news below) -- Enter arrives as a DataTable.CellSelected
            # event (_dash_cal_cell_selected), but Space doesn't generate one,
            # so read the highlighted cell directly instead.
            self._dash_cal_open_highlighted()
        elif pane in ("dash-mail", "dash-news"):
            # Space mirrors Enter on these cards (open the highlighted item) —
            # neither has a toggle/expand action of its own, so reuse the same
            # dispatch the DataTable RowSelected handler uses, keyed by the row
            # under the cursor.
            lst_id = "#dash-mail-list" if pane == "dash-mail" else "#dash-news-list"
            cid = list_tables.current_row_key(self.query_one(lst_id, DataTable))
            if cid:
                self._open_dash_list_row(cid)

    def action_dash_jump(self) -> None:
        """"g": jump from the focused Dashboard card to wherever its data
        actually comes from -- Mail/News switch to the matching app tab;
        Calendar/Today/Tasks all go to the Calendar tab (Tasks and the CLOCK
        have no tab of their own, and Calendar/Today already share the
        Calendar tab's own month/event state); Weather/Stocks/Word/Picture
        have no app tab, so this opens the real external source in the
        Browser tab instead (Word/Picture reuse the exact link Enter already
        opens on those two). Hermes and CLOCK have nowhere sensible to jump
        to, so they're a no-op, same as every other Dashboard-tab-gated
        single-letter action when it doesn't apply to the focused card."""
        if self._main_tabs().active != "tab-dashboard":
            return
        pane = self._dash_active
        if pane == "dash-mail":
            self._goto_tab("tab-mail")
        elif pane == "dash-news":
            self._goto_tab("tab-news")
        elif pane in ("dash-calendar", "dash-today", "tasks"):
            self._goto_tab("tab-calendar")
        elif pane == "dash-weather":
            loc = self.settings.weather_location or self._resolve_weather_location()
            self._open_dashboard_link({"link": "https://www.google.com/search?q="
                                       f"weather+{urllib.parse.quote(loc)}"})
        elif pane == "dash-stocks":
            # ListView.highlighted_child stays None until the user actually
            # presses an arrow key in it -- focus() alone doesn't select the
            # first item -- so fall back to the first row if nothing's been
            # explicitly highlighted yet, rather than silently no-op'ing.
            lst = self.query_one("#dash-stocks-list", ListView)
            item = lst.highlighted_child or next(iter(lst.children), None)
            cid = item.id if item else None
            if cid and cid.startswith("ds-"):
                symbol = cid.removeprefix("ds-")
                self._open_dashboard_link(
                    {"link": f"https://www.google.com/finance/quote/{symbol}"})
        elif pane == "dash-word":
            self._open_dashboard_link(self._word_of_day)
        elif pane == "dash-potd":
            self._open_dashboard_link(self._wiki_potd)

    # ---- list selections (Enter) ----
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Only the single-item Dashboard info cards (WORD/PICTURE OF THE DAY)
        # are still ListViews here; every other list is a DataTable (see
        # on_data_table_row_selected).
        cid = event.item.id or ""
        if cid == "dw-open":
            # WORD OF THE DAY — no in-terminal detail view (it's a single short
            # definition already), so Enter opens the full Merriam-Webster
            # entry in the Browser tab instead.
            self._open_dashboard_link(self._word_of_day)
        elif cid == "dp-open":
            # PICTURE OF THE DAY — same reasoning as dw-open; this app can't
            # render the image itself yet (ROADMAP: Drive image preview needs
            # textual-image), so Enter opens the Wikipedia file page.
            self._open_dashboard_link(self._wiki_potd)

    def _open_email_thread(self, tid: str) -> None:
        """Open ThreadModal for `tid`, paged over the current display order so
        the modal's Left/Right nav walks the same (filtered) list."""
        order = self._email_thread_order()
        try:
            index = order.index(tid)
        except ValueError:
            order, index = [tid], 0
        self.push_screen(ThreadModal(self.svc, tid, thread_ids=order, index=index),
                          self._on_thread_modal_result)

    # ---- modal returns ----
    # NOTE: ModalScreen.Dismissed doesn't exist in the installed Textual
    # version, so an on_dismiss(self, event) handler here is silently never
    # called — every push_screen that needs to react to its result MUST pass
    # an explicit callback instead (see AGENTS.md §2). The callback fires
    # BEFORE the screen is actually popped off the stack, so anything that
    # pushes another screen or touches widgets on the screen underneath is
    # deferred one step via call_after_refresh.
    def _on_thread_modal_result(self, result) -> None:
        if isinstance(result, tuple) and result and result[0] == "compose":
            _, tid, mode = result
            self.call_after_refresh(self._open_compose_from_thread, tid, mode)
        elif result == "refresh":
            # ThreadModal trashed/archived a thread — refetch the mail list so
            # the removed thread drops out of the Email pane (same post-write
            # refresh path the reply/forward "sent" flow uses).
            self.run_worker(self._refresh_all_thread, thread=True, exclusive=True)

    def _open_compose_from_thread(self, tid: str, mode: str) -> None:
        self.push_screen(ComposeModal(self.svc, tid, mode), self._on_compose_result)

    def _on_compose_result(self, result) -> None:
        # "draft" refreshes too so a newly-saved draft shows up if the Email
        # pane happens to be viewing the Drafts label.
        if result in ("sent", "draft"):
            self.run_worker(self._refresh_all_thread, thread=True, exclusive=True)

    def _on_task_modal_result(self, mutated) -> None:
        # TaskModal (P2, 2026-07-15 subtask add/toggle/delete) dismisses
        # with whether it mutated anything; only then is it safe to touch
        # #task-list — see TaskModal's class docstring for the NoMatches
        # this avoids by NOT refreshing while the modal was still on top.
        if not mutated:
            return
        if self._online:
            self.run_worker(self._refresh_all_thread, thread=True, exclusive=True)
        else:
            # An offline subtask toggle already mutated self._tasks_cache's
            # dicts in place (TaskModal.subtasks holds the SAME objects, not
            # copies — see _child_tasks) and got queued for replay; a real
            # refetch here would just fail and notify a spurious "Refresh
            # error" right after the user's optimistic toggle succeeded. A
            # local re-render is enough — _replay_pending_mutations_thread
            # triggers the real refresh once reconnected.
            self._refresh_task_list()

    # ---- hermes ask / browser address bar (shared Input.Submitted) ----
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "hermes-input":
            self._hermes_submit(event)
        elif event.input.id == "browser-url":
            self._browser_submit(event)
        elif event.input.id == "settings-feed-url":
            self._add_feed_url()
        elif event.input.id in ("nav-origin", "nav-destination"):
            self._nav_go()
        elif event.input.id == "cal-search":
            self._cal_find(event.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "contacts-search":
            # Debounced: each keystroke fuzzy-matches the ENTIRE contact book
            # and rebuilds every row of the list. Restarting the timer (rather
            # than stacking one rebuild per character) means typing "brad"
            # costs one rebuild instead of four.
            if self._contacts_search_timer is not None:
                self._contacts_search_timer.stop()
            self._contacts_search_timer = self.set_timer(
                _CONTACTS_SEARCH_DEBOUNCE, self._refresh_contacts_list)
        elif event.input.id == "email-search":
            # Same debounce-then-rebuild pattern as contacts, but re-renders
            # from the already-fetched self._threads_cache — no Gmail call
            # per keystroke. Goes through _apply_email_list so the existing
            # ListView.clear()-is-async / generation-counter handling
            # (AGENTS.md's NOTE) covers this path too, not just refresh/label
            # switches.
            if self._email_search_timer is not None:
                self._email_search_timer.stop()
            self._email_search_timer = self.set_timer(
                _EMAIL_SEARCH_DEBOUNCE,
                lambda: self._apply_email_list(list(self._threads_cache.values())))
        elif event.input.id == "tasks-search":
            if self._tasks_search_timer is not None:
                self._tasks_search_timer.stop()
            self._tasks_search_timer = self.set_timer(
                _TASKS_SEARCH_DEBOUNCE, self._refresh_task_list)
        elif event.input.id == "events-search":
            if self._events_search_timer is not None:
                self._events_search_timer.stop()
            self._events_search_timer = self.set_timer(
                _EVENTS_SEARCH_DEBOUNCE, self._refresh_event_list)
        elif event.input.id == "drive-search":
            if self._drive_search_timer is not None:
                self._drive_search_timer.stop()
            self._drive_search_timer = self.set_timer(
                _DRIVE_SEARCH_DEBOUNCE, self._refresh_drive_list)
        elif event.input.id == "news-search":
            if self._news_search_timer is not None:
                self._news_search_timer.stop()
            self._news_search_timer = self.set_timer(
                _NEWS_SEARCH_DEBOUNCE, self._refresh_news_list)

    def _hermes_ask_title(self) -> str:
        """Dashboard Hermes card title / HermesAskModal heading -- always
        names the currently configured AI provider (Settings -> AI Provider),
        not a hardcoded "Hermes", since ai_provider can be claude_code/
        opencode/gemini_cli too. See ask.display_name."""
        return f"ASK {ask.display_name(self.settings.ai_provider).upper()}  (type a question, Enter)"

    def _update_hermes_labels(self) -> None:
        """Keeps every always-mounted Hermes-Ask surface in sync with
        Settings -> AI Provider: the Dashboard card's title Label and its
        Input placeholder. Called at startup and whenever the provider
        changes (on_radio_set_changed's settings-ai-provider branch).
        HermesAskModal builds its own title/placeholder fresh every time
        it's opened, so it never goes stale and needs no update call here."""
        name = ask.display_name(self.settings.ai_provider)
        try:
            self.query_one("#hermes-pane-title", Label).update(self._hermes_ask_title())
        except Exception:
            pass
        try:
            self.query_one("#hermes-input", Input).placeholder = f"Ask {name} about your Google stuff…"
        except Exception:
            pass

    def _hermes_submit(self, event: Input.Submitted, log: RichLog | None = None) -> None:
        """log defaults to the Dashboard card's own #hermes-log; HermesAskModal
        passes its own #hermes-popup-log so the two share this one submit/
        LLM-calling path (_hermes_thread) instead of duplicating it."""
        q = event.value.strip()
        if not q:
            return
        event.input.value = ""
        if log is None:
            log = self.query_one("#hermes-log", RichLog)
            # First question reveals the Dashboard card's response box (it
            # starts hidden so an empty box doesn't squat the tab). Harmless
            # once already shown. The HermesAskModal passes its own always-
            # visible popup log, so this only affects the Dashboard card.
            log.remove_class("hidden")
        log.write(f"You: {q}")
        self.run_worker(lambda: self._hermes_thread(q, log), thread=True,
                        exclusive=False, group="hermes")

    def _hermes_thread(self, q: str, log: RichLog) -> None:
        """MUST run with thread=True. Every call in here is blocking network
        I/O — `_build_context` hits Gmail + Calendar, and `provider.ask` /
        `run_action` is an LLM round-trip that can take many seconds. As an
        `async def` worker (what this used to be) all of that ran on the event
        loop and locked the entire UI until the model answered.
        """
        provider = ask.get_provider(self.settings.ai_provider, nous_api_key=self.settings.nous_api_key,
                                     model=self.app_config.llm_model,
                                     nous_base_url=self.settings.nous_base_url)
        try:
            if needs_agent(q):
                self.call_from_thread(log.write, f"[running {provider.display_name} agent…]")
                ans = provider.run_action(q)
            else:
                ctx = self._build_context()
                sys_prompt = (
                    "You are an assistant answering questions using the user's live "
                    "Google Workspace data provided below. Be concise (couple of "
                    "sentences). If you need to take an action, say so plainly.\n\n"
                    "CONTEXT:\n" + ctx)
                ans = provider.ask(sys_prompt, q)
            self.call_from_thread(log.write, f"{provider.display_name}: {ans}")
        except Exception as e:
            self.call_from_thread(log.write, f"(error: {e})")

    def _build_context(self) -> str:
        parts = []
        try:
            threads, _ = gauth.list_threads(self.svc, max_results=10)
            parts.append("RECENT EMAIL THREADS:\n" + "\n".join(
                f"- {t['from']}: {t['subject']}" for t in threads[:8]))
        except Exception:
            pass
        try:
            events = gauth.list_events(self.svc, days=7)
            parts.append("UPCOMING EVENTS (7d):\n" + "\n".join(
                f"- {_fmt_date(e.get('start',{}).get('dateTime') or e.get('start',{}).get('date',''))} {e.get('summary','')}" for e in events[:8]))
        except Exception:
            pass
        return "\n\n".join(parts) or "(no context available)"

    # ---- browser tab (M2: Web / Gopher / Gemini / Search) ----

    def _bookmarks_render(self) -> None:
        """(Re)populate #browser-bookmarks (a DataTable) from
        self._bookmark_current_list — called on entering/leaving a folder and
        whenever "B" resets to root. Synchronous: DataTable.clear() is
        immediate, so the old awaited-worker + generation-counter (needed only
        for ListView.clear()'s AwaitRemove and the reused bm-<idx> keys) is
        gone. Single column; folder/protocol icon + label as a styled Text.

        Rows are built from a *sorted copy* (self._bookmark_sorted_list, per
        Settings.browser_bookmark_sort) so "bm-<i>" keys index into that copy,
        not the underlying (unsorted, folder-order-preserving) list — the
        dicts themselves are shared references either way, so
        _bookmark_open_selected/_delete still mutate the real settings data.
        """
        try:
            table = self.query_one("#browser-bookmarks", DataTable)
        except Exception:
            return
        self._bookmark_sorted_list = _sort_bookmark_entries(
            self._bookmark_current_list, self.settings.browser_bookmark_sort)
        rows: list[tuple[str, list]] = []
        if self._bookmark_parent_stack:
            rows.append(("bm-up", [Text("📂 .. (up)")]))
        for i, entry in enumerate(self._bookmark_sorted_list):
            if entry.get("type") == "folder":
                cell = Text(f"📂 {entry.get('label') or '(folder)'}")
            else:
                icon, color = _bookmark_scheme_style(entry.get("url", ""))
                label = entry.get("label") or entry.get("url", "")
                cell = Text(f"{icon} {label}", style=color)
            rows.append((f"bm-{i}", [cell]))
        list_tables.rebuild_flat_table(table, [("Bookmarks", None)], rows, flex_min=20)

    def _bookmark_open_selected(self) -> None:
        cid = list_tables.current_row_key(self.query_one("#browser-bookmarks", DataTable)) or ""
        if not cid:
            return
        if cid == "bm-up":
            if self._bookmark_parent_stack:
                self._bookmark_current_list = self._bookmark_parent_stack.pop()
            self._bookmarks_render()
            return
        if not cid.startswith("bm-"):
            return
        idx = int(cid.removeprefix("bm-"))
        if idx >= len(self._bookmark_sorted_list):
            return
        entry = self._bookmark_sorted_list[idx]
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if entry.get("type") == "folder":
            entry["last_opened_at"] = now
            save_settings(self.settings)
            self._bookmark_parent_stack.append(self._bookmark_current_list)
            self._bookmark_current_list = entry.get("children") or []
            self._bookmarks_render()
            return
        url = entry.get("url", "")
        if not url:
            return
        entry["last_opened_at"] = now
        save_settings(self.settings)
        try:
            self.query_one("#browser-url", Input).value = url
        except Exception:
            pass
        self._browser_navigate(url, push_history=True)

    def action_browser_cycle_sort(self) -> None:
        """S: cycle the Browser tab's bookmark sort order (Name -> Added ->
        Used -> Name), shown live in that tab's shortcut bar. Browser-tab-
        only, same guard as action_browser_home."""
        if self._main_tabs().active != "tab-browser":
            return
        cur = self.settings.browser_bookmark_sort
        idx = _BOOKMARK_SORT_CYCLE.index(cur) if cur in _BOOKMARK_SORT_CYCLE else 0
        self.settings.browser_bookmark_sort = _BOOKMARK_SORT_CYCLE[(idx + 1) % len(_BOOKMARK_SORT_CYCLE)]
        save_settings(self.settings)
        self._bookmarks_render()
        self._update_help_bar()

    def action_browser_delete_bookmark(self) -> None:
        """Delete: remove the highlighted bookmark (or folder, with its
        contents) from the Browser tab's bookmarks table. Browser-tab-only,
        same guard as action_browser_home. No confirmation dialog, matching
        the same convention _remove_selected_feed already uses for Settings
        -> News Feeds."""
        if self._main_tabs().active != "tab-browser":
            return
        cid = list_tables.current_row_key(self.query_one("#browser-bookmarks", DataTable)) or ""
        if not cid or cid == "bm-up" or not cid.startswith("bm-"):
            return
        idx = int(cid.removeprefix("bm-"))
        if idx >= len(self._bookmark_sorted_list):
            return
        entry = self._bookmark_sorted_list[idx]
        label = entry.get("label") or entry.get("url") or "(untitled)"
        # Remove by identity, not equality -- two bookmarks could otherwise
        # share an identical label+url and .remove() would drop the wrong one.
        self._bookmark_current_list[:] = [e for e in self._bookmark_current_list if e is not entry]
        save_settings(self.settings)
        self._bookmarks_render()
        self.notify(f"Removed bookmark: {label}")

    def _open_dashboard_link(self, item: dict | None) -> None:
        """Dashboard WORD OF THE DAY / PICTURE OF THE DAY cards' Enter
        action: switch to the Browser tab and navigate to item["link"], same
        two-step _goto_tab + _browser_navigate _bookmark_open_selected uses.
        A no-op if the card has nothing cached yet (item is None) or the
        fetch never got a link (unexpected but not fatal, so don't crash)."""
        link = (item or {}).get("link") if item else None
        if not link:
            return
        self._goto_tab("tab-browser")
        try:
            self.query_one("#browser-url", Input).value = link
        except Exception:
            pass
        self._browser_navigate(link, push_history=True)

    def _browser_submit(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw:
            return
        self._browser_navigate(raw, push_history=True)

    def _browser_toggle_focus(self) -> None:
        try:
            url_input = self.query_one("#browser-url", Input)
            doc_view = self.query_one("#browser-doc", DocumentView)
        except Exception:
            return
        if self.focused is url_input:
            doc_view.focus()
        else:
            url_input.focus()

    def _browser_capture_scroll(self) -> None:
        if not (0 <= self._browser_hist_pos < len(self._browser_history)):
            return
        try:
            doc_view = self.query_one("#browser-doc", DocumentView)
        except Exception:
            return
        self._browser_history[self._browser_hist_pos].scroll_y = doc_view.scroll_y

    def _browser_back(self) -> None:
        if self._browser_hist_pos <= 0:
            self.notify("No earlier page in this session", severity="warning")
            return
        self._browser_capture_scroll()
        self._browser_hist_pos -= 1
        self._browser_show_history_entry()

    def _browser_forward(self) -> None:
        if self._browser_hist_pos >= len(self._browser_history) - 1:
            self.notify("No later page in this session", severity="warning")
            return
        self._browser_capture_scroll()
        self._browser_hist_pos += 1
        self._browser_show_history_entry()

    def _browser_show_history_entry(self) -> None:
        entry = self._browser_history[self._browser_hist_pos]
        mode, _ = _classify_address(entry.url)
        try:
            self.query_one("#browser-url", Input).value = entry.url
            self.query_one("#browser-mode", Static).update(mode.upper())
            self.query_one("#browser-status", Static).update("")
            doc_view = self.query_one("#browser-doc", DocumentView)
            doc_view.document = entry.document
            doc_view.scroll_to(y=entry.scroll_y, animate=False)
            doc_view.focus()
        except Exception:
            pass

    def _browser_navigate(self, raw: str, *, push_history: bool) -> None:
        if push_history:
            self._browser_capture_scroll()
        mode, target = _classify_address(raw)
        if mode in ("ftp", "sftp"):
            # Remote-filesystem browsing lives in the Drive tab now (source
            # picker), not Browser — see _redirect_to_drive_source. No
            # worker spun up here; that function does its own fetch/connect
            # via the Drive tab's existing machinery.
            self._redirect_to_drive_source(mode, target)
            return
        display_url = target if mode != "search" else raw
        try:
            self.query_one("#browser-mode", Static).update(mode.upper())
            self.query_one("#browser-status", Static).update("Loading…")
        except Exception:
            pass
        self.run_worker(
            lambda: self._browser_fetch_thread(mode, target, display_url, push_history),
            thread=True, exclusive=True, group="browser-fetch",
        )

    def _redirect_to_drive_source(self, mode: str, url: str) -> None:
        """An ftp://sftp:// address typed in the Browser tab (or clicked
        from a bookmark) switches to the Drive tab instead of fetching
        inline. Reuses RemoteHostModal (pre-filled from the URL) rather than
        inventing session-scoped ephemeral picker state; its existing "save"
        Switch does double duty as "save this host." Note: unlike the old
        Browser-tab ftp:// flow, URL-embedded credentials (user:pass@host)
        no longer silently override a differing saved login for the same
        host — a rare-enough case (bookmarks essentially never embed a real
        password) that RemoteHostModal's pre-filled username + one extra
        keystroke for the password is an acceptable trade for not needing a
        second credential-precedence rule.
        """
        try:
            self.query_one("#browser-status", Static).update("")
        except Exception:
            pass
        if mode == "ftp":
            host, port, path, username, _ = drive_sources.parse_ftp_url(url)
            protocol = "ftp"
        else:
            host, port, path, username, _ = drive_sources.parse_sftp_url(url)
            protocol = "ssh"
        self._goto_tab("tab-drive")
        saved = remote_creds.get(self._encrypt_key, protocol, host, port)
        if saved is not None:
            saved_username, saved_password = saved
            self.call_after_refresh(self._drive_connect_new_source, protocol, host, port,
                                     saved_username, saved_password, path)
            return
        self.call_after_refresh(
            self.push_screen,
            RemoteHostModal(protocol=protocol, host=host, port=port, username=username),
            lambda result: self._on_remote_host_modal_result(result, path),
        )

    def _browser_fetch_dispatch(self, mode: str, target: str) -> render.Document:
        if mode == "http":
            return fetchers.fetch_http(target, ascii_mode=self.settings.ascii_mode)
        if mode == "gopher":
            return fetchers.fetch_gopher(target)
        if mode == "gemini":
            return fetchers.fetch_gemini(target, self._browser_tofu)
        return fetchers.run_search(target, self.settings, searxng_url_fallback=self.app_config.searxng_url)

    def _browser_fetch_thread(self, mode: str, target: str, display_url: str, push_history: bool) -> None:
        try:
            doc = self._browser_fetch_dispatch(mode, target)
        except fetchers.GeminiInputRequired as e:
            self.call_from_thread(self._browser_prompt_gemini_input, e, push_history)
            return
        except fetchers.GeminiRedirectConfirm as e:
            self.call_from_thread(self._browser_confirm_redirect, e, push_history)
            return
        except fetchers.BrowserFetchError as e:
            self.call_from_thread(self._browser_apply_error, str(e))
            return
        except Exception as e:
            self.call_from_thread(self._browser_apply_error, f"Unexpected error: {e}")
            return
        self.call_from_thread(self._browser_apply_document, doc, mode, display_url, push_history)

    def _browser_apply_error(self, message: str) -> None:
        try:
            self.query_one("#browser-status", Static).update("")
        except Exception:
            pass
        self.notify(message, severity="error")

    def _browser_apply_document(self, doc: render.Document, mode: str, display_url: str, push_history: bool) -> None:
        if push_history:
            current = (self._browser_history[self._browser_hist_pos]
                       if 0 <= self._browser_hist_pos < len(self._browser_history) else None)
            if current is not None and current.url == display_url:
                # Reload of the currently-displayed URL: update in place,
                # not a new history frame.
                current.document = doc
                current.scroll_y = 0.0
            else:
                # Standard back-stack semantics: navigating to a new URL
                # while not at the tail truncates everything past here.
                self._browser_history = self._browser_history[: self._browser_hist_pos + 1]
                self._browser_history.append(BrowserHistoryEntry(url=display_url, document=doc))
                self._browser_hist_pos = len(self._browser_history) - 1

        try:
            self.query_one("#browser-url", Input).value = display_url
            self.query_one("#browser-mode", Static).update(mode.upper())
            self.query_one("#browser-status", Static).update("")
            doc_view = self.query_one("#browser-doc", DocumentView)
            doc_view.document = doc
            doc_view.scroll_y = 0
            doc_view.focus()
        except Exception:
            pass

        try:
            self.query_one("#browser-bookmarks").add_class("hidden")
        except Exception:
            pass

    def _browser_prompt_gemini_input(self, exc: fetchers.GeminiInputRequired, push_history: bool) -> None:
        try:
            self.query_one("#browser-status", Static).update("")
        except Exception:
            pass
        self.push_screen(
            GeminiInputModal(exc.meta, sensitive=exc.sensitive),
            lambda result: self._browser_resume_gemini_input(exc, result, push_history),
        )

    def _browser_resume_gemini_input(self, exc: fetchers.GeminiInputRequired, result, push_history: bool) -> None:
        if result is None:
            self.notify("Cancelled", severity="warning")
            return
        # push_screen's callback fires BEFORE the modal is actually popped
        # (see AGENTS.md's push_screen-callback-timing NOTE) — defer so
        # _browser_navigate's query_one calls resolve against the base
        # screen, not the still-on-top modal.
        self.call_after_refresh(
            self._browser_navigate,
            f"{exc.url}?{urllib.parse.quote(result, safe='')}",
            push_history=push_history,
        )

    def _browser_confirm_redirect(self, exc: fetchers.GeminiRedirectConfirm, push_history: bool) -> None:
        try:
            self.query_one("#browser-status", Static).update("")
        except Exception:
            pass
        msg = f"Redirect to a different host:\n{exc.from_url}\n->\n{exc.to_url}\n\nFollow it?"
        self.push_screen(
            ConfirmModal(msg),
            lambda ok: self._browser_resume_redirect(exc, ok, push_history),
        )

    def _browser_resume_redirect(self, exc: fetchers.GeminiRedirectConfirm, ok: bool, push_history: bool) -> None:
        if not ok:
            self.notify("Redirect not followed", severity="warning")
            return
        self.call_after_refresh(self._browser_navigate, exc.to_url, push_history=push_history)

    def on_document_view_link_activated(self, event: DocumentView.LinkActivated) -> None:
        if event.link.url.startswith("mailto:"):
            self.notify("mailto: links aren't handled by the Browser tab yet", severity="warning")
            return
        active_screen = self.screen
        if isinstance(active_screen, (ThreadModal, NewsEntryModal)):
            # These are reading modals stacked over whatever tab was active
            # when opened (Mail for ThreadModal, News for NewsEntryModal) --
            # there's no in-place "navigate" for a modal like there is for
            # the Browser tab's own page. Least-surprising choice, mirroring
            # "open link in browser" from a mail/feed reader: close the
            # modal, switch to the Browser tab, and load the link there.
            # ThreadModal's per-message [N] numbering is independent per
            # message (see its docstring) -- this only ever sees the single
            # link actually activated in the DocumentView it came from, so
            # that constraint is naturally respected, not something this
            # handler needs to enforce itself.
            url = event.link.url
            active_screen.dismiss(None)
            # dismiss() pops the screen stack synchronously but the actual
            # DOM teardown/mount is deferred (see AGENTS.md's push_screen
            # callback-timing NOTE) -- defer one step so the tab switch and
            # _browser_navigate's query_one calls land cleanly on the base
            # screen, same pattern _browser_resume_gemini_input uses above.
            self.call_after_refresh(self._open_link_in_browser, url)
            return
        if self._main_tabs().active != "tab-browser":
            return
        self._browser_navigate(event.link.url, push_history=True)

    def _open_link_in_browser(self, url: str) -> None:
        self._goto_tab("tab-browser")
        self._browser_navigate(url, push_history=True)

    # ---- calendar tab ----
    def _on_dash_calendar(self) -> bool:
        """True when the Dashboard tab's CALENDAR card is the focused pane --
        action_cal_prev/next's second entry point (besides the Calendar tab
        itself), since the dashboard card shares the exact same
        _cal_year/_cal_month/_build_cal_month state."""
        return (self._main_tabs().active == "tab-dashboard"
                and self._dash_active == "dash-calendar")

    def action_cal_prev(self) -> None:
        on_dash_cal = self._on_dash_calendar()
        if self._main_tabs().active != "tab-calendar" and not on_dash_cal:
            return
        # The dashboard card has no week view of its own -- always move by
        # month there, regardless of whatever the Calendar tab's own
        # month/week toggle happens to be set to.
        if not on_dash_cal and self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week":
            self._cal_week_start -= dt.timedelta(days=7)
            self._build_cal_week()
        else:
            self._cal_month -= 1
            if self._cal_month == 0:
                self._cal_month = 12
                self._cal_year -= 1
            self._build_cal_month()

    def action_cal_next(self) -> None:
        on_dash_cal = self._on_dash_calendar()
        if self._main_tabs().active != "tab-calendar" and not on_dash_cal:
            return
        if not on_dash_cal and self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week":
            self._cal_week_start += dt.timedelta(days=7)
            self._build_cal_week()
        else:
            self._cal_month += 1
            if self._cal_month == 13:
                self._cal_month = 1
                self._cal_year += 1
            self._build_cal_month()

    def _day_cell_text(self, day: int, events: list[dict], *, is_today: bool = False,
                        max_events: int = 2, line_width: int = 18) -> str | Text:
        lines = [str(day)]
        colors: list[str | None] = [None]  # parallel list -- colors[i] styles lines[i]
        for e in events[:max_events]:
            start = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
            time_part = start.split()[-1] if " " in start else ""
            summary_width = max(1, line_width - len(time_part) - 1)
            lines.append(f"{time_part} {e.get('summary','')[:summary_width]}"[:line_width])
            colors.append(e.get("_color"))
        if len(events) > max_events:
            lines.append(f"+{len(events) - max_events} more")
            colors.append(None)
        # Total lines is always max_events + 2 (day number + up to max_events
        # event lines + one overflow/blank line) so every cell in the grid --
        # regardless of how many events it holds -- gets the same row height.
        while len(lines) < max_events + 2:
            lines.append("")
            colors.append(None)
        joined = "\n".join(lines)
        if not is_today and not any(colors):
            return joined
        # "reverse" swaps whatever fg/bg the cell already has rather than a
        # hardcoded color, so today's highlight still works under any theme.
        # stylize() (not the Text(..., style=...) constructor) is required
        # here so each span covers only its own line, not the whole
        # multi-line cell -- the constructor's style applies to all text.
        text = Text(joined)
        pos = 0
        for i, (line, color) in enumerate(zip(lines, colors)):
            if i == 0 and is_today:
                text.stylize("bold reverse", pos, pos + len(line))
            elif color:
                text.stylize(f"on {color}", pos, pos + len(line))
            pos += len(line) + 1  # +1 for the "\n" joining this line to the next
        return text

    def _fetch_cal_month(self, calendars: list[dict] | None = None) -> list[dict]:
        return gauth.month_events(self.svc, self._cal_year, self._cal_month, calendars=calendars)

    def _build_cal_month(self) -> None:
        self._apply_cal_month(self._fetch_cal_month())

    def _apply_cal_month(self, events: list[dict]) -> None:
        grid = self.query_one("#cal-grid")
        grid.clear(columns=True)
        # Overlay offline-created events that fall in THIS month. Range-filtered
        # explicitly because _event_day returns a day-of-month only (month-
        # agnostic), so a pending event in another month would otherwise land
        # on the same-numbered cell here.
        events = list(events) + [e for e in _pending_event_creates(self._pending_mutations)
                                 if _event_in_month(e, self._cal_year, self._cal_month)]
        by_day: dict[int, list[dict]] = {}
        for e in events:
            d = _event_day(e)
            if d:
                by_day.setdefault(d, []).append(e)
        self._cal_by_day = by_day
        # Dashboard CALENDAR card shows the SAME month/events -- driven from
        # this one call site (all four of its own callers already funnel
        # through _apply_cal_month) rather than a separate fetch.
        self._apply_dash_calendar(by_day)
        first = dt.date(self._cal_year, self._cal_month, 1)
        offset = first.weekday()
        if self._cal_month == 12:
            days_in_month = (dt.date(self._cal_year + 1, 1, 1) - first).days
        else:
            days_in_month = (dt.date(self._cal_year, self._cal_month + 1, 1) - first).days
        cells: list[int | None] = [None] * offset
        for d in range(1, days_in_month + 1):
            cells.append(d)
        while len(cells) % 7:
            cells.append(None)
        num_rows = len(cells) // 7

        # Stretch day-squares to fill the widget's actual size instead of
        # the old fixed 7-auto-width-columns/height=4 layout, which left a
        # visible gap on wide/tall terminals and cramped small ones. Falls
        # back to the old fixed sizing if the grid hasn't been laid out yet
        # (size not yet known -- e.g. building the grid before first paint).
        avail_width = grid.size.width
        col_width = avail_width // 7 if avail_width > 0 else None
        if col_width is not None:
            col_width = max(10, col_width)
        extra = (avail_width - col_width * 7) if col_width else 0
        for i, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            width = (col_width + 1) if col_width and i < extra else col_width
            grid.add_column(label, width=width)

        avail_height = grid.size.height - 1  # header row
        row_height = max(4, min(8, avail_height // num_rows)) if avail_height > 0 and num_rows else 4
        max_events = row_height - 2
        line_width = max(18, (col_width or 18) - 2)

        today = dt.date.today()
        this_month_is_current = (self._cal_year, self._cal_month) == (today.year, today.month)
        for i in range(0, len(cells), 7):
            row = [self._day_cell_text(d, by_day.get(d, []),
                                        is_today=this_month_is_current and d == today.day,
                                        max_events=max_events, line_width=line_width)
                   if d else "" for d in cells[i:i + 7]]
            grid.add_row(*row, height=row_height)

    def _apply_dash_calendar(self, by_day: dict[int, list[dict]]) -> None:
        """Dashboard CALENDAR card's compact month-view calendar: day numbers
        only (no per-day event text -- there's no room for that in a narrow-
        column card), a "•" marker after any day with 1+ events, and today's
        cell bold+reverse (same "reverse swaps whatever fg/bg the theme
        has" trick _day_cell_text uses, so it works under any theme). Fully
        navigable (cursor_type="cell" in compose()) -- arrow keys move the
        cursor, Enter/Space open the highlighted day's events
        (_dash_cal_cell_selected / _dash_cal_open_highlighted), "["/"]" change
        month (action_cal_prev/next, extended to this card) -- all sharing
        the exact same self._cal_year/_cal_month/_cal_by_day state as the
        Calendar tab's own #cal-grid, so moving either one moves both."""
        try:
            grid = self.query_one("#dash-calendar-grid", DataTable)
        except Exception:
            return
        grid.clear(columns=True)
        first = dt.date(self._cal_year, self._cal_month, 1)
        offset = first.weekday()
        if self._cal_month == 12:
            days_in_month = (dt.date(self._cal_year + 1, 1, 1) - first).days
        else:
            days_in_month = (dt.date(self._cal_year, self._cal_month + 1, 1) - first).days
        cells: list[int | None] = [None] * offset
        for d in range(1, days_in_month + 1):
            cells.append(d)
        while len(cells) % 7:
            cells.append(None)
        # Subtract DataTable's own per-column cell padding (both sides) before
        # dividing by 7, same correction list_tables.flex_fill_width applies --
        # without it, 7 declared column widths that exactly fill avail_width
        # actually render wider once padding is added, clipping the last
        # column or two off the edge of this already-narrow card (confirmed
        # visually: "Su" and some day numbers cut off before this fix).
        avail_width = grid.size.width
        pad_overhead = grid.cell_padding * 2 * 7
        col_width = max(3, (avail_width - pad_overhead) // 7) if avail_width > 0 else 4
        for label in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"):
            grid.add_column(label, width=col_width)
        today = dt.date.today()
        this_month_is_current = (self._cal_year, self._cal_month) == (today.year, today.month)
        for i in range(0, len(cells), 7):
            row = []
            for d in cells[i:i + 7]:
                if d is None:
                    row.append("")
                    continue
                cell = Text(f"{d}{'•' if by_day.get(d) else ''}")
                if this_month_is_current and d == today.day:
                    cell.stylize("bold reverse")
                row.append(cell)
            grid.add_row(*row)

    def _update_dash_clock(self) -> None:
        """CLOCK card, ticked once a second (on_mount's set_interval): big
        block-digit local time (Textual's built-in Digits widget -- no
        external dependency), then one plain-text "HH:MM:SS <zone>" line per
        Settings.clock_timezones entry underneath (default just ["UTC"], the
        common ham-radio "local + Zulu" pairing; empty shows local only).
        `.astimezone()` with no args attaches the system's local timezone
        (name + offset) to the naive `now()` result for the big digits.
        `ZoneInfo(name)` accepts "UTC" and any IANA zone (e.g.
        "America/Denver"); an unrecognized name someone mistyped in Settings
        is just skipped, never a crash."""
        try:
            digits = self.query_one("#dash-clock-digits", Digits)
            zones_widget = self.query_one("#dash-clock-zones", Static)
        except Exception:
            return
        local = dt.datetime.now().astimezone()
        digits.update(local.strftime("%H:%M:%S"))
        lines = []
        for tz_name in self.settings.clock_timezones:
            try:
                zoned = dt.datetime.now(ZoneInfo(tz_name))
            except (ZoneInfoNotFoundError, ValueError):
                continue
            lines.append(f"{zoned.strftime('%H:%M:%S')}  {tz_name}")
        zones_widget.update("\n".join(lines))

    def _fetch_cal_week(self, calendars: list[dict] | None = None) -> list[dict]:
        start = dt.datetime.combine(self._cal_week_start, dt.time.min).replace(tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=7)
        return gauth.events_between(self.svc, start, end, calendars=calendars)

    def _build_cal_week(self) -> None:
        self._apply_cal_week(self._fetch_cal_week())

    def _apply_cal_week(self, events: list[dict]) -> None:
        grid = self.query_one("#cal-week-grid")
        grid.clear(columns=True)
        grid.add_column("Hour")
        for label in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            grid.add_column(label)
        # Overlay offline-created events falling in the displayed week.
        events = list(events) + [e for e in _pending_event_creates(self._pending_mutations)
                                 if _event_in_week(e, self._cal_week_start)]
        cells: dict[tuple[int, int], list[dict]] = {}
        # All-day events (date-only start/end) and multi-day *timed* events
        # (start/end dateTimes on different calendar dates -- e.g. an
        # overnight or multi-day conference session) both go here instead of
        # into the hour grid: repeating a summary into every hour row it
        # spans reads fine for an ordinary same-day meeting, but not for
        # something that covers a whole day or several of them.
        allday_by_col: dict[int, list[dict]] = {}
        for e in events:
            s = e.get("start", {}).get("dateTime")
            en = e.get("end", {}).get("dateTime")
            if not s or not en:
                try:
                    sd = dt.date.fromisoformat(e.get("start", {}).get("date", ""))
                    end_raw = e.get("end", {}).get("date")
                    # Calendar's all-day end.date is EXCLUSIVE (matches the
                    # create-event convention already used elsewhere, e.g.
                    # CreateEventModal._try_create).
                    ed = dt.date.fromisoformat(end_raw) if end_raw else sd + dt.timedelta(days=1)
                except Exception:
                    continue
                for col in range(7):
                    day = self._cal_week_start + dt.timedelta(days=col)
                    if sd <= day < ed:
                        allday_by_col.setdefault(col, []).append(e)
                continue
            try:
                sdt = dt.datetime.fromisoformat(s)
                edt = dt.datetime.fromisoformat(en)
            except Exception:
                continue
            if sdt.date() != edt.date():
                for col in range(7):
                    day = self._cal_week_start + dt.timedelta(days=col)
                    if sdt.date() <= day <= edt.date():
                        allday_by_col.setdefault(col, []).append(e)
                continue
            col = (sdt.date() - self._cal_week_start).days
            if not (0 <= col < 7):
                continue
            start_hour = sdt.hour
            end_hour = edt.hour + (1 if edt.minute else 0)
            end_hour = max(end_hour, start_hour + 1)
            for hour in range(start_hour, min(end_hour, 24)):
                cells.setdefault((hour, col), []).append(e)
        self._cal_week_cells = cells
        self._cal_week_allday = allday_by_col

        # Dedicated all-day row above the hour grid (row 0 -- _cal_week_cell_selected
        # and _cal_week_matches below both account for this +1 offset against
        # the hour rows that follow).
        allday_row = ["All day"]
        for col in range(7):
            evs = allday_by_col.get(col, [])
            if not evs:
                allday_row.append("")
            elif len(evs) == 1:
                allday_row.append(_bg_cell(evs[0].get("summary", "")[:16], evs[0].get("_color")))
            else:
                allday_row.append(f"{len(evs)} events")
        grid.add_row(*allday_row)

        for hour in range(24):
            label = dt.time(hour).strftime("%I %p").lstrip("0")
            row = [label]
            for col in range(7):
                evs = cells.get((hour, col), [])
                if not evs:
                    row.append("")
                elif len(evs) == 1:
                    row.append(_bg_cell(evs[0].get("summary", "")[:16], evs[0].get("_color")))
                else:
                    row.append(f"{len(evs)} events")
            grid.add_row(*row)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        # Calendar grids use the default CELL cursor; the migrated list views
        # (ROADMAP P3) use cursor_type="row" and fire RowSelected instead
        # (on_data_table_row_selected below), so these two never overlap.
        table_id = event.data_table.id
        if table_id == "cal-grid":
            self._cal_month_cell_selected(event)
        elif table_id == "cal-week-grid":
            self._cal_week_cell_selected(event)
        elif table_id == "dash-calendar-grid":
            self._dash_cal_open_day(event.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter/click on a row of a migrated DataTable list. Dispatches by
        table id to the same detail action the old ListView.Selected did,
        looking the row up by its row key (was the ListItem id)."""
        table_id = event.data_table.id
        cid = event.row_key.value or ""
        if table_id == "contacts-list":
            if cid.startswith("ct-"):
                self._open_contact_detail(cid)
        elif table_id == "news-list":
            entry = self._news_by_cid.get(cid)
            if entry:
                self.push_screen(NewsEntryModal(entry))
        elif table_id == "event-list":
            if cid.startswith("e-"):
                self._open_event_by_id(cid[2:])
        elif table_id == "task-list":
            if cid.startswith("k-"):
                t = self._selected_task()
                if t:
                    # Reconciled task list so a pending offline subtask create
                    # shows up as a child inside the modal (and a pending delete
                    # is already filtered out).
                    self.push_screen(
                        TaskModal(self.svc, t,
                                  self._reconcile_tasks(getattr(self, "_tasks_cache", []))),
                        self._on_task_modal_result)
        elif table_id == "email-list":
            if cid == LOAD_MORE_EMAIL_ID:
                self.action_load_more_email()
            else:
                # Enter on a summary OR a revealed message row opens the thread.
                tid = self._thread_id_from_key(cid)
                if tid:
                    self._open_email_thread(tid)
        elif table_id in ("dash-mail-list", "dash-news-list"):
            self._open_dash_list_row(cid)
        elif table_id == "browser-bookmarks":
            self._bookmark_open_selected()
        elif table_id == "drive-list":
            if cid == LOAD_MORE_DRIVE_ID:
                self.action_load_more_drive()
            else:
                self._drive_open_selected(cid)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Row-cursor moved on a migrated list — the preview-on-highlight path
        (was on_list_view_highlighted). Drive only for now; the Mail preview
        pane joins when the Mail list migrates (Phase 4).

        Gated on the owning tab being active so a rebuild that resets the cursor
        (a resize reflow, an expand/collapse, or the startup cache paint of an
        unvisited tab) can't kick off a wasted preview fetch — unlike the old
        in-place _reflow, a DataTable rebuild re-emits RowHighlighted. Real
        cursor movement always happens with the tab active, so previews fire."""
        table_id = event.data_table.id
        key = event.row_key.value or ""
        if table_id == "drive-list" and self._main_tabs().active == "tab-drive":
            self._drive_on_highlight(key)
        elif table_id == "email-list" and self._main_tabs().active == "tab-mail":
            self._email_on_highlight(key)

    def _cal_month_cell_selected(self, event: DataTable.CellSelected) -> None:
        first_line = str(event.value).split("\n")[0]
        if not first_line.isdigit():
            return
        day = int(first_line)
        events = self._cal_by_day.get(day, [])
        if events:
            self.push_screen(DayEventsModal(day, self._cal_month, self._cal_year, events))

    def _dash_cal_open_day(self, value) -> None:
        """Dashboard CALENDAR card's Enter/Space: same DayEventsModal the
        Calendar tab's own month view opens (_cal_month_cell_selected), just
        parsing this card's plainer "d" / "d•" cell text (no per-day event
        summaries, so no multi-line split needed) instead of #cal-grid's
        multi-line cells."""
        day_text = str(value).rstrip("•")
        if not day_text.isdigit():
            return
        day = int(day_text)
        events = self._cal_by_day.get(day, [])
        if events:
            self.push_screen(DayEventsModal(day, self._cal_month, self._cal_year, events))

    def _dash_cal_open_highlighted(self) -> None:
        """Space's path to the same place Enter's CellSelected event
        (on_data_table_cell_selected -> _dash_cal_open_day) reaches -- Space
        doesn't generate one, so read the highlighted cell directly."""
        try:
            grid = self.query_one("#dash-calendar-grid", DataTable)
            value = grid.get_cell_at(grid.cursor_coordinate)
        except Exception:
            return
        self._dash_cal_open_day(value)

    def _cal_week_cell_selected(self, event: DataTable.CellSelected) -> None:
        col = event.coordinate.column - 1  # column 0 is the Hour label
        if col < 0:
            return
        row = event.coordinate.row
        if row == 0:  # the dedicated all-day row above the hour grid
            evs = self._cal_week_allday.get(col, [])
        else:
            evs = self._cal_week_cells.get((row - 1, col), [])
        if not evs:
            return
        if len(evs) == 1:
            self.push_screen(EventModal(evs[0]))
        else:
            day = (self._cal_week_start + dt.timedelta(days=col)).day
            self.push_screen(DayEventsModal(day, self._cal_week_start.month, self._cal_week_start.year, evs))

    # ---- calendar "/" jump-to-next-match (find-next over the date grid) ----
    # Enter-triggered, NOT a live filter: the Month/Week views are a fetched
    # date grid, not a ListView, so there's no list to hide non-matching rows
    # from — instead "/" moves the DataTable cursor to the next day (Month) or
    # hour-cell (Week) whose event(s) match, wrapping around, exactly like a
    # text editor's find-next. Mirrors ThreadModal._find (same
    # matches-equal-last-query → advance-pos idiom) and reuses _fuzzy_score so
    # the short-query / false-positive behaviour matches every other search in
    # the app. Searches only what the currently-active view has loaded
    # (_cal_by_day for Month, _cal_week_cells for Week) — a jump within what's
    # on screen, never a new fetch.
    def _event_matches(self, e: dict, query_lower: str, threshold: int) -> bool:
        target = f"{e.get('summary','')} {e.get('description','')}".strip().lower()
        return bool(target) and _fuzzy_score(query_lower, target, threshold) is not None

    def _cal_month_matches(self, query_lower: str, threshold: int) -> list[tuple[int, int]]:
        # Map each matching day to its (row, col) cell in #cal-grid. The month
        # grid lays day d at flat index offset+d-1 (offset = weekday of the 1st,
        # Mon=0), so row = idx // 7, col = idx % 7 — the same layout
        # _apply_cal_month builds. sorted() keeps reading order so a repeat-Enter
        # walks days top-to-bottom, left-to-right.
        first = dt.date(self._cal_year, self._cal_month, 1)
        offset = first.weekday()
        matches: list[tuple[int, int]] = []
        for day in sorted(self._cal_by_day):
            if any(self._event_matches(e, query_lower, threshold)
                   for e in self._cal_by_day[day]):
                idx = offset + day - 1
                matches.append((idx // 7, idx % 7))
        return matches

    def _cal_week_matches(self, query_lower: str, threshold: int) -> list[tuple[int, int]]:
        # #cal-week-grid column 0 is the Hour label, so a stored week-cell col
        # (0..6) maps to DataTable column col+1; row 0 is the dedicated
        # all-day row, so hour h lives at row h+1. A multi-hour event spans
        # several hour-cells, each a distinct jump target — deliberate, so
        # find-next steps through the block hour by hour.
        matches: list[tuple[int, int]] = []
        for col in sorted(self._cal_week_allday):
            if any(self._event_matches(e, query_lower, threshold)
                   for e in self._cal_week_allday[col]):
                matches.append((0, col + 1))
        for (hour, col) in sorted(self._cal_week_cells):
            if any(self._event_matches(e, query_lower, threshold)
                   for e in self._cal_week_cells[(hour, col)]):
                matches.append((hour + 1, col + 1))
        return matches

    def _cal_find(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        query_lower = query.lower()
        week = self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week"
        grid = self.query_one("#cal-week-grid" if week else "#cal-grid", DataTable)
        threshold = 75
        matches = (self._cal_week_matches if week else self._cal_month_matches)(
            query_lower, threshold)
        if not matches:
            self.notify("No matching events in this view", severity="warning")
            self._cal_search_matches = []
            self._cal_search_pos = -1
            return
        if matches == self._cal_search_matches:
            # Repeat-Enter of the same query → advance to the next hit (wraps).
            self._cal_search_pos = (self._cal_search_pos + 1) % len(matches)
        else:
            # New query → jump to the first hit at/after the current cursor
            # (wrapping to the first overall if none follow it), so "/" behaves
            # relative to where the user is looking, like find-next.
            self._cal_search_matches = matches
            cur = grid.cursor_coordinate
            here = (cur.row, cur.column)
            self._cal_search_pos = next(
                (i for i, c in enumerate(matches) if c > here), 0)
        row, col = matches[self._cal_search_pos]
        grid.move_cursor(row=row, column=col, scroll=True)
        grid.focus()
        if len(matches) > 1:
            self.notify(f"Match {self._cal_search_pos + 1} of {len(matches)}")

    # ---- new event (Calendar tab, and the Dashboard tab's Today pane) ----
    def action_new_event(self) -> None:
        tab = self._main_tabs().active
        if tab == "tab-calendar":
            default_date = self._cal_default_day()
        elif tab == "tab-dashboard" and self._dash_active == "dash-today":
            default_date = dt.date.today()
        else:
            return
        # No online gate: offline, the modal queues the create (with a temp id)
        # instead of blocking — see CreateEventModal._try_create.
        self.push_screen(CreateEventModal(self.svc, default_date), self._on_create_event_result)

    def _cal_default_day(self) -> dt.date:
        """Seed the create-event form's date field from what the Calendar
        tab currently has in view -- today if today falls inside the
        viewed month/week, else the first of the viewed month (Month) or
        that week's Monday (Week). There's no "currently highlighted day"
        concept in either grid outside of actually clicking a populated
        cell (which opens DayEventsModal/EventModal instead, not this
        modal), so this is the practical stand-in for DayEventsModal's
        "which day's events to show" — a day the grid is already showing,
        not an arbitrary one.
        """
        today = dt.date.today()
        if self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week":
            week_end = self._cal_week_start + dt.timedelta(days=6)
            if self._cal_week_start <= today <= week_end:
                return today
            return self._cal_week_start
        if self._cal_year == today.year and self._cal_month == today.month:
            return today
        return dt.date(self._cal_year, self._cal_month, 1)

    def _on_create_event_result(self, created) -> None:
        # Mirrors _on_task_modal_result's timing: CreateEventModal dismisses
        # with whether it actually created something; only then is it safe
        # to touch base-screen widgets (see AGENTS.md's push_screen callback-
        # timing NOTE and the query_one/screens NOTE -- this callback fires
        # before the modal is actually popped).
        if not created:
            return
        if created == "queued":
            # Offline: the create is already in the queue. Re-render from cache
            # + overlay — NO network refresh (which would just fail and toast a
            # spurious error). The Events pane and the active calendar grid both
            # pick up the pending event via their reconcile-at-render overlay.
            self._refresh_event_list()
            self._rebuild_active_cal_grid()
            return
        try:
            cal_active_week = self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week"
        except Exception:
            cal_active_week = False
        self.run_worker(lambda: self._after_create_event_thread(cal_active_week),
                         thread=True, exclusive=True)

    def _rebuild_active_cal_grid(self) -> None:
        """Re-apply the currently-active calendar grid (Month or Week) from
        cache, no network fetch — used after an offline create so the pending
        event appears in the grid too. _apply_cal_month/_apply_cal_week overlay
        the queue themselves, so this just replays the cached base data."""
        if not self._cache:
            return
        try:
            if self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week":
                key = self._cal_week_start.isoformat()
                self._apply_cal_week(self._cache.get("cal_week", key) or [])
            else:
                key = f"{self._cal_year:04d}-{self._cal_month:02d}"
                self._apply_cal_month(self._cache.get("cal_month", key) or [])
        except Exception:
            pass  # Calendar tab not mounted / grid not composed yet

    def _after_create_event_thread(self, cal_active_week: bool) -> None:
        """Runs on its own worker thread (see AGENTS.md's fetch/apply-split
        NOTE) -- refreshes both places a newly-created event needs to show
        up: the Dashboard tab's Events pane (via the same _refresh_all_thread
        path a task toggle or a sent message already uses) and the
        Calendar tab's currently-active grid (Month or Week), rebuilt via
        the existing _fetch_cal_month/_fetch_cal_week + _apply_cal_month/
        _apply_cal_week fetch/apply pair rather than a new refresh path.
        """
        self._refresh_all_thread()
        try:
            if cal_active_week:
                events = self._fetch_cal_week()
                self.call_from_thread(self._apply_cal_week, events)
            else:
                events = self._fetch_cal_month()
                self.call_from_thread(self._apply_cal_month, events)
        except Exception as e:
            self.call_from_thread(self.notify, f"Calendar refresh error: {e}", severity="error")

    # ---- drive tab ----
    def action_toggle_preview(self) -> None:
        """"p" — shared by Drive (file preview column) and Mail (Email
        preview pane), same dual-context-single-action pattern as "n"/
        action_new_event (Calendar tab vs Mail's Events pane). No-ops
        outside those two tabs.
        """
        tab = self._main_tabs().active
        if tab == "tab-drive":
            self._drive_preview_visible = not self._drive_preview_visible
            self._apply_drive_preview_visibility()
        elif tab == "tab-mail":
            self._toggle_email_preview()

    def action_download_drive_file(self) -> None:
        """"d" — download the highlighted Drive file to EXPORT_DIR
        (Documents/google-tui/), the same no-picker-widget destination
        Navigation's itinerary export uses. No-ops outside the Drive tab,
        on folders/the "load more" row, and while offline (get_media/
        export_media are live API calls -- no cached-bytes fallback exists
        the way text preview has one).
        """
        if self._main_tabs().active != "tab-drive":
            return
        cid = list_tables.current_row_key(self.query_one("#drive-list", DataTable)) or ""
        if not cid.startswith("d-") or cid == "d-up":
            return
        f = self._drive_items_by_cid.get(cid)
        if f is None:
            return
        if f["is_folder"]:
            self.notify("Select a file, not a folder, to download.", severity="warning")
            return
        # self._online specifically tracks GOOGLE reachability (AGENTS.md
        # §1a) -- only gate on it for the Google backend. FTP/SSH connect
        # live per-call regardless of Google's status; a real connection
        # failure there surfaces through _drive_download_thread's own
        # try/except instead.
        if self.drive_backend.source_key == "google" and not self._online:
            self.notify("Downloading needs a network connection.", severity="warning")
            return
        self.notify(f"Downloading {f['name']}…")
        self.run_worker(lambda: self._drive_download_thread(f),
                         thread=True, exclusive=True, group="drive-download")

    def _drive_download_thread(self, f: dict) -> None:
        try:
            name, data = self.drive_backend.download(f["id"])
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = EXPORT_DIR / name
            path.write_bytes(data)
        except Exception as e:
            self.call_from_thread(self.notify, f"Download failed: {e}", severity="error")
            return
        self.call_from_thread(self.notify, f"Downloaded to {path}")

    # ---- Drive-tab source picker (Google Drive / FTP / SSH) ----
    def _drive_switch_source(self, source_key: str) -> None:
        if source_key == "google":
            self._set_drive_backend(drive_sources.GoogleDriveSource(self.svc))
            return
        protocol, host, port = _split_source_key(source_key)
        saved = remote_creds.get(self._encrypt_key, protocol, host, port)
        if saved is None:
            self.notify("No saved credentials for this host.", severity="error")
            self._refresh_drive_source_select()  # snap the Select back
            return
        username, password = saved
        self._set_drive_backend(drive_sources.build_source(protocol, host, port, username, password))

    def _set_drive_backend(self, backend: "drive_sources.DriveBackend", *,
                            folder_id: str | None = None, path: str | None = None) -> None:
        old = self._drive_backend
        if old is not None and old is not backend:
            old.close()
        self._drive_backend = backend
        self._drive_folder_stack = []
        self._drive_preview_cache.clear()
        self._drive_load(folder_id if folder_id is not None else backend.root_id,
                          path if path is not None else backend.root_path)

    def _on_remote_host_modal_result(self, result, path: str | None = None) -> None:
        # push_screen's callback fires BEFORE the modal is actually popped
        # off the stack (§2's NOTE) — defer past it like every other
        # modal-result relay in this app. `path` threads through from
        # _redirect_to_drive_source (open at the URL's path, not the
        # source's root) — None from the Drive tab's own "+ Add remote
        # host…" flow, which always starts a new source at its root.
        if result is None:
            self.call_after_refresh(self._refresh_drive_source_select)
            return
        protocol, host, port, username, password, save = result
        if save:
            remote_creds.set_credentials(self._encrypt_key, protocol, host, port, username, password)
        self.call_after_refresh(self._drive_connect_new_source, protocol, host, port, username, password, path)

    def _drive_connect_new_source(self, protocol: str, host: str, port: int,
                                   username: str, password: str, path: str | None = None) -> None:
        backend = drive_sources.build_source(protocol, host, port, username, password)
        self._set_drive_backend(backend, folder_id=path, path=path)
        self._refresh_drive_source_select()

    def _refresh_drive_source_select(self) -> None:
        try:
            sel = self.query_one("#drive-source-select", Select)
        except Exception:
            return
        sel.set_options(_drive_source_select_options(self._encrypt_key, self.drive_backend))
        sel.value = self.drive_backend.source_key

    def _apply_drive_preview_visibility(self) -> None:
        try:
            preview_col = self.query_one("#drive-preview-col")
            list_col = self.query_one("#drive-list-col")
        except Exception:
            return
        hidden = not self._drive_preview_visible
        preview_col.set_class(hidden, "drive-preview-hidden")
        list_col.set_class(hidden, "drive-list-full")

    def _fetch_drive_files(self, folder_id: str) -> list[dict]:
        files, next_page_token = self.drive_backend.list_children(folder_id, None)
        # Plain attribute write from a (possibly) worker thread — same
        # pattern as self._email_next_page_token. A folder navigation
        # always fetches page 1 fresh, so this always reflects the
        # CURRENTLY OPEN folder's next page, never a stale one.
        self._drive_next_page_token = next_page_token
        return files

    def _apply_drive_files(self, files: list[dict], folder_id: str, path: str) -> None:
        if path == "/":
            self._drive_folder_stack = []
        self._drive_folder_id = folder_id
        self._drive_path = path
        # Single normalization choke point: every ingest path (startup cache
        # restore, live refresh, folder nav, load-more) funnels through here,
        # so guaranteeing is_folder once keeps a stale-schema cache from
        # crashing the render worker (see _with_is_folder).
        files = [_with_is_folder(f) for f in files]
        self._drive_files = files
        self.query_one("#drive-path").update(path)
        # DataTable.clear() is synchronous, so the old awaited-worker +
        # generation-counter dance (needed only because ListView.clear()
        # returned an AwaitRemove) is gone: rebuild inline. Both callers of
        # this (folder nav on the main thread, load-more via call_from_thread)
        # already run on the main thread, so a straight rebuild can't overlap.
        self._rebuild_drive_table()

    def _rebuild_drive_table(self) -> None:
        """(Re)paint #drive-list from self._drive_files through the current
        #drive-search filter, plus the '.. (up)' chrome row and the 'Load
        more' sentinel. Shared by folder nav and the debounced search."""
        try:
            query = self.query_one("#drive-search", Input).value
        except Exception:
            query = ""
        visible = (_fuzzy_filter_drive_files(self._drive_files, query)
                   if query.strip() else self._drive_files)
        self._drive_items_by_cid = {}
        # Seed the dedup set with the two chrome keys so a file whose slugified
        # id happens to collide with them still gets a unique row key.
        seen: set[str] = {"d-up", LOAD_MORE_DRIVE_ID}
        rows: list[tuple[str, list]] = []
        if self._drive_path != "/":
            rows.append(("d-up", ["📂 .. (up)"]))
        for f in visible:
            cid = _unique_id(_mk_id("d", f["id"]), seen)
            self._drive_items_by_cid[cid] = f
            icon = "📁" if f["is_folder"] else "📄"
            rows.append((cid, [f"{icon} {f.get('name', '')}"]))
        if self._drive_next_page_token and not query.strip():
            rows.append((LOAD_MORE_DRIVE_ID, ["↓ Load more files…"]))
        list_tables.rebuild_flat_table(
            self.query_one("#drive-list", DataTable), [("Name", None)], rows,
            flex_min=10, fallback_width=_DRIVE_ROW_DEFAULT_W)

    def _drive_load(self, folder_id: str = "root", path: str = "/") -> None:
        try:
            files = self._fetch_drive_files(folder_id)
        except Exception as ex:
            self.notify(f"Drive error: {ex}", severity="error")
            files = []
        self._apply_drive_files(files, folder_id, path)

    def action_load_more_drive(self) -> None:
        if not self._drive_next_page_token:
            self.notify("No more files to load.", severity="warning")
            return
        if not self._online:
            self.notify("Can't load more files while offline.", severity="warning")
            return
        self.run_worker(self._load_more_drive_thread, thread=True, exclusive=True, group="drive-loadmore")

    def _load_more_drive_thread(self) -> None:
        folder_id = self._drive_folder_id
        token = self._drive_next_page_token
        try:
            new_files, next_page_token = self.drive_backend.list_children(folder_id, token)
        except Exception as e:
            self.call_from_thread(self.notify, f"Load more error: {e}", severity="error")
            return
        self._drive_next_page_token = next_page_token
        if not new_files:
            self.call_from_thread(self.notify, "No more files.", severity="warning")
        # Dict merge, not list concat — same DuplicateIds hazard/fix as
        # action_load_more_email's merge by threadId. Written to cache in
        # this same deduplicated shape so "drive_listing" never persists a
        # literal duplicate row either.
        merged = {f["id"]: f for f in self._drive_files}
        for f in new_files:
            merged[f["id"]] = f
        combined = list(merged.values())
        # Only Google Drive's listing participates in the offline cache —
        # FTP/SSH sources are live-connect-on-demand only, same as Browser's
        # old ftp:// handling never had offline support either.
        if self._cache and self.drive_backend.source_key == "google":
            self._cache.put("drive_listing", folder_id, combined)
        self.call_from_thread(self._apply_drive_files, combined, folder_id, self._drive_path)

    def _refresh_drive_list(self) -> None:
        # Debounced keystroke path for #drive-search — filters
        # self._drive_files, the CURRENT folder's listing only (never the
        # whole Drive tree, never a fresh Drive call per keystroke). A straight
        # synchronous rebuild now (DataTable.clear() is immediate); it and the
        # folder-nav rebuild both run on the main thread, so no exclusive-group
        # / generation-counter guard is needed anymore.
        self._rebuild_drive_table()

    def _drive_open_selected(self, cid: str) -> None:
        if cid == "d-up":
            if self._drive_folder_stack:
                parent_id, parent_path = self._drive_folder_stack.pop()
            else:
                parent_id, parent_path = self.drive_backend.root_id, self.drive_backend.root_path
            self._drive_load(parent_id, parent_path)
            return
        if not cid.startswith("d-"):
            return
        f = self._drive_items_by_cid.get(cid)
        if f and f["is_folder"]:
            self._drive_folder_stack.append((self._drive_folder_id, self._drive_path))
            self._drive_load(f["id"], self._drive_path + f["name"] + "/")

    def _drive_on_highlight(self, cid: str) -> None:
        if not cid:
            return
        if cid == "d-up":
            self._drive_cancel_pending_preview()
            self.query_one("#drive-preview-meta").update("(parent folder)")
            self._drive_preview_reset()
            return
        if not cid.startswith("d-"):
            return
        f = self._drive_items_by_cid.get(cid)
        if not f:
            return
        # DEBOUNCE. This fires on every highlight change — i.e. on every arrow
        # keypress — and a preview is a metadata round-trip PLUS a full file
        # download. Firing one per row while the user holds Down means a
        # download per row, all of them but the last one wasted. Wait for the
        # cursor to settle first; the timer is restarted (not stacked) on each
        # keypress, so arrowing through 20 rows costs ONE preview, not 20.
        self._drive_cancel_pending_preview()
        self._drive_preview_timer = self.set_timer(
            _PREVIEW_DEBOUNCE, lambda: self._drive_start_preview(f))

    def _drive_cancel_pending_preview(self) -> None:
        if self._drive_preview_timer is not None:
            self._drive_preview_timer.stop()
            self._drive_preview_timer = None

    def _drive_preview_reset(self) -> None:
        """Clears both preview widgets and leaves the plain RichLog visible.

        Called whenever the preview pane is about to show a transient,
        non-file state (parent-folder row, "Loading…") rather than a real
        body — otherwise a Markdown ``DocumentView`` left rendered from the
        previously highlighted file would stay on screen underneath the new
        meta text until the fetch completes.
        """
        doc_widget = self.query_one("#drive-preview-doc", DocumentView)
        doc_widget.document = None
        doc_widget.add_class("hidden")
        text_widget = self.query_one("#drive-preview-text", RichLog)
        text_widget.remove_class("hidden")
        text_widget.clear()

    def _drive_start_preview(self, f: dict) -> None:
        self._drive_preview_timer = None
        self._drive_preview_gen += 1
        gen = self._drive_preview_gen
        fid = f["id"]
        # Session cache: re-highlighting a row already previewed this session
        # (very common — cursor moves down then back up) repaints from memory
        # with no network call at all.
        hit = self._drive_preview_cache.get(fid)
        if hit is not None:
            self._apply_drive_preview(gen, hit[0], hit[1], hit[2])
            return
        self._drive_preview_reset()
        self.query_one("#drive-preview-meta").update(f"Name: {f.get('name','')}\nLoading…")
        self.run_worker(lambda: self._drive_preview_thread(gen, f),
                        thread=True, exclusive=True, group="drive-preview")

    def _drive_preview_thread(self, gen: int, f: dict) -> None:
        """MUST run with thread=True — `get_file_metadata` is an HTTPS
        round-trip and `read_drive_text` downloads the file body. This used to
        be an `async def` worker, so BOTH ran on the event loop and every
        cursor move in the Drive list froze the whole app until Google
        answered. Pure fetch; all widget writes go through _apply_drive_preview
        on the main thread (AGENTS.md's fetch/apply-split NOTE).
        """
        info, body, is_markdown = self._drive_preview_fetch(f)
        if gen == self._drive_preview_gen:
            self._drive_preview_cache[f["id"]] = (info, body, is_markdown)
        self.call_from_thread(self._apply_drive_preview, gen, info, body, is_markdown)

    def _drive_preview_fetch(self, f: dict) -> tuple[str, str, bool]:
        """Blocking; returns the (meta_text, body_text, is_markdown) tuple
        to render. ``is_markdown`` is only ever True alongside a real
        successfully-fetched file body — every placeholder/error message
        path below returns False so it's never mistakenly run through
        ``parse_markdown``."""
        is_folder = f["is_folder"]
        fid = f["id"]
        backend = self.drive_backend
        # Cache categories are namespaced by source_key (not the key itself,
        # since an SSH id can contain ":") -- two different FTP/SSH hosts can
        # share a path like "/readme.txt", so a bare fid would collide once
        # more than one source exists. Same "category string carries the
        # discriminator" precedent as f"thread_summary:{label_id}" elsewhere
        # in this file.
        meta_category = f"drive_file_meta:{backend.source_key}"
        text_category = f"drive_file_text:{backend.source_key}"
        # The folder listing already told us this file's modifiedTime, for free.
        # Drive stamps a new one on every edit, so it revalidates the cache the
        # same way a thread's historyId does: if what we cached was stamped with
        # the same modifiedTime, it IS the current file and there is nothing to
        # download. Previously the cache was consulted ONLY when offline, so the
        # normal (online) path re-downloaded every file on every look.
        listed_mtime = str(f.get("modifiedTime") or "")
        cached_meta = self._cache.get(meta_category, fid) if self._cache else None
        fresh = bool(
            cached_meta and listed_mtime
            and str(cached_meta.get("modifiedTime") or "") == listed_mtime
        )

        if fresh:
            meta = cached_meta
        elif self._online:
            try:
                meta = backend.get_metadata(fid)
            except Exception as ex:
                return f"(metadata error: {ex})", "", False
            # Persistent per-file caching applies to every source (it's a
            # perf/revalidation optimization keyed off modifiedTime, not an
            # offline-availability guarantee) -- unlike the root LISTING
            # cache below, which stays Google-only since FTP/SSH sources are
            # live-connect-on-demand with no offline browsing story.
            if self._cache:
                self._cache.put(meta_category, fid, meta)
        else:
            meta = cached_meta
            if meta is None:
                return (f"Name: {f.get('name','')}\n(offline — never viewed online, "
                        "no cached details)", "(not available offline)", False)

        # owner/createdTime are Google-only concepts (FTP has no owner
        # notion; SSH's st_ctime is "inode change time," not creation time,
        # so it's not surfaced as one either) -- omit rather than show a
        # misleading value when a backend can't supply them.
        modified = _fmt_date(meta.get("modifiedTime") or "")
        kind = "Folder" if is_folder else meta.get("mimeType", "")
        info_lines = [
            f"Name:     {meta.get('name','')}",
            f"Type:     {kind}",
            f"Where:    {backend.label} — {self._drive_path}",
        ]
        if meta.get("owner"):
            info_lines.append(f"Owner:    {meta['owner']}")
        if meta.get("createdTime"):
            info_lines.append(f"Created:  {_fmt_date(meta['createdTime'])}")
        info_lines.append(f"Modified: {modified}")
        info = "\n".join(info_lines)
        if not self._online:
            info += "\n(offline — showing cached details)"
        if is_folder:
            return info, "(folder — press Enter to open)", False
        mime = meta.get("mimeType", "")
        if not _is_previewable(mime):
            if mime.startswith("image/"):
                return info, "(image file — no text preview)", False
            return info, "(binary file — no text preview)", False
        is_markdown = _is_markdown_file(meta.get("name") or f.get("name", ""), mime)

        # The body is the expensive part (a full file download). Reuse the
        # cached text whenever the modifiedTime says the file hasn't changed —
        # `fresh` was decided against the listing's modifiedTime above.
        cached_text = self._cache.get(text_category, fid) if self._cache else None
        if fresh and cached_text:
            return info, cached_text["text"][:8000], is_markdown

        if self._online:
            try:
                text = backend.read_preview_text(fid)
            except Exception as ex:
                return info, f"(preview error: {ex})", False
            if self._cache:
                self._cache.put(text_category, fid, {"text": text})
            return info, text[:8000], is_markdown

        if cached_text:
            return info, cached_text["text"][:8000], is_markdown
        return info, "(not available offline — open this file once while online to cache it)", False

    def _apply_drive_preview(self, gen: int, info: str, body: str, is_markdown: bool = False) -> None:
        if gen != self._drive_preview_gen:
            return  # cursor moved on; a newer preview owns the pane now
        self.query_one("#drive-preview-meta").update(info)
        doc_widget = self.query_one("#drive-preview-doc", DocumentView)
        text_widget = self.query_one("#drive-preview-text", RichLog)
        if is_markdown and body:
            text_widget.add_class("hidden")
            text_widget.clear()
            doc_widget.remove_class("hidden")
            doc_widget.document = render.parse_markdown(body)
            doc_widget.scroll_home(animate=False)
            return
        doc_widget.add_class("hidden")
        doc_widget.document = None
        text_widget.remove_class("hidden")
        text_widget.clear()
        if body:
            text_widget.write(body)
        text_widget.scroll_home(animate=False)

    # ---- news tab (P1 M3) ----
    def _fetch_news_data(self) -> list[dict]:
        """Pure data, thread-safe (see AGENTS.md's fetch/apply-split NOTE).

        Fetches every subscribed feed, wrapping each one in its OWN
        try/except so a single unreachable/broken feed URL doesn't take the
        whole News refresh (or any other feed) down with it — same defensive
        style as the calendar/drive/labels blocks in `_live_refresh_thread`.
        Deliberately does NOT feed into `ok`/`self._online` the way those
        blocks do: `self._online` specifically tracks GOOGLE reachability
        (see AGENTS.md §1a), and feed URLs are unrelated third-party sites —
        a dead RSS feed should not flip the header to "Offline".
        """
        entries: list[dict] = []
        for url in self.settings.feed_urls:
            try:
                entries.extend(fetchers.fetch_feed(url))
            except Exception as e:
                self.call_from_thread(self.notify, f"Feed error ({url}): {e}", severity="error")
        return entries

    def _write_news_cache(self, entries: list[dict]) -> None:
        if self._cache and entries:
            self._cache.put_many("feed_entry", {e["id"]: e for e in entries})

    def _apply_news_data(self, entries: list[dict]) -> None:
        # Both #news-list and the Dashboard #dash-news-list are DataTables now,
        # rebuilt synchronously (clear is immediate), so the old awaited-worker
        # + generation-counter (needed only for ListView.clear()'s AwaitRemove)
        # is gone — this runs inline on the main thread even though it can fire
        # more than once per session (cache load, live refresh, feed add/remove).
        # Entries are deduped by id so a story syndicated across feeds (or a
        # feed listed twice) neither doubles a headline nor collides two keys.
        self._news_entries_cache = _dedup_by_key(entries)
        try:
            query = self.query_one("#news-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_news_entries(entries, query) if query.strip() else entries
        self._populate_news_list(visible)
        self._dash_news_offset = 0
        self._populate_dash_news()

    # Dashboard NEWS card: number of headlines shown at once, and how often the
    # rotation interval advances the window.
    _DASH_NEWS_WINDOW = 5
    _DASH_NEWS_ROTATE_SECONDS = 12.0

    def _populate_dash_news(self) -> None:
        """Fill the Dashboard NEWS card with a window of the newest feed
        entries (self._news_entries_cache, newest-first), starting at
        self._dash_news_offset so _rotate_dash_news can cycle through more than
        the _DASH_NEWS_WINDOW visible at once. Row keys are `dn-<cid>` (distinct
        from the News tab's `n-` ids) mapped via self._dash_news_by_cid; Enter
        opens NewsEntryModal. DataTable (Feed/Headline columns) — rebuilds in
        place via list_tables (synchronous clear), so callers no longer need to
        pre-clear it the way the old reused-dn-id ListView did."""
        try:
            table = self.query_one("#dash-news-list", DataTable)
        except Exception:
            return
        entries = sorted(self._news_entries_cache,
                         key=lambda e: e.get("published") or "", reverse=True)
        self._dash_news_by_cid = {}
        cols = [("Feed", 16), ("Headline", None)]
        if not entries:
            list_tables.rebuild_flat_table(
                table, cols,
                [("dash-empty-news", ["", "No news yet — add feeds in Settings → News Feeds"])],
                flex_min=20)
            return
        window = entries[self._dash_news_offset:self._dash_news_offset + self._DASH_NEWS_WINDOW]
        seen: set = set()
        rows: list[tuple[str, list]] = []
        for e in window:
            cid = _unique_id(_mk_id("dn", e["id"]), seen)
            self._dash_news_by_cid[cid] = e
            rows.append((cid, [e.get("feed_title") or "", e.get("title") or "(untitled)"]))
        list_tables.rebuild_flat_table(table, cols, rows, flex_min=20)

    def _rotate_dash_news(self) -> None:
        """Advance the NEWS card window by _DASH_NEWS_WINDOW, wrapping at the
        end. Skipped while the card is focused (so a rotation never yanks the
        selection out from under an Enter) or when there's nothing to rotate
        (<= one window of entries). Synchronous now — _populate_dash_news
        rebuilds the DataTable in place. Driven by a set_interval started in
        on_mount."""
        entries = self._news_entries_cache
        if len(entries) <= self._DASH_NEWS_WINDOW or "dash-news" not in self._dash_enabled_ids:
            return
        if (self._dash_active == "dash-news"
                and self._main_tabs().active == "tab-dashboard"):
            return
        self._dash_news_offset += self._DASH_NEWS_WINDOW
        if self._dash_news_offset >= len(entries):
            self._dash_news_offset = 0
        self._populate_dash_news()

    def _populate_news_list(self, entries: list[dict]) -> None:
        # DataTable with real Date/Feed/Title columns. feed_title/title come
        # straight from someone else's RSS/Atom feed; list_tables feeds each as
        # a plain Rich Text (no markup parsing), so a literal "[Feed Title]"
        # can't be mis-read as a style tag — the reason the old Label row
        # needed markup=False.
        self._news_by_cid = {}
        seen: set = set()
        rows: list[tuple[str, list]] = []
        for e in sorted(entries, key=lambda e: e.get("published") or "", reverse=True):
            cid = _unique_id(_mk_id("n", e["id"]), seen)
            self._news_by_cid[cid] = e
            date = _fmt_date(e.get("published", "")).split(" ")[0][:_NEWS_DATE_W]
            rows.append((cid, [date, e.get("feed_title") or "", e.get("title") or "(untitled)"]))
        list_tables.rebuild_flat_table(
            self.query_one("#news-list", DataTable),
            [("Date", _NEWS_DATE_W), ("Feed", _NEWS_FEED_W), ("Title", None)], rows,
            flex_min=_NEWS_TITLE_MIN_W, fallback_width=_NEWS_ROW_DEFAULT_W)

    def _refresh_news_list(self) -> None:
        # Debounced keystroke path for #news-search — filters the already-
        # fetched self._news_entries_cache, never re-fetches any feed per
        # keystroke. Synchronous now (DataTable.clear() is immediate); the
        # news-list rebuild can't overlap the full news apply the way the old
        # ListView workers could, so no exclusive-group/gen guard is needed.
        try:
            query = self.query_one("#news-search", Input).value
        except Exception:
            query = ""
        entries = self._news_entries_cache
        visible = _fuzzy_filter_news_entries(entries, query) if query.strip() else entries
        self._populate_news_list(visible)

    def _fetch_and_merge_one_feed(self, url: str) -> None:
        """Background fetch for a single newly-added feed (Settings tab),
        so the News list isn't empty for that feed until the next full
        Ctrl+R/live refresh. Runs on a worker thread (`thread=True`), so —
        per the fetch/apply split — it only touches `self._cache` (lock-
        guarded, thread-safe) directly; the actual widget repopulation is
        handed back to the main thread via `call_from_thread`.
        """
        try:
            new_entries = fetchers.fetch_feed(url)
        except Exception as e:
            self.call_from_thread(self.notify, f"Feed error ({url}): {e}", severity="error")
            return
        if self._cache:
            self._cache.put_many("feed_entry", {e["id"]: e for e in new_entries})
            all_entries = list(self._cache.get_all("feed_entry").values())
        else:
            all_entries = new_entries
        self.call_from_thread(self._apply_news_data, all_entries)

    def _refresh_feed_list(self) -> None:
        """(Re)populate the Settings → News Feeds DataTable from
        Settings.feed_urls. Row key -> url is kept in _feeds_by_cid for the
        remove button (was a `.feed_url` attribute on the ListItem). Feed URLs
        are arbitrary external text, fed as plain Rich Text — no markup parse,
        so a "[" in a URL renders literally (the reason the old row used
        markup=False)."""
        try:
            table = self.query_one("#settings-feed-list", DataTable)
        except Exception:
            return
        self._feeds_by_cid = {}
        seen: set = set()
        rows: list[tuple[str, list]] = []
        for url in self.settings.feed_urls:
            cid = _unique_id(_mk_id("sf", url), seen)
            self._feeds_by_cid[cid] = url
            rows.append((cid, [url]))
        list_tables.rebuild_flat_table(table, [("Feed URL", None)], rows, flex_min=20)

    def _subscribe_feed(self, url: str) -> None:
        """Add `url` to `Settings.feed_urls`, refresh the Settings-tab list,
        and kick a background merge fetch. Shared by the manual add-by-URL
        flow (`_add_feed_url`) and the popular-feeds picker
        (`_on_feed_pick_result`) so both go through one code path."""
        if url in self.settings.feed_urls:
            return
        self.settings.feed_urls.append(url)
        save_settings(self.settings)
        self._refresh_feed_list()
        self.run_worker(
            lambda: self._fetch_and_merge_one_feed(url),
            thread=True, exclusive=False, group="news-fetch-one",
        )

    def _unsubscribe_feed(self, url: str) -> None:
        """Remove `url` from `Settings.feed_urls`, refresh the Settings-tab
        list, and purge its cached entries from the News tab/Dashboard card.
        Shared by `_remove_selected_feed` and the popular-feeds picker."""
        if url not in self.settings.feed_urls:
            return
        self.settings.feed_urls.remove(url)
        save_settings(self.settings)
        self._refresh_feed_list()
        if self._cache:
            remaining = [e for e in self._cache.get_all("feed_entry").values() if e.get("feed_url") != url]
            self._apply_news_data(remaining)

    def _add_feed_url(self) -> None:
        inp = self.query_one("#settings-feed-url", Input)
        url = inp.value.strip()
        if not url:
            return
        if url in self.settings.feed_urls:
            self.notify("Already subscribed to that feed", severity="warning")
            return
        self._subscribe_feed(url)
        inp.value = ""
        self.notify(f"Added feed: {url}")

    def _remove_selected_feed(self) -> None:
        cid = list_tables.current_row_key(self.query_one("#settings-feed-list", DataTable))
        url = self._feeds_by_cid.get(cid or "")
        if url is None:
            self.notify("Select a feed to remove first", severity="warning")
            return
        if url not in self.settings.feed_urls:
            return
        self._unsubscribe_feed(url)
        self.notify(f"Removed feed: {url}")

    def _open_feed_picker(self) -> None:
        applied = frozenset(self.settings.feed_urls)
        self.push_screen(FeedPickerModal(applied_urls=applied), self._on_feed_pick_result)

    def _on_feed_pick_result(self, urls: list[str] | None) -> None:
        if urls is None:
            return
        picked = set(urls)
        curated_urls = {f["url"] for feeds in POPULAR_FEEDS.values() for f in feeds}
        added = 0
        removed = 0
        for url in curated_urls:
            subscribed = url in self.settings.feed_urls
            wanted = url in picked
            if wanted and not subscribed:
                self._subscribe_feed(url)
                added += 1
            elif subscribed and not wanted:
                self._unsubscribe_feed(url)
                removed += 1
        if added or removed:
            self.notify(f"Feeds updated: +{added} / -{removed}")

    # ---- contacts tab (P1 M5) ----
    def _fetch_contacts_data(self) -> list[dict]:
        """Pure data, thread-safe (AGENTS.md fetch/apply split) — called from
        `_contacts_fetch_thread` on a worker thread."""
        return gauth.list_contacts(self.svc)

    def _write_contacts_cache(self, contacts: list[dict]) -> None:
        if self._cache and contacts:
            self._cache.put_many("contact", {c["resource_name"]: c for c in contacts})

    def _contacts_fetch_thread(self) -> None:
        """Lazy contacts fetch — kicked off once, the first time the
        Contacts tab is activated (see on_tabbed_content_tab_activated), not
        on every startup/Ctrl+R alongside mail/calendar/drive/news, since
        contacts change far less often than those. Runs on a real OS thread
        (googleapiclient calls are blocking), same as every other live-data
        fetch in this app.

        This is the call that WILL fail against a token minted before
        `contacts.readonly` was added to the requested scopes (see
        SETUP.md §7) — caught here and surfaced as an actionable notify
        instead of crashing the tab or the app.
        """
        try:
            contacts = self._fetch_contacts_data()
        except Exception as e:
            # Distinguish "token itself is broken" (point at Settings, don't
            # show stale/blank rows) from a one-off/transient API error
            # (leave whatever's on screen alone).
            if not self._google_creds_ok():
                self.call_from_thread(self._apply_contacts_auth_broken)
            else:
                self.call_from_thread(
                    self.notify,
                    f"Contacts unavailable: {e} — re-run the OAuth flow with the "
                    f"contacts.readonly scope (see SETUP.md §7), then restart.",
                    severity="error",
                )
            return
        self._write_contacts_cache(contacts)
        self.call_from_thread(self._apply_contacts_data, contacts)

    def _apply_contacts_auth_broken(self) -> None:
        self._contacts_auth_broken = True
        self.notify("Google token missing or expired — reconnect in Settings to load contacts.",
                     severity="error")
        self._refresh_contacts_list()

    def _apply_contacts_data(self, contacts: list[dict]) -> None:
        """Main-thread widget mutation half of the fetch/apply split.
        Stashes the full list (backs ComposeModal's To-field autocomplete,
        which reads self.app._contacts_cache directly) and re-renders the
        list through the current search filter."""
        self._contacts_auth_broken = False
        self._contacts_cache = contacts
        self._refresh_contacts_list()

    def _refresh_contacts_list(self) -> None:
        """Rebuild #contacts-list (a DataTable) through the current search
        filter. Synchronous: DataTable.clear() is immediate (no ListView
        AwaitRemove), so the old generation-counter worker is gone — this can
        run on the main thread on cache load, live fetch, and every debounced
        search keystroke without overlapping rebuilds."""
        try:
            query = self.query_one("#contacts-search", Input).value
        except Exception:
            query = ""
        table = self.query_one("#contacts-list", DataTable)
        self._contacts_by_cid = {}
        if self._contacts_auth_broken:
            list_tables.rebuild_flat_table(table, [("", None)], [
                ("ct-auth-broken", [
                    "Not connected — Google token is missing or expired. "
                    "Reconnect from Settings → General to load contacts."])])
            return
        seen: set[str] = set()
        rows = []
        for c in _fuzzy_filter_contacts(self._contacts_cache, query):
            name = (c.get("name") or "").strip()
            addr = (c.get("email") or "").strip()
            if not name and not addr:
                continue  # no usable info at all — not worth a row
            cid = _unique_id(_mk_id("ct", c.get("resource_name", "")), seen)
            self._contacts_by_cid[cid] = c
            rows.append((cid, [name, addr]))
        list_tables.rebuild_flat_table(
            table, [("Name", _CONTACT_NAME_W), ("Email", None)], rows,
            flex_min=_CONTACT_ADDR_MIN_W, fallback_width=_CONTACT_ROW_DEFAULT_W)

    def _open_contact_detail(self, cid: str) -> None:
        c = self._contacts_by_cid.get(cid)
        if c:
            self.push_screen(ContactModal(c), self._on_contact_modal_result)

    def _on_contact_modal_result(self, result) -> None:
        if isinstance(result, tuple) and result and result[0] == "compose":
            _, email = result
            self.call_after_refresh(self._open_compose_new, email)

    def _open_compose_new(self, to_email: str = "") -> None:
        self.push_screen(ComposeModal(self.svc, None, mode="new", to=to_email), self._on_compose_result)

    # ---- navigation tab (P1 M6) ----
    def _nav_go(self) -> None:
        origin = self.query_one("#nav-origin", Input).value.strip()
        destination = self.query_one("#nav-destination", Input).value.strip()
        if not origin or not destination:
            self.notify("Enter both an origin and a destination.", severity="warning")
            return
        if not self.settings.routes_api_key:
            self.notify("Set a Routes API key in Settings -> Navigation first.", severity="warning")
            return
        self.query_one("#nav-status", Static).update("Computing route...")
        self.run_worker(lambda: self._nav_fetch_thread(origin, destination),
                         thread=True, exclusive=True, group="nav-fetch")

    def _nav_fetch_thread(self, origin: str, destination: str) -> None:
        try:
            result = fetchers.compute_route(origin, destination, self.settings.routes_api_key,
                                             ascii_mode=self.settings.ascii_mode)
        except fetchers.BrowserFetchError as e:
            self.call_from_thread(self._nav_apply_error, str(e))
            return
        except Exception as e:
            self.call_from_thread(self._nav_apply_error, f"Unexpected error: {e}")
            return
        self.call_from_thread(self._nav_apply_result, result)

    def _nav_apply_error(self, message: str) -> None:
        self.query_one("#nav-status", Static).update("")
        self.notify(message, severity="error")

    def _nav_apply_result(self, result: "fetchers.RouteResult") -> None:
        self._nav_last_result = result
        self.query_one("#nav-status", Static).update("")
        self.query_one("#nav-summary", Static).update(
            f"{result.origin} -> {result.destination}   Total: {result.distance_text} - {result.duration_text}"
        )
        log = self.query_one("#nav-log", RichLog)
        log.clear()
        for i, step in enumerate(result.steps, start=1):
            log.write(f"{i}. {step.instruction}  ({step.distance_text}, {step.duration_text})", scroll_end=False)

    def _nav_export(self) -> None:
        if self._nav_last_result is None:
            self.notify("Compute a route first.", severity="warning")
            return
        try:
            path = _export_itinerary(self._nav_last_result)
            self.notify(f"Exported to {path}")
        except Exception as e:
            self.notify(f"Export failed: {e}", severity="error")

    # ---- settings tab ----
    # ---- Google re-authorization (in-app OAuth flow, replaces the old
    # copy-a-script-and-run-it-yourself process). GoogleReauthModal owns the
    # whole interactive URL-then-paste-code flow (needs no browser/display
    # on THIS machine — see its docstring); this just pushes it and reacts
    # to the result. ----
    def _start_google_reauth(self) -> None:
        self.push_screen(GoogleReauthModal(), self._on_google_reauth_modal_result)

    def _on_google_reauth_modal_result(self, result) -> None:
        if result != "reauthorized":
            return
        # push_screen's callback fires BEFORE the modal is actually popped
        # (§2's NOTE) — defer past it, same as every other modal-result
        # relay in this app.
        self.call_after_refresh(self._apply_google_reauth_success)

    def _apply_google_reauth_success(self) -> None:
        self.notify("Google re-authorized.")
        # Rebuild self.svc with the fresh credentials and pull live data
        # immediately — unlike the encrypt-at-rest settings, re-auth doesn't
        # touch the cache/encryption key, so there's no reason to make the
        # user restart the app to see it take effect.
        try:
            self.svc = gauth.services()
        except Exception as e:
            self.notify(f"Re-authorized, but couldn't rebuild the Google connection: {e}", severity="error")
            return
        self.run_worker(self._live_refresh_thread, thread=True, exclusive=True)
        # If this fired from the forced first-run onboarding modal (still
        # under GoogleReauthModal on the stack), close it out through the
        # same path Retry uses — _on_onboarding_result defers to
        # _continue_startup via call_after_refresh.
        if isinstance(self.screen, OnboardingWizardModal):
            self.screen.dismiss("resolved")

    def _refresh_remote_hosts_list(self) -> None:
        try:
            table = self.query_one("#settings-remote-hosts-list", DataTable)
        except Exception:
            return
        # Row key -> (protocol, host, port), read back by the remove button
        # (was a `.remote_host` attribute stashed on the ListItem): a hostname
        # isn't reversible from a slugified key either (dots aren't valid).
        self._remote_hosts_by_cid = {}
        hosts = remote_creds.list_hosts(self._encrypt_key)
        if not hosts:
            list_tables.rebuild_flat_table(table, [("Saved hosts", None)],
                                           [("rh-empty", ["(no saved remote hosts)"])], flex_min=20)
            return
        seen: set = set()
        rows: list[tuple[str, list]] = []
        for protocol, host, port in hosts:
            display = f"{protocol}://{host}:{port}"
            cid = _unique_id(_mk_id("remotehost", display), seen)
            self._remote_hosts_by_cid[cid] = (protocol, host, port)
            rows.append((cid, [display]))
        list_tables.rebuild_flat_table(table, [("Saved hosts", None)], rows, flex_min=20)

    def _update_settings_cache_info(self) -> None:
        """Disk usage readout under the cache buttons: total on disk, then the
        breakdown by what's actually using it, biggest first — the point being
        that someone tight on space can see it's (say) Drive file contents, not
        mail, and prune accordingly."""
        human = cache_mod.human_bytes
        if not self._cache:
            info = f"{cache_mod.CACHE_DB_PATH}  (not created yet)"
        else:
            try:
                st = self._cache.stats()
            except Exception as e:
                self._safe_update("#settings-cache-info", f"(couldn't read cache size: {e})")
                return
            # Merge categories that share a display name before ranking them:
            # thread summaries are stored per-label ("thread_summary:INBOX",
            # "thread_summary:ALL", ...), and listing "Email (list)" three times
            # tells the reader nothing useful about where their disk went.
            merged: dict[str, dict] = {}
            for c in st["categories"]:
                m = merged.setdefault(
                    _cache_category_label(c["category"]), {"bytes": 0, "rows": 0})
                m["bytes"] += c["bytes"]
                m["rows"] += c["rows"]
            ranked = sorted(merged.items(), key=lambda kv: kv[1]["bytes"], reverse=True)

            lines = [f"{cache_mod.CACHE_DB_PATH}",
                     f"On disk: {human(st['db_bytes'])}   ({st['rows']:,} {_plural(st['rows'], 'item')})"]
            for name, m in ranked[:8]:
                if not m["rows"]:
                    continue
                lines.append(f"   {name:<24} {human(m['bytes']):>9}   "
                             f"{m['rows']:,} {_plural(m['rows'], 'item')}")
            if not st["rows"]:
                lines.append("   (empty)")
            info = "\n".join(lines)
        self._safe_update("#settings-cache-info", info)

    def _safe_update(self, selector: str, text: str) -> None:
        try:
            self.query_one(selector).update(text)
        except Exception:
            pass

    def _prune_cache(self, announce: bool = True) -> None:
        """Enforce the configured retention window / size cap.

        Runs on a worker thread: the DELETEs are indexed and cheap, but the
        VACUUM that follows them rewrites the database file, and on a large
        cache that's long enough to stutter the UI if done on the event loop.
        No-ops when no limit is set, so the default config pays nothing.
        """
        if not self._cache:
            return
        if not self.settings.cache_retention_days and not self.settings.cache_max_mb:
            if announce:
                self.notify("No cache limits set — nothing to apply.")
            self._update_settings_cache_info()
            return
        self.run_worker(lambda: self._prune_cache_thread(announce),
                        thread=True, exclusive=True, group="cache-prune")

    def _prune_cache_thread(self, announce: bool) -> None:
        cache = self._cache
        if cache is None:
            return
        before = cache.db_size()
        try:
            removed = cache.prune(
                max_age_days=self.settings.cache_retention_days,
                max_bytes=self.settings.cache_max_mb * 1024 * 1024,
            )
        except Exception as e:
            if announce:
                self.call_from_thread(self.notify, f"Cache prune failed: {e}", severity="error")
            return
        freed = max(0, before - cache.db_size())
        self.call_from_thread(self._apply_prune_result, removed, freed, announce)

    def _apply_prune_result(self, removed: dict, freed: int, announce: bool) -> None:
        self._update_settings_cache_info()
        n = removed["by_age"] + removed["by_size"]
        if not announce:
            return
        if n:
            self.notify(f"Removed {n:,} cached item(s), freed {cache_mod.human_bytes(freed)}.")
        else:
            self.notify("Cache is already within your limits.")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "settings-show-sender-address-switch":
            self.settings.show_sender_address = event.value
            save_settings(self.settings)
            self._apply_email_list(list(self._threads_cache.values()))
            return
        if event.switch.id == "settings-quote-on-reply-switch":
            self.settings.quote_on_reply = event.value
            save_settings(self.settings)
            return
        if event.switch.id == "settings-email-preview-default-switch":
            # Persisted DEFAULT only -- like the other switches here, this
            # doesn't touch the CURRENT session's self._email_preview_visible
            # (that's "p"/action_toggle_preview's job, ephemeral per session,
            # same as Drive's toggle). Next launch reads the new default.
            self.settings.email_preview_default_visible = event.value
            save_settings(self.settings)
            return
        if event.switch.id == "settings-update-check-switch":
            self.settings.check_for_updates = event.value
            save_settings(self.settings)
            return
        if event.switch.id == "settings-ascii-mode-switch":
            # Live, not restart-required — same precedent as
            # settings-show-sender-address-switch above: this only changes
            # how already-loaded data/UI chrome is rendered, no cache/key
            # implications like encrypt-at-rest below, so there's no reason
            # to make the user restart for it.
            self.settings.ascii_mode = event.value
            save_settings(self.settings)
            self._apply_ascii_mode()
            self.notify(f"ASCII-safe mode {'on' if event.value else 'off'}.")
            return
        if event.switch.id != "settings-encrypt-switch":
            return
        try:
            self.query_one("#settings-key-method").set_class(not event.value, "hidden")
        except Exception:
            pass
        if not event.value:
            self.settings.encrypt_at_rest = False
            save_settings(self.settings)
            if self._cache:
                self._cache.clear_all()
                self._cache.rekey(None)
            self._encrypt_key = None
            self.notify("Encryption disabled. Local cache cleared; it will repopulate unencrypted.")
            self._update_settings_cache_info()
            return
        if self.settings.key_method == "passphrase":
            self.push_screen(UnlockModal(self.settings, mode="create"), self._on_settings_passphrase_result)
        else:
            key = read_or_create_keyfile()
            self.settings.encrypt_at_rest = True
            self.settings.key_method = "keyfile"
            save_settings(self.settings)
            if self._cache:
                self._cache.clear_all()
                self._cache.rekey(key)
            self._encrypt_key = key
            self.notify("Encryption enabled (local key file). Cache cleared and now encrypted.")
            self._update_settings_cache_info()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "settings-ai-provider":
            pid = event.pressed.id.removeprefix("rb-provider-")
            if pid == self.settings.ai_provider:
                return
            self.settings.ai_provider = pid
            save_settings(self.settings)
            self._update_hermes_labels()  # Dashboard card title/placeholder + any open HermesAskModal is unaffected (built fresh per-open)
            label = next(l for l, v in ask.PROVIDER_CHOICES if v == pid)
            self.notify(f"AI provider set to {label}")
            return
        if event.radio_set.id == "settings-search-provider":
            provider_map = {
                "rb-search-google": ("google", "Google"),
                "rb-search-duckduckgo": ("duckduckgo", "DuckDuckGo"),
                "rb-search-searxng": ("searxng", "SearXNG"),
            }
            provider, label = provider_map.get(event.pressed.id, ("google", "Google"))
            self.settings.search_provider = provider
            save_settings(self.settings)
            try:
                self.query_one("#settings-google-group").set_class(provider != "google", "hidden")
                self.query_one("#settings-searxng-group").set_class(provider != "searxng", "hidden")
            except Exception:
                pass
            self.notify(f"Search provider set to {label}")
            return
        if event.radio_set.id != "settings-key-method":
            return
        if not self.settings.encrypt_at_rest:
            return  # spurious event during initial mount, before the switch is on
        method = "passphrase" if event.pressed.id == "rb-passphrase" else "keyfile"
        if method == self.settings.key_method:
            return
        if method == "passphrase":
            self.push_screen(UnlockModal(self.settings, mode="create"), self._on_settings_passphrase_result)
        else:
            key = read_or_create_keyfile()
            self.settings.key_method = "keyfile"
            self.settings.kdf_salt = None
            self.settings.canary = None
            save_settings(self.settings)
            if self._cache:
                self._cache.clear_all()
                self._cache.rekey(key)
            self._encrypt_key = key
            self.notify("Switched to local key file. Cache cleared and rekeyed.")
            self._update_settings_cache_info()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        """Settings -> Dashboard's card checklist, live-applied (see
        _apply_dashboard_panes_enabled). SelectionList.SelectedChanged carries
        no per-item info -- event.selection_list.selected is always the FULL
        current selection, so this just re-reads and re-applies it wholesale.
        """
        if event.selection_list.id != "settings-dashboard-panes":
            return
        selected = list(event.selection_list.selected)
        if not selected:
            # An empty Dashboard has nothing for Tab/Alt-arrows to land on.
            # re-selecting "hermes" fires this handler again (non-empty this
            # time), so it converges in two dispatches, not a loop.
            self.notify("At least one Dashboard card must stay enabled — keeping Hermes Ask on.",
                       severity="warning")
            event.selection_list.select("hermes")
            return
        self.settings.dashboard_panes_enabled = selected
        save_settings(self.settings)
        self._apply_dashboard_panes_enabled()

    def _on_settings_passphrase_result(self, key: bytes | None) -> None:
        # See the NOTE on push_screen callbacks in _on_startup_unlock_result:
        # this callback fires before the modal is popped, so defer.
        self.call_after_refresh(self._apply_settings_passphrase_result, key)

    def _apply_settings_passphrase_result(self, key: bytes | None) -> None:
        if key is None:
            try:
                self.query_one("#settings-encrypt-switch", Switch).value = self.settings.encrypt_at_rest
            except Exception:
                pass
            self.notify("Encryption not changed.")
            return
        if self._cache:
            self._cache.clear_all()
            self._cache.rekey(key)
        self._encrypt_key = key
        self.notify("Encryption enabled (passphrase). Cache cleared and now encrypted.")
        self._update_settings_cache_info()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-reauth-google":
            self._start_google_reauth()
        elif event.button.id == "settings-view-queue":
            # Same dict object, not a copy — cancelling inside the modal
            # mutates self._pending_mutations directly (see _cancel_mutation),
            # so the modal's own re-render already sees the removal.
            self.push_screen(PendingMutationsModal(self._pending_mutations))
        elif event.button.id == "settings-clear-cache":
            if self._cache:
                self._cache.clear_all()
            self.notify("Local cache cleared.")
            self._update_settings_cache_info()
        elif event.button.id == "settings-prune-cache":
            self._prune_cache()
        elif event.button.id == "settings-save-nous-key":
            key = self.query_one("#settings-nous-key", Input).value.strip()
            base_url = self.query_one("#settings-nous-base-url", Input).value.strip()
            self.settings.nous_api_key = key or None
            self.settings.nous_base_url = base_url or None
            save_settings(self.settings)
            self.notify("Nous API key / gateway URL saved.")
        elif event.button.id == "settings-save-browser-home":
            url = self.query_one("#settings-browser-home-url", Input).value.strip()
            self.settings.browser_home_url = url or "https://www.google.com"
            save_settings(self.settings)
            self.notify("Browser home page saved.")
        elif event.button.id == "browser-go":
            raw = self.query_one("#browser-url", Input).value.strip()
            if raw:
                self._browser_navigate(raw, push_history=True)
        elif event.button.id == "settings-add-feed":
            self._add_feed_url()
        elif event.button.id == "settings-remove-feed":
            self._remove_selected_feed()
        elif event.button.id == "settings-browse-feeds":
            self._open_feed_picker()
        elif event.button.id == "settings-remove-remote-host":
            cid = list_tables.current_row_key(
                self.query_one("#settings-remote-hosts-list", DataTable))
            triple = self._remote_hosts_by_cid.get(cid or "")
            if triple is None:
                self.notify("Select a saved remote host to remove first", severity="warning")
                return
            protocol, host, port = triple
            remote_creds.remove(self._encrypt_key, protocol, host, port)
            self._refresh_remote_hosts_list()
            self._refresh_drive_source_select()
            self.notify(f"Removed saved login for {protocol}://{host}:{port}.")
        elif event.button.id == "settings-save-search":
            key = self.query_one("#settings-google-cse-key", Input).value.strip()
            cx = self.query_one("#settings-google-cse-id", Input).value.strip()
            searxng_url = self.query_one("#settings-searxng-url", Input).value.strip()
            self.settings.google_cse_api_key = key or None
            self.settings.google_cse_id = cx or None
            self.settings.searxng_url = searxng_url or None
            save_settings(self.settings)
            self.notify("Search settings saved.")
        elif event.button.id == "nav-go":
            self._nav_go()
        elif event.button.id == "nav-export":
            self._nav_export()
        elif event.button.id == "settings-save-routes":
            key = self.query_one("#settings-routes-key", Input).value.strip()
            self.settings.routes_api_key = key or None
            save_settings(self.settings)
            self.notify("Routes API key saved.")
        elif event.button.id == "settings-save-dashboard-cards":
            loc = self.query_one("#settings-weather-location", Input).value.strip()
            syms_raw = self.query_one("#settings-stock-symbols", Input).value.strip()
            zones_raw = self.query_one("#settings-clock-timezones", Input).value.strip()
            self.settings.weather_location = loc or None
            self.settings.stock_symbols = [s.strip().upper() for s in syms_raw.split(",") if s.strip()]
            self.settings.clock_timezones = [z.strip() for z in zones_raw.split(",") if z.strip()]
            save_settings(self.settings)
            self.notify("Dashboard card settings saved.")
            self._update_dash_clock()  # live, not next-tick -- new timezone list shows immediately
            # Re-fetch now rather than waiting for the next periodic refresh
            # -- a location/symbol list you just typed in should show real
            # data immediately, not "not available yet" for up to a refresh
            # cycle. Same worker _live_refresh_thread is always run through.
            self.run_worker(self._live_refresh_thread, thread=True, exclusive=True)
        elif event.button.id == "contacts-refresh":
            if self._google_creds_ok():
                self._contacts_fetch_started = True
                self.run_worker(self._contacts_fetch_thread, thread=True, exclusive=True, group="contacts-fetch")
            else:
                self._contacts_auth_broken = True
                self._refresh_contacts_list()


# ============================================================================
# Modals
# ============================================================================

class OnboardingWizardModal(ModalScreen):
    """Forced-first-run guidance when Google and/or every AI provider is
    unreachable. "Retry" re-runs the diagnosis in place; "Continue anyway"
    lets the app launch in a degraded state — per-action error handling
    (offline mode, notify() on API errors) already covers that gracefully.
    """

    def __init__(self, app_ref: "GoogleTUI", problems: list[str]):
        super().__init__()
        self._app_ref = app_ref
        self._problems = problems

    def compose(self) -> ComposeResult:
        with Container(id="onboarding-box", classes="pane"):
            yield Label("WELCOME — SETUP NEEDED", classes="pane-title-text")
            with VerticalScroll(id="onboarding-scroll"):
                yield Static(self._body_text(), id="onboarding-text")
            with Horizontal(classes="btnrow"):
                if "google" in self._problems:
                    # Only useful if a token file already exists (expired/
                    # missing scope) — gauth.build_reauth_flow() reuses the
                    # OAuth client embedded in it. A genuinely first-ever
                    # setup (no token file yet) still needs the manual
                    # walkthrough below; this button will just surface that
                    # as an error (inside GoogleReauthModal).
                    yield Button("Re-authorize Google account", id="onboarding-reauth-google")
                yield Button("Retry", id="onboarding-retry")
                yield Button("Continue anyway", id="onboarding-continue")

    def _body_text(self) -> str:
        parts = []
        if "google" in self._problems:
            parts.append(setup_instructions.GOOGLE_SETUP_STEPS)
        if "ai" in self._problems:
            parts.append(setup_instructions.AI_PROVIDER_SETUP_STEPS)
        return "\n\n".join(parts)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "onboarding-continue":
            self.dismiss("continue")
            return
        if e.button.id == "onboarding-reauth-google":
            # _start_google_reauth lives on the App (needs self.svc/
            # self._online/etc.), not this modal — it pushes GoogleReauthModal
            # ON TOP of this one; on success it dismisses THIS screen too
            # (checks isinstance(self.screen, OnboardingWizardModal)) rather
            # than round-tripping back here.
            self._app_ref._start_google_reauth()
            return
        problems = self._app_ref._diagnose_setup()
        if problems:
            self._problems = problems
            self.query_one("#onboarding-text", Static).update(self._body_text())
            self.notify("Still missing setup — see the instructions below.", severity="warning")
        else:
            self.dismiss("resolved")

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss("continue")


class GoogleReauthModal(ModalScreen):
    """In-app Google OAuth re-authorization for headless/no-browser
    environments — this app commonly runs on a headless VM or an
    underpowered laptop with no X11/Wayland compositor (see AGENTS.md), so
    the usual InstalledAppFlow.run_local_server() (spawn a local HTTP
    server, auto-open a browser, wait for the redirect to hit it) doesn't
    work: there's often no browser to open here, and even opening the URL
    on a different device (e.g. a phone) can't reach a server listening on
    THIS machine's localhost.

    Instead: shows the authorization URL as plain, copyable/clickable text
    (works with any browser, on any device); the user consents there, lands
    on a page that fails to load (nothing is listening at the placeholder
    redirect — EXPECTED, not a bug), and pastes the resulting URL (or just
    its `code=` value) back into an Input here. gauth.complete_reauth
    exchanges that for tokens via a single POST — no listening server
    needed at all.

    Dismisses with "reauthorized" on success, None on cancel/escape.
    """

    def __init__(self):
        super().__init__()
        self.flow = None
        self._auth_url: str | None = None
        self._error: str | None = None
        # $TMUX/$STY only tell us THIS process is inside a multiplexer, not
        # whether OSC 52 clipboard forwarding actually works there -- that
        # depends on the multiplexer's version and its own clipboard config
        # (confirmed in practice: a tmux upgrade silently broke `set -g
        # set-clipboard on` for one user, invisible from in here — see
        # git history). So this only prompts a "try outside tmux" step,
        # it can't detect or fix the underlying config itself.
        self._multiplexer = "tmux" if os.environ.get("TMUX") else "screen" if os.environ.get("STY") else None
        try:
            self.flow = gauth.build_reauth_flow()
            self._auth_url = gauth.reauth_authorization_url(self.flow)
        except Exception as e:
            self._error = str(e)

    def compose(self) -> ComposeResult:
        with Container(id="reauth-box", classes="pane"):
            yield Label("GOOGLE RE-AUTHORIZATION", classes="pane-title-text")
            if self._error:
                yield Static(f"Can't start re-authorization:\n\n{self._error}",
                              id="reauth-error", markup=False)
                with Horizontal(classes="btnrow"):
                    yield Button("Close", id="reauth-close")
            else:
                yield Static(
                    "1. Open this URL in ANY browser, on ANY device — this "
                    "machine doesn't need a browser or a display:",
                    id="reauth-instructions-1",
                )
                # markup=False: Textual's markup parser (Content.from_markup,
                # NOT Rich's) chokes on "://" inside a [link=...] tag value
                # (confirmed empirically — same family of gotcha as the News
                # tab's bracketed-feed-title issue in AGENTS.md). Plain text
                # is the correct fix here too, not escaping: most terminals
                # (iTerm2, GNOME Terminal, Windows Terminal, ...) auto-detect
                # and linkify bare URLs in plain output on their own, and a
                # terminal's native mouse-drag selection works on it either
                # way — that covers "copy or click" without fighting the
                # markup parser for a cosmetic OSC-8 tag.
                yield Static(self._auth_url, id="reauth-url", markup=False)
                with Horizontal(classes="btnrow"):
                    yield Button("Copy URL", id="reauth-copy", variant="primary")
                    yield Button("Save to file", id="reauth-save")
                copy_help = (
                    "Copy URL puts it on your computer's clipboard even over "
                    "SSH (terminal must allow OSC 52 — in tmux: "
                    "set -g set-clipboard on). If nothing lands on your "
                    "clipboard, use Save to file, or press F12 to release the "
                    "mouse and select the URL with your terminal as usual."
                )
                if self._multiplexer:
                    copy_help += (
                        f" Detected {self._multiplexer} — if it still doesn't "
                        f"land after that, some {self._multiplexer} versions "
                        "block OSC 52 regardless of config; try detaching/"
                        f"exiting {self._multiplexer} and running this step "
                        "outside it."
                    )
                yield Static(copy_help, id="reauth-copy-help", classes="muted")
                yield Static(
                    "2. Sign in and grant access. You will land on a page "
                    "that fails to load (\"can't reach this page\" / "
                    "connection refused) — that's expected, nothing is "
                    "listening there.\n"
                    "3. Copy the FULL URL from your browser's address bar "
                    "(or just the code= value in it) and paste it below.",
                    id="reauth-instructions-2",
                )
                yield Input(placeholder="Paste the redirect URL or code here", id="reauth-code-input")
                yield Static("", id="reauth-status", classes="muted")
                with Horizontal(classes="btnrow"):
                    yield Button("Submit", id="reauth-submit")
                    yield Button("Cancel", id="reauth-cancel")

    def on_mount(self) -> None:
        if not self._error:
            self.query_one("#reauth-code-input", Input).focus()

    def _copy_url(self) -> None:
        """OSC 52 — the escape sequence goes to the TERMINAL EMULATOR, not the
        machine the app runs on, so the URL lands on the clipboard of whatever
        computer you're sitting at even when the app is on a headless box over
        SSH. Not universal (macOS Terminal ignores it; tmux needs
        `set-clipboard on`), which is exactly why "Save to file" and the F12
        mouse-release toggle exist alongside it.
        """
        self.app.copy_to_clipboard(self._auth_url or "")
        status = (
            "Copied to clipboard. If your clipboard is still empty, your "
            "terminal blocks OSC 52 — use Save to file or F12 instead."
        )
        if self._multiplexer:
            status += (
                f" Still empty and you're in {self._multiplexer}? Some "
                f"versions block OSC 52 no matter how it's configured — try "
                f"exiting {self._multiplexer} and copying from a plain shell."
            )
        self.query_one("#reauth-status", Static).update(status)

    def _save_url(self) -> None:
        """Bulletproof fallback for terminals that swallow OSC 52: drop the URL
        in a file the user can `cat`, `scp`, or open from another shell."""
        path = AUTH_URL_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((self._auth_url or "") + "\n", encoding="utf-8")
        except Exception as e:
            self.query_one("#reauth-status", Static).update(f"Couldn't write {path}: {e}")
            return
        self.query_one("#reauth-status", Static).update(f"Saved to {path}")
        self.notify(f"Auth URL written to {path}")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id in ("reauth-cancel", "reauth-close"):
            self.dismiss(None)
        elif e.button.id == "reauth-copy":
            self._copy_url()
        elif e.button.id == "reauth-save":
            self._save_url()
        elif e.button.id == "reauth-submit":
            self._submit()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id == "reauth-code-input":
            self._submit()

    def _submit(self) -> None:
        pasted = self.query_one("#reauth-code-input", Input).value.strip()
        if not pasted:
            return
        self.query_one("#reauth-code-input", Input).disabled = True
        self.query_one("#reauth-submit", Button).disabled = True
        self.query_one("#reauth-status", Static).update("Exchanging code with Google…")
        # complete_reauth POSTs to Google's token endpoint — a real network
        # call, so it runs on a worker thread like every other gauth call in
        # this app, not inline here.
        self.run_worker(lambda: self._exchange(pasted), thread=True, exclusive=True)

    def _exchange(self, pasted: str) -> None:
        try:
            gauth.complete_reauth(self.flow, pasted)
        except Exception as e:
            self.app.call_from_thread(self._apply_error, e)
            return
        self.app.call_from_thread(self._apply_success)

    def _apply_success(self) -> None:
        self.dismiss("reauthorized")

    def _apply_error(self, error: Exception) -> None:
        try:
            self.query_one("#reauth-status", Static).update(f"Failed: {error}")
            self.query_one("#reauth-code-input", Input).disabled = False
            self.query_one("#reauth-submit", Button).disabled = False
        except Exception:
            pass
        self.notify(f"Re-authorization failed: {error}", severity="error")

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class LoadingModal(ModalScreen):
    """Shown immediately on startup, before any Google API call is made."""

    def compose(self) -> ComposeResult:
        with Container(id="loading-box", classes="pane"):
            yield Label("Loading your Google Workspace data…", classes="pane-title-text")

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class UnlockModal(ModalScreen):
    """Passphrase entry, in two modes:

    - "unlock" (app startup, encryption already configured): verify against
      the stored canary; a wrong passphrase re-prompts, with a "reset" escape
      hatch that wipes the (unrecoverable without it) encrypted cache.
    - "create" (Settings tab, first time enabling passphrase mode): enter +
      confirm, then generate a fresh salt/canary and save them to settings.

    Dismisses with the derived Fernet key (bytes) on success, or None if the
    user backed out (Settings caller: leave encryption off / unchanged;
    startup caller: fall back to an unencrypted, freshly-cleared cache).
    """

    def __init__(self, settings: Settings, mode: str = "unlock"):
        super().__init__()
        self.settings = settings
        self.mode = mode
        self._confirm_stage = False
        self._first_passphrase: str | None = None

    def compose(self) -> ComposeResult:
        title = "CREATE PASSPHRASE" if self.mode == "create" else "UNLOCK CACHE"
        with Container(id="unlock-box", classes="pane"):
            yield Label(title, classes="pane-title-text")
            yield Static("", id="unlock-error", classes="muted")
            yield Input(placeholder="Passphrase", password=True, id="unlock-input")
        with Horizontal(classes="btnrow"):
            if self.mode == "unlock":
                yield Button("Reset (forgot passphrase)", id="reset")
            else:
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#unlock-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        pw = event.value
        error = self.query_one("#unlock-error")
        inp = self.query_one("#unlock-input", Input)
        if self.mode == "create":
            if not self._confirm_stage:
                self._first_passphrase = pw
                self._confirm_stage = True
                inp.value = ""
                error.update("Confirm passphrase:")
                return
            if pw != self._first_passphrase:
                error.update("Passphrases didn't match — try again.")
                self._confirm_stage = False
                self._first_passphrase = None
                inp.value = ""
                return
            salt = new_salt()
            key = derive_key_from_passphrase(pw, salt)
            self.settings.encrypt_at_rest = True
            self.settings.key_method = "passphrase"
            self.settings.kdf_salt = base64.b64encode(salt).decode("ascii")
            self.settings.canary = make_canary(key)
            save_settings(self.settings)
            self.dismiss(key)
        else:  # unlock
            salt = base64.b64decode(self.settings.kdf_salt)
            key = derive_key_from_passphrase(pw, salt)
            if verify_canary(key, self.settings.canary):
                self.dismiss(key)
            else:
                error.update("Wrong passphrase — try again.")
                inp.value = ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("reset", "cancel"):
            self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape" and self.mode == "create":
            self.dismiss(None)


def _human_size(n) -> str:
    """Compact human-readable byte size for the attachments list."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class AttachmentsModal(ModalScreen):
    """View + download a thread's attachments (ROADMAP P2). Enter or 'd'
    downloads the highlighted attachment to EXPORT_DIR (Documents/google-tui/),
    the same no-picker destination the Drive tab's 'd' download uses. An
    attachment Gmail inlined (small; `inline_data` present) is decoded
    directly; a larger one is fetched via gauth.download_attachment."""

    def __init__(self, svc, attachments: list[dict]):
        super().__init__()
        self.svc = svc
        self.attachments = attachments

    def compose(self) -> ComposeResult:
        with Container(id="att-box", classes="pane"):
            yield Label(f"ATTACHMENTS ({len(self.attachments)})", classes="pane-title-text")
            yield ListView(id="att-list")
            yield Static("Enter / d  download to Documents/google-tui     Esc  close",
                         classes="muted")

    def on_mount(self) -> None:
        lst = self.query_one("#att-list", ListView)
        lst.extend(
            ListItem(Label(f"{a['filename']}  ({_human_size(a.get('size'))}, {a.get('mime_type','')})",
                           markup=False), id=f"att-{i}")
            for i, a in enumerate(self.attachments))
        if self.attachments:
            lst.index = 0
        lst.focus()

    def on_list_view_selected(self, e: ListView.Selected) -> None:
        self._download(e.item)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)
        elif e.key == "d":
            item = self.query_one("#att-list", ListView).highlighted_child
            if item is not None:
                self._download(item)

    def _download(self, item) -> None:
        try:
            idx = int((item.id or "att--1").split("-")[1])
        except (ValueError, IndexError):
            return
        if not (0 <= idx < len(self.attachments)):
            return
        a = self.attachments[idx]
        if not a.get("inline_data") and not self.app._online:
            self.notify("Downloading needs a network connection.", severity="warning")
            return
        self.notify(f"Downloading {a['filename']}…")
        self.run_worker(lambda: self._download_thread(a), thread=True, exclusive=True,
                         group="attachment-download")

    def _download_thread(self, a: dict) -> None:
        try:
            if a.get("inline_data"):
                data = base64.urlsafe_b64decode(a["inline_data"])
            else:
                data = gauth.download_attachment(self.svc, a["message_id"], a["attachment_id"])
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            # basename: an attachment filename is attacker-influenced — never
            # let it escape EXPORT_DIR via "../" or an absolute path.
            path = EXPORT_DIR / Path(a["filename"]).name
            path.write_bytes(data)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Download failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.notify, f"Downloaded to {path}")


class SnoozeModal(ModalScreen):
    """Pick a remind-at time for snoozing a thread (ROADMAP P2). Dismisses
    with a tz-aware ``datetime`` (a preset or a parsed custom value) or None.
    The preset times are computed in the app's timezone so "Tomorrow 9:00"
    means 9am where the user is, not UTC."""

    def __init__(self, tzinfo):
        super().__init__()
        self.tzinfo = tzinfo

    def compose(self) -> ComposeResult:
        with Container(id="snooze-box", classes="pane"):
            yield Label("SNOOZE UNTIL", classes="pane-title-text")
            with Vertical():
                yield Button("Later today (+3h)", id="sn-3h")
                yield Button("Tomorrow 09:00", id="sn-tom")
                yield Button("This weekend (Sat 09:00)", id="sn-weekend")
                yield Button("Next week (Mon 09:00)", id="sn-week")
            yield Input(placeholder="Custom: YYYY-MM-DD HH:MM", id="sn-custom")
            with Horizontal(classes="btnrow"):
                yield Button("Snooze custom", id="sn-custom-go")
                yield Button("Cancel", id="cancel")

    def _at(self, day: dt.date, hour: int = 9, minute: int = 0) -> dt.datetime:
        return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tzinfo)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        now = dt.datetime.now(self.tzinfo)
        if e.button.id == "cancel":
            self.dismiss(None)
        elif e.button.id == "sn-3h":
            self.dismiss(now + dt.timedelta(hours=3))
        elif e.button.id == "sn-tom":
            self.dismiss(self._at(now.date() + dt.timedelta(days=1)))
        elif e.button.id == "sn-weekend":
            # Days until the coming Saturday (weekday 5); if today is already
            # Sat/Sun, jump to next Saturday rather than "today".
            ahead = (5 - now.weekday()) % 7 or 7
            self.dismiss(self._at(now.date() + dt.timedelta(days=ahead)))
        elif e.button.id == "sn-week":
            ahead = (7 - now.weekday()) % 7 or 7  # next Monday
            self.dismiss(self._at(now.date() + dt.timedelta(days=ahead)))
        elif e.button.id == "sn-custom-go":
            self._submit_custom()

    def _submit_custom(self) -> None:
        raw = self.query_one("#sn-custom", Input).value.strip()
        try:
            when = dt.datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=self.tzinfo)
        except ValueError:
            self.notify("Use YYYY-MM-DD HH:MM (24h)", severity="warning")
            return
        if when <= dt.datetime.now(self.tzinfo):
            self.notify("Pick a time in the future", severity="warning")
            return
        self.dismiss(when)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class BulkActionModal(ModalScreen):
    """Chooser for a multi-select bulk action (ROADMAP P2). Opened with 'X'
    when ≥1 thread is checked; dismisses with the chosen action id
    ("archive"/"trash"/"label") or None. The App runs the action over every
    selected thread — see _on_bulk_action_result."""

    def __init__(self, count: int):
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Container(id="bulk-box", classes="pane"):
            yield Label(f"BULK ACTIONS — {self.count} selected", classes="pane-title-text")
            with Vertical():
                yield Button("Archive", id="bulk-archive")
                yield Button("Trash", id="bulk-trash")
                yield Button("Apply Label…", id="bulk-label")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss({"bulk-archive": "archive", "bulk-trash": "trash",
                      "bulk-label": "label"}.get(e.button.id))

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class LabelPickerModal(ModalScreen):
    """Multi-select label picker for ThreadModal's "L" action. Presents the
    account's user labels as a checklist, pre-checked for whichever labels
    the thread already carries (`applied_ids`, from the union of each
    message's `label_ids` — see gauth.get_thread); dismisses with the list
    of NEWLY selected label ids to ADD to the thread (or None on cancel).
    Already-applied ids are excluded from the add-list since they're
    already on the thread — this stays assign-only (no removal), matching
    what the ROADMAP asked for."""

    def __init__(self, labels: list[dict], applied_ids: frozenset[str] = frozenset()):
        super().__init__()
        self._labels = labels
        self._applied_ids = applied_ids
        # Checked state survives filtering — SelectionList itself only knows
        # about whatever's currently rendered, so a checked-then-filtered-out
        # label would otherwise lose its check. _visible_ids is whichever
        # labels _rebuild_list last rendered, so a toggle handler can tell
        # "this id's SelectionList state is now authoritative" (visible) from
        # "leave whatever we already have" (filtered out, untouched).
        self._checked_ids: set[str] = set(applied_ids)
        self._visible_ids: set[str] = {l["id"] for l in labels}

    def compose(self) -> ComposeResult:
        with Container(id="labelpick-box", classes="pane"):
            yield Label("ASSIGN LABELS", classes="pane-title-text")
            yield Input(placeholder="Filter labels…", id="labelpick-search")
            yield SelectionList(
                *[(_label_display_name(l), l["id"], l["id"] in self._checked_ids)
                  for l in self._labels],
                id="labelpick-list")
            with Horizontal(classes="btnrow"):
                yield Button("Apply", id="labelpick-apply")
                yield Button("Cancel", id="labelpick-cancel")

    def _rebuild_list(self, query: str) -> None:
        filtered = _fuzzy_filter_labels(self._labels, query)
        self._visible_ids = {l["id"] for l in filtered}
        sel_list = self.query_one("#labelpick-list", SelectionList)
        sel_list.clear_options()
        sel_list.add_options(
            [(_label_display_name(l), l["id"], l["id"] in self._checked_ids) for l in filtered])

    def on_input_changed(self, e: Input.Changed) -> None:
        if e.input.id == "labelpick-search":
            self._rebuild_list(e.value)

    def on_selection_list_selection_toggled(self, e: SelectionList.SelectionToggled) -> None:
        sel_list = self.query_one("#labelpick-list", SelectionList)
        self._checked_ids = (self._checked_ids - self._visible_ids) | set(sel_list.selected)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "labelpick-apply":
            sel_list = self.query_one("#labelpick-list", SelectionList)
            checked = (self._checked_ids - self._visible_ids) | set(sel_list.selected)
            self.dismiss(list(checked - self._applied_ids))
        else:
            self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class FeedPickerModal(ModalScreen):
    """Multi-select picker over `popular_feeds.POPULAR_FEEDS` for Settings ->
    News Feeds' "Browse popular feeds…" button (ROADMAP: RSS subscription
    list). Unlike LabelPickerModal this is a genuine two-way toggle, not an
    assign-only add: checking a box subscribes, unchecking one already
    subscribed unsubscribes, so on Apply the caller (`GoogleTUI._on_feed_pick_
    result`) diffs the FULL returned selection against `Settings.feed_urls`
    rather than just looking at what's newly checked. Manually-added feeds
    outside this curated table are untouched either way — they never appear
    in this list, so they can't be toggled off by it.

    Each row's `SelectionList` id is the feed's URL (globally unique, unlike
    title). The visible label is "Category — Title" so the same filter box
    doubles as a category filter (typing "cyber" surfaces every Cybersecurity
    row) and a title filter (typing "bbc" surfaces every BBC feed)."""

    def __init__(self, applied_urls: frozenset[str] = frozenset()):
        super().__init__()
        self._feeds: list[dict] = [
            {"category": cat, "title": f["title"], "url": f["url"], "label": f"{cat} — {f['title']}"}
            for cat, feeds in POPULAR_FEEDS.items() for f in feeds
        ]
        self._checked_urls: set[str] = set(applied_urls) & {f["url"] for f in self._feeds}
        self._visible_urls: set[str] = {f["url"] for f in self._feeds}

    def compose(self) -> ComposeResult:
        with Container(id="feedpick-box", classes="pane"):
            yield Label("BROWSE POPULAR FEEDS", classes="pane-title-text")
            yield Input(placeholder="Filter by category or title…", id="feedpick-search")
            yield SelectionList(
                *[(f["label"], f["url"], f["url"] in self._checked_urls) for f in self._feeds],
                id="feedpick-list")
            with Horizontal(classes="btnrow"):
                yield Button("Apply", id="feedpick-apply")
                yield Button("Cancel", id="feedpick-cancel")

    def _rebuild_list(self, query: str) -> None:
        filtered = _fuzzy_filter_feeds(self._feeds, query)
        self._visible_urls = {f["url"] for f in filtered}
        sel_list = self.query_one("#feedpick-list", SelectionList)
        sel_list.clear_options()
        sel_list.add_options(
            [(f["label"], f["url"], f["url"] in self._checked_urls) for f in filtered])

    def on_input_changed(self, e: Input.Changed) -> None:
        if e.input.id == "feedpick-search":
            self._rebuild_list(e.value)

    def on_selection_list_selection_toggled(self, e: SelectionList.SelectionToggled) -> None:
        sel_list = self.query_one("#feedpick-list", SelectionList)
        self._checked_urls = (self._checked_urls - self._visible_urls) | set(sel_list.selected)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "feedpick-apply":
            sel_list = self.query_one("#feedpick-list", SelectionList)
            checked = (self._checked_urls - self._visible_urls) | set(sel_list.selected)
            self.dismiss(list(checked))
        else:
            self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class ThreadModal(ModalScreen):
    """Thread detail. Each message is rendered through M1's shared renderer
    (P1 M4) instead of the old plain-text-stripped RichLog: an HTML message
    (`html_body` non-empty) is parsed via `render.parse_feed_entry` — which
    itself routes HTML through `render.parse_html` — into a `Document` shown
    in its own `render.DocumentView`; a plain-text-only message hits the same
    call but takes `parse_feed_entry`'s non-HTML fallback (each line becomes
    a paragraph block), so there's only one rendering path for both cases.
    One (From/Date header `Static` + `DocumentView`) pair per message,
    stacked in `#thread-messages` (a `VerticalScroll`), oldest-first —
    message order is unchanged from before (see AGENTS.md). Deliberately NOT
    a single merged `Document`: that would require renumbering each
    message's `[N]` link markers to stay unique across the whole thread.
    `on_document_view_link_activated` now DOES act on links pressed here
    (closes this modal, switches to the Browser tab, and loads the link —
    see that handler in `GoogleTUI`), but strictly per-message: each
    message's `DocumentView` keeps its own independently-numbered `[N]`s,
    and only the one link actually activated (in whichever message's
    `DocumentView` had focus) is ever resolved. Don't "fix" this by merging
    to one `Document`/renumbering across messages — that's an unrelated,
    unrequested change to the numbering scheme, not a bug.
    """

    # ThreadModal is a ModalScreen, which truncates Textual's binding-chain
    # walk at the modal boundary — so the App-level r/a/f bindings never
    # reach here while this is open. These are real modal-scoped bindings,
    # not a reliance on that (previously dead) app-level fallthrough.
    BINDINGS = bindings.bindings_for_scope("modal:ThreadModal")

    # Disable Textual's auto-focus-on-mount for this modal. compose() yields a
    # `.hidden` (CSS display:none) #thread-search Input BEFORE the message
    # VerticalScroll. `.hidden` only sets `display`, but Widget.focusable keys
    # off `visibility` (DOMNode.visible) — so a display:none widget is still
    # "focusable", and Screen._update_auto_focus (driven by app.AUTO_FOCUS="*")
    # would silently focus that hidden search box as the first focusable widget
    # in DOM order the instant this modal opens. A focused Input swallows
    # printable keys, so r/a/f/d/s/l/arrows would all no-op on first open until
    # focus moved. Must be "" (falsy), NOT None: Screen.AUTO_FOCUS=None means
    # "inherit app.AUTO_FOCUS" (="*"), which is exactly the buggy behavior;
    # only a falsy-but-not-None value makes _update_auto_focus skip focusing.
    # "/" still calls search.focus() itself, independent of AUTO_FOCUS.
    AUTO_FOCUS = ""

    def __init__(self, svc, thread_id: str, thread_ids: list[str] | None = None,
                 index: int = 0):
        super().__init__()
        self.svc = svc
        self.thread_id = thread_id
        # The ordered list of thread ids in the Email pane (as it looked when
        # this modal was opened) + our position in it, so Left/Right can page
        # to the prev/next message's thread IN PLACE without closing/reopening
        # (see AGENTS.md P2 item). Defaults to a single-element list so the
        # modal still works when opened outside that context.
        self.thread_ids: list[str] = list(thread_ids) if thread_ids else [thread_id]
        self.index: int = index if 0 <= index < len(self.thread_ids) else 0
        # Search-within-thread state (the "/" action). _search_targets is
        # (DocumentView, lowercased-searchable-text) per mounted message,
        # rebuilt every _apply_thread; _search_matches/_search_pos track the
        # current find-next cursor over the last query's hits.
        self._search_targets: list[tuple[DocumentView, str]] = []
        self._search_matches: list[DocumentView] = []
        self._search_pos: int = -1
        # Flattened attachments across the thread's messages ('g' viewer),
        # (re)built in _apply_thread; empty until the thread loads.
        self._attachments: list[dict] = []
        # Union of every message's label_ids (gauth.get_thread) — lets
        # LabelPickerModal pre-check already-applied labels, and the
        # "Labels: …" line below make an apply visibly confirmed instead of
        # trusting the toast alone (see ROADMAP P1 LabelPickerModal item).
        self._label_ids: set[str] = set()

    def compose(self) -> ComposeResult:
        with Container(id="thread-box", classes="pane"):
            yield Label("THREAD", classes="pane-title-text", id="thread-title")
            yield Static("", id="thread-labels", classes="muted")
            yield Input(placeholder="Find in thread… (Enter = next)",
                        id="thread-search", classes="hidden")
            with VerticalScroll(id="thread-messages"):
                yield Static("Loading…", markup=False)
            # Contextual help bar for this modal, consistent with the app's
            # global help bar — entries are clickable action links (see
            # bindings.help_markup). This is the ONLY control surface for
            # reply/reply-all/forward/trash/archive/labels/close now — a
            # duplicate row of full-size buttons repeating the same commands
            # used to sit below the message list too, wasting the scarce
            # vertical space every other pane/tab keeps free by showing
            # shortcuts as text instead of buttons (see ROADMAP/CHANGELOG).
            yield Static(bindings.help_markup("modal:ThreadModal",
                                              self.app.settings.ascii_mode),
                         id="thread-help")

    def on_mount(self) -> None:
        # Respect Settings.ascii_mode for this modal's borders: _apply_ascii_mode
        # can't have reached #thread-box (it's only in the DOM while this modal
        # is open, and that method runs at startup / on the Settings toggle),
        # so apply the class here on open. The paired ".pane.ascii-border" CSS
        # rule does the actual border-glyph swap.
        if self.app.settings.ascii_mode:
            self.query_one("#thread-box").add_class("ascii-border")
        # gauth.get_thread is a blocking synchronous network call (same as
        # every other gauth-touching method in this app) — must run on a
        # worker THREAD, not directly in on_mount, both so it doesn't freeze
        # the UI and so a transient network error (e.g. an SSL hiccup while
        # the app's own background reconnect is still in flight) is caught
        # and shown as a notify instead of crashing the whole app.
        self.run_worker(self._fetch_thread, thread=True, exclusive=True)

    def _fetch_thread(self) -> None:
        app = self.app
        # Serve the body from cache when we can prove it's current. The thread
        # summary carries the historyId Gmail last reported for this thread, and
        # Gmail bumps it on ANY change — so a cached body stamped with the same
        # historyId is exactly what a refetch would return. Without this, every
        # reopen of the same email re-downloaded the whole thread (cache.py's
        # docstring always claimed to cache "thread bodies"; nothing ever did).
        # Bonus: an already-read thread is now readable offline.
        summary = getattr(app, "_threads_cache", {}).get(self.thread_id) or {}
        hid = str(summary.get("historyId") or "")
        cache = getattr(app, "_cache", None)
        msgs = None
        if cache and hid:
            hit = cache.get("thread_body", self.thread_id)
            if hit and str(hit.get("historyId") or "") == hid:
                msgs = hit.get("msgs")

        if msgs is None:
            try:
                msgs = gauth.get_thread(self.svc, self.thread_id)
            except Exception as e:
                app.call_from_thread(self._apply_error, e)
                return
            if cache and hid:
                cache.put("thread_body", self.thread_id, {"historyId": hid, "msgs": msgs})

        app.call_from_thread(self._apply_thread, msgs)
        # Only worth a network write if it's actually unread. (Marking it read
        # bumps the historyId, which self-invalidates the row we just cached —
        # correct: the next open refetches once and re-caches as read.)
        if summary.get("unread", True):
            try:
                gauth.mark_read(self.svc, self.thread_id)
            except Exception:
                pass  # best-effort — not worth surfacing a separate error for this

    async def _apply_thread(self, msgs: list[dict]) -> None:
        # async (unlike the rest of this app's call_from_thread targets):
        # each DocumentView needs its own children (#doc-nav/#doc-title/
        # #doc-body, from DocumentView.compose()) actually mounted before
        # `.document =` triggers watch_document's query_one calls on them —
        # `await container.mount(...)` is what guarantees that, a bare
        # fire-and-forget `.mount()` races it. call_from_thread awaits a
        # coroutine callback via `invoke()`, so returning one here is safe.
        self._update_title()
        self._label_ids = set()
        for m in msgs:
            self._label_ids.update(m.get("label_ids") or [])
        # Every attachment across the thread's messages, flattened, for the
        # 'g' attachments viewer/download (ROADMAP P2).
        self._attachments = [a for m in msgs for a in (m.get("attachments") or [])]
        self._update_labels_line()
        # A fresh thread body invalidates the previous message's search hits.
        self._search_targets = []
        self._search_matches = []
        self._search_pos = -1
        ascii_mode = self.app.settings.ascii_mode
        container = self.query_one("#thread-messages", VerticalScroll)
        await container.remove_children()
        if not msgs:
            await container.mount(Static("(no messages)", markup=False))
            return
        pending: list[tuple[DocumentView, "render.Document", str]] = []
        new_widgets = []
        for m in msgs:
            header = f"From: {m.get('from', '')}    Date: {m.get('date', '')}"
            atts = m.get("attachments") or []
            if atts:
                clip = "Attachments: " if ascii_mode else "📎 "
                header += "\n" + clip + ", ".join(a["filename"] for a in atts)
            header_widget = Static(header, classes="thread-msg-header", markup=False)
            if ascii_mode:
                header_widget.add_class("ascii-border")  # see on_mount / _apply_ascii_mode
            new_widgets.append(header_widget)
            html_body = (m.get("html_body") or "").strip()
            text_body = m.get("body") or ""
            source = html_body if html_body else text_body
            doc = render.parse_feed_entry(m.get("subject", ""), source, base_url="",
                                           ascii_mode=ascii_mode)
            dv = DocumentView(classes="thread-msg-doc")
            new_widgets.append(dv)
            # Searchable text for the "/" find: the header plus every block's
            # rendered text, lowercased once so find-next is a cheap substring
            # test per message (not a re-parse per keystroke).
            searchable = (header + "\n"
                          + "\n".join(b.text for b in doc.blocks)).lower()
            pending.append((dv, doc, searchable))
        await container.mount(*new_widgets)
        for dv, doc, searchable in pending:
            # DocumentView's own DEFAULT_CSS sets height:1fr (correct for
            # its usual full-pane use in the Browser/News tabs); stacking
            # several inside one VerticalScroll needs auto height instead
            # so each message takes only the space its content needs.
            dv.styles.height = "auto"
            dv.document = doc
            self._search_targets.append((dv, searchable))

    def _update_title(self) -> None:
        try:
            title = self.query_one("#thread-title", Label)
        except Exception:
            return
        if len(self.thread_ids) > 1:
            title.update(f"THREAD  ({self.index + 1}/{len(self.thread_ids)})")
        else:
            title.update("THREAD")

    def _update_labels_line(self) -> None:
        try:
            widget = self.query_one("#thread-labels", Static)
        except Exception:
            return
        by_id = {l["id"]: l for l in getattr(self.app, "_labels_cache", [])}
        names = sorted(_label_display_name(by_id[lid]) for lid in self._label_ids if lid in by_id)
        widget.update(f"Labels: {', '.join(names)}" if names else "")

    def _apply_error(self, error: Exception) -> None:
        container = self.query_one("#thread-messages", VerticalScroll)
        container.remove_children()
        container.mount(Static(f"Couldn't load this thread:\n{error}", markup=False))
        self.app.notify(f"Thread load error: {error}", severity="error")

    def action_reply(self) -> None:
        self.dismiss(("compose", self.thread_id, "reply"))

    def action_reply_all(self) -> None:
        self.dismiss(("compose", self.thread_id, "reply_all"))

    def action_forward(self) -> None:
        self.dismiss(("compose", self.thread_id, "forward"))

    # Not on any BINDINGS entry — Esc is handled ad hoc in on_key below (it
    # has to check the search box first). This exists only so "Esc Close" in
    # the help bar (see bindings._CLICK_ACTIONS) can be a clickable action
    # link, since the removed button row was mouse users' only other way to
    # close this modal.
    def action_close(self) -> None:
        self.dismiss(None)

    # ---- Left/Right: prev/next message in the current folder, in place ----
    def action_prev_message(self) -> None:
        self._navigate(-1)

    def action_next_message(self) -> None:
        self._navigate(1)

    def _navigate(self, delta: int) -> None:
        new_index = self.index + delta
        if not (0 <= new_index < len(self.thread_ids)):
            return  # at an end — no wraparound (matches the list's own edges)
        self.index = new_index
        self.thread_id = self.thread_ids[new_index]
        # Hide any open search box and show the loading placeholder, then
        # re-run the exact same fetch/apply path as on open for the new id.
        self._hide_search()
        container = self.query_one("#thread-messages", VerticalScroll)
        container.remove_children()
        container.mount(Static("Loading…", markup=False))
        self._update_title()
        self.run_worker(self._fetch_thread, thread=True, exclusive=True)

    # ---- "/" find-in-thread ----
    def action_focus_search(self) -> None:
        search = self.query_one("#thread-search", Input)
        search.remove_class("hidden")
        search.focus()

    def _hide_search(self) -> None:
        try:
            search = self.query_one("#thread-search", Input)
        except Exception:
            return
        search.value = ""
        search.add_class("hidden")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "thread-search":
            self._find(event.value)

    def _find(self, query: str) -> None:
        query = query.strip().lower()
        if not query:
            return
        matches = [dv for dv, text in self._search_targets if query in text]
        if not matches:
            self.app.notify("No match in this thread", severity="warning")
            self._search_matches = []
            self._search_pos = -1
            return
        # Re-run of the same query → advance to the next hit (find-next);
        # a new query → start at the first hit.
        if matches == self._search_matches:
            self._search_pos = (self._search_pos + 1) % len(matches)
        else:
            self._search_matches = matches
            self._search_pos = 0
        target = matches[self._search_pos]
        self.query_one("#thread-messages", VerticalScroll).scroll_to_widget(
            target, top=True, animate=False)
        if len(matches) > 1:
            self.app.notify(f"Match {self._search_pos + 1} of {len(matches)}")

    # ---- D trash / S archive / L labels (all reversible; see gauth.py) ----
    def action_trash(self) -> None:
        if not self.app._online:
            self._queue_mutation({"type": "trash", "thread_id": self.thread_id},
                                 "Offline — queued, will move to Trash once reconnected.")
            return
        self._run_mutation(lambda: gauth.trash_thread(self.svc, self.thread_id),
                           "Moved to Trash — Ctrl+Z to undo",
                           on_success=lambda: self.app._record_mail_undo("trash", self.thread_id))

    def action_archive(self) -> None:
        if not self.app._online:
            self._queue_mutation({"type": "archive", "thread_id": self.thread_id},
                                 "Offline — queued, will archive once reconnected.")
            return
        self._run_mutation(lambda: gauth.archive_thread(self.svc, self.thread_id),
                           "Archived (removed from Inbox) — Ctrl+Z to undo",
                           on_success=lambda: self.app._record_mail_undo("archive", self.thread_id))

    def action_labels(self) -> None:
        # No online gate here — same reasoning as ComposeModal: picking labels
        # is harmless offline, only the actual write (_on_labels_result) needs
        # to be gated/queued.
        labels = getattr(self.app, "_labels_cache", [])
        pickable = [l for l in labels
                    if l.get("type") != "system" and l.get("id") and l.get("name")]
        if not pickable:
            self.app.notify("No labels available to assign", severity="warning")
            return
        self.app.push_screen(
            LabelPickerModal(pickable, applied_ids=frozenset(self._label_ids)),
            self._on_labels_result)

    def action_email_to_task(self) -> None:
        # Delegates to the app: it owns the tasklists + the create/queue flow,
        # and the modal it opens stacks on top of this one (create, then land
        # back on the thread). Same thread_id either way.
        self.app._open_email_to_task(self.thread_id)

    def action_email_to_event(self) -> None:
        self.app._open_email_to_event(self.thread_id)

    def action_attachments(self) -> None:
        """'g' — open the attachments viewer for this thread (download any of
        them to Documents/google-tui/). No-op with a toast when the thread
        has none."""
        if not self._attachments:
            self.app.notify("No attachments in this thread")
            return
        self.app.push_screen(AttachmentsModal(self.svc, self._attachments))

    def _on_labels_result(self, add_ids) -> None:
        if not add_ids:
            return
        if not self.app._online:
            self._label_ids.update(add_ids)
            self._update_labels_line()
            self._queue_mutation(
                {"type": "modify_labels", "thread_id": self.thread_id, "add": list(add_ids)},
                f"Offline — queued {len(add_ids)} label(s), will apply once reconnected.",
                close=False)
            return
        self._run_mutation(
            lambda: gauth.modify_labels(self.svc, self.thread_id, add=list(add_ids)),
            f"Applied {len(add_ids)} label(s)", close=False,
            on_success=lambda: self._confirm_labels_applied(add_ids))

    def _confirm_labels_applied(self, add_ids) -> None:
        self._label_ids.update(add_ids)
        self._update_labels_line()

    def _queue_mutation(self, mutation: dict, msg: str, close: bool = True) -> None:
        """Offline counterpart to _run_mutation: enqueue for replay instead of
        calling the API now. Trash/archive optimistically drop the thread from
        the shared cache — the same "it's gone now" UX the online path's
        post-write refresh gives — since the user just asked for it to leave
        the list. Labels have nothing inline to update (the email list doesn't
        show per-thread label chips), so modify_labels just queues silently.
        Dismissing with "queued" (not "refresh") tells _on_thread_modal_result
        not to attempt a network refetch that would just fail offline."""
        self.app._enqueue_mutation(mutation)
        if mutation["type"] in ("trash", "archive"):
            self.app._threads_cache.pop(self.thread_id, None)
            self.app._apply_email_list(list(self.app._threads_cache.values()))
        self.app.notify(msg)
        if close:
            self.dismiss("queued")

    def _run_mutation(self, fn, success_msg: str, close: bool = True, on_success=None) -> None:
        """Run a mutating gauth call on a worker thread (fetch/apply split),
        then notify + (optionally) dismiss with "refresh" so the Email pane
        drops/updates the thread. `close=False` keeps the modal open (used for
        label changes, which don't remove the thread from view). `on_success`,
        if given, runs on the UI thread right before the notify — used by the
        labels flow to reflect the newly-applied labels immediately instead of
        trusting the toast alone (see LabelPickerModal/action_labels)."""
        def work() -> None:
            try:
                fn()
            except Exception as e:
                self.app.call_from_thread(self.app.notify, f"Action failed: {e}",
                                          severity="error")
                return
            self.app.call_from_thread(self._after_mutation, success_msg, close, on_success)
        self.run_worker(work, thread=True, exclusive=True)

    def _after_mutation(self, msg: str, close: bool, on_success=None) -> None:
        if on_success:
            on_success()
        self.app.notify(msg)
        if close:
            self.dismiss("refresh")

    def on_key(self, e) -> None:
        if e.key == "escape":
            # Escape closes the search box first (if open), otherwise the modal.
            try:
                search = self.query_one("#thread-search", Input)
            except Exception:
                search = None
            if search is not None and not search.has_class("hidden"):
                self._hide_search()
                self.query_one("#thread-messages", VerticalScroll).focus()
                e.stop()
                return
            self.dismiss(None)


class ComposeModal(ModalScreen):
    """Reply / Reply All / Forward (mode in {"reply","reply_all","forward"},
    thread_id required) OR a blank compose-from-scratch (mode == "new",
    thread_id is None, `to` optionally pre-fills the To field — used by the
    Contacts tab's "Compose New"/per-contact compose entry points, P1 M5).
    """
    SEND_COUNTDOWN_SECONDS = 5

    def __init__(self, svc, thread_id: str | None, mode: str, to: str = ""):
        super().__init__()
        self.svc = svc
        self.thread_id = thread_id
        self.mode = mode
        self._prefill_to = to
        self._countdown_remaining = 0
        self._countdown_timer = None  # Textual Timer handle while a send is pending
        self._attachments: list[str] = []  # local file paths to attach on send

    def compose(self) -> ComposeResult:
        with Container(id="compose-box", classes="pane"):
            yield Label("COMPOSE", classes="pane-title-text")
            yield Input(placeholder="To", id="c-to")
            yield ListView(id="c-to-suggestions", classes="hidden")
            yield Input(placeholder="Cc", id="c-cc")
            yield Input(placeholder="Bcc", id="c-bcc")
            yield Input(placeholder="Subject", id="c-subject")
            yield TextArea(id="c-body", language="markdown")
            with Horizontal(classes="btnrow"):
                yield Input(placeholder="Attach a file (path), then Enter", id="c-attach")
                yield Button("Attach", id="c-attach-btn")
            yield Static("", id="c-attach-list")
        with Horizontal(classes="btnrow"):
            yield Button("Send", id="send")
            yield Button("Save Draft", id="save-draft")
            yield Button("Cancel", id="cancel")
        yield Static("", id="send-countdown")

    def _prefill_from_cache(self) -> tuple[str, str, str]:
        """(to, cc, subject) from the cached thread summary — no network. Used
        offline, and as the fallback when the live fetch below fails. The
        summary carries subject/from only (list_threads never fetches To/Cc),
        so reply_all degrades to replying just to the sender; the actual send
        gets QUEUED (see _send_now), so this degraded prefill is the only thing
        compose loses, not the send itself."""
        cached = self.app._threads_cache.get(self.thread_id, {})
        subj = cached.get("subject", "")
        if self.mode == "forward":
            return "", "", subj if subj.lower().startswith("fwd:") else "Fwd: " + subj
        subject = subj if subj.lower().startswith("re:") else "Re: " + subj
        if self.mode == "reply_all":
            self.notify("Replying from cached info; Cc'd recipients may be missing.",
                        severity="warning")
        return cached.get("from", ""), "", subject

    def _prefill_from_server(self) -> tuple[str, str, str]:
        """(to, cc, subject) from a live threads().get(), and inserts the reply
        quote into #c-body. Raises on any network/auth error — the caller falls
        back to the cached prefill rather than letting it crash the app."""
        cc = ""
        g = self.svc["gmail"]
        # format="full" (not "metadata") so the last message's payload
        # carries body parts too -- needed below for quote_on_reply.
        th = g.users().threads().get(userId="me", id=self.thread_id, format="full").execute()
        last = th["messages"][-1]
        hdrs = {h["name"].lower(): h["value"] for h in last.get("payload", {}).get("headers", [])}
        subj = hdrs.get("subject", "")
        if self.mode == "reply":
            to = hdrs.get("from", "")
            subject = subj if subj.lower().startswith("re:") else "Re: " + subj
        elif self.mode == "reply_all":
            to = hdrs.get("from", "")
            cc = ", ".join(filter(None, [hdrs.get("to", ""), hdrs.get("cc", "")]))
            subject = subj if subj.lower().startswith("re:") else "Re: " + subj
        else:  # forward
            to = ""
            subject = subj if subj.lower().startswith("fwd:") else "Fwd: " + subj
        if self.mode in ("reply", "reply_all") and self.app.settings.quote_on_reply:
            quote = gauth.quote_for_reply(last.get("payload", {}), hdrs.get("from", ""), hdrs.get("date", ""))
            body_area = self.query_one("#c-body", TextArea)
            body_area.text = "\n\n" + quote
            # Cursor at the very top, above the quote, so typing starts
            # the new reply text where the roadmap item asked for it --
            # "below" the reply text, i.e. above the quote block.
            body_area.move_cursor((0, 0))
        return to, cc, subject

    def on_mount(self) -> None:
        if self.mode == "new":
            to, cc, subject = self._prefill_to, "", ""
        elif not self.app._online:
            # Offline: no threads().get() round trip to block on.
            to, cc, subject = self._prefill_from_cache()
        else:
            # Online but a live fetch can still fail (a transient SSL/auth
            # hiccup while the app's own background reconnect is in flight, an
            # expired token) — same class of failure ThreadModal._fetch_thread
            # guards against. Falling back to the cached prefill keeps a bad
            # round trip from crashing the whole app in on_mount.
            try:
                to, cc, subject = self._prefill_from_server()
            except Exception as e:
                self.notify(f"Couldn't load the thread ({e}); using cached info.",
                            severity="warning")
                to, cc, subject = self._prefill_from_cache()
        self.query_one("#c-to").value = to
        self.query_one("#c-cc").value = cc
        self.query_one("#c-subject").value = subject
        if self.mode == "new" and not to:
            self.query_one("#c-to", Input).focus()

    # ---- Compose's To-field fuzzy autocomplete (P1 M5) ----
    # Filters self.app._contacts_cache client-side (rapidfuzz) — never
    # re-queries Google. No-ops silently if contacts were never fetched
    # (empty cache, e.g. the People API scope is missing — see gauth.
    # list_contacts / the Contacts tab's error handling), per the ROADMAP
    # item's instruction not to error here.
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "c-to":
            self._update_to_suggestions(event.value)

    def _update_to_suggestions(self, value: str) -> None:
        suggestions = self.query_one("#c-to-suggestions", ListView)
        contacts = getattr(self.app, "_contacts_cache", None) or []
        # Only fuzzy-match the fragment after the last comma, so
        # "alice@x.com, bo" still matches "bo" against contacts rather than
        # the whole accumulated To string.
        fragment = value.rsplit(",", 1)[-1].strip()
        matches = _fuzzy_filter_contacts(contacts, fragment, limit=6) if fragment else []
        suggestions.clear()
        if not matches:
            suggestions.add_class("hidden")
            return
        # Per-render unique ids (not the contact's resource_name) sidestep
        # the DuplicateIds trap from AGENTS.md's ListView.clear() NOTE
        # entirely: clear() above isn't awaited (this list is small/
        # ephemeral, unlike the mail/news/contacts lists), so a fresh
        # keystroke's ids must never collide with a still-being-removed
        # prior render's ids.
        self._suggest_render_gen = getattr(self, "_suggest_render_gen", 0) + 1
        gen = self._suggest_render_gen
        for i, c in enumerate(matches):
            name, addr = c.get("name"), c.get("email", "")
            label = f"{name}  <{addr}>" if name else addr
            item = ListItem(Label(label, markup=False), id=f"sug-{gen}-{i}")
            item.contact_email = c.get("email", "")
            suggestions.append(item)
        suggestions.remove_class("hidden")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "c-to-suggestions":
            return
        email = getattr(event.item, "contact_email", "")
        if not email:
            return
        to_input = self.query_one("#c-to", Input)
        current = to_input.value
        if "," in current:
            prefix = current.rsplit(",", 1)[0].strip()
            to_input.value = f"{prefix}, {email}"
        else:
            to_input.value = email
        self.query_one("#c-to-suggestions", ListView).add_class("hidden")
        to_input.focus()
        to_input.cursor_position = len(to_input.value)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "cancel":
            if self._countdown_timer is not None:
                self._cancel_countdown()
            else:
                self.dismiss(None)
            return
        if e.button.id == "send":
            self._try_send()
            return
        if e.button.id == "save-draft":
            self._save_draft()
            return
        if e.button.id == "c-attach-btn":
            self._add_attachment()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id == "c-attach":
            self._add_attachment()

    def _add_attachment(self) -> None:
        inp = self.query_one("#c-attach", Input)
        raw = inp.value.strip()
        if not raw:
            return
        p = Path(raw).expanduser()
        if not p.is_file():
            self.notify(f"No such file: {raw}", severity="warning")
            return
        self._attachments.append(str(p))
        inp.value = ""
        self._update_attach_list()

    def _update_attach_list(self) -> None:
        widget = self.query_one("#c-attach-list", Static)
        if not self._attachments:
            widget.update("")
            return
        names = ", ".join(Path(a).name for a in self._attachments)
        prefix = "Attachments: " if self.app.settings.ascii_mode else "📎 "
        widget.update(f"{prefix}{len(self._attachments)}: {names}")

    def _try_send(self) -> None:
        if self._countdown_timer is not None:
            return  # already counting down
        if not self.query_one("#c-to").value.strip():
            return
        self._start_send_countdown()

    def _start_send_countdown(self) -> None:
        self._countdown_remaining = self.SEND_COUNTDOWN_SECONDS
        self.query_one("#c-to").disabled = True
        self.query_one("#c-cc").disabled = True
        self.query_one("#c-bcc").disabled = True
        self.query_one("#c-subject").disabled = True
        self.query_one("#c-body").disabled = True
        self.query_one("#c-attach").disabled = True
        self.query_one("#c-attach-btn", Button).disabled = True
        self.query_one("#send", Button).disabled = True
        self.query_one("#save-draft", Button).disabled = True
        self._update_countdown_label()
        self._countdown_timer = self.set_interval(1.0, self._countdown_tick)

    def _countdown_tick(self) -> None:
        self._countdown_remaining -= 1
        if self._countdown_remaining <= 0:
            self._countdown_timer.stop()
            self._countdown_timer = None
            self._send_now()
        else:
            self._update_countdown_label()

    def _update_countdown_label(self) -> None:
        self.query_one("#send-countdown", Static).update(
            f"Sending in {self._countdown_remaining}… (Cancel or Esc to stop)"
        )

    def _reenable_fields(self) -> None:
        self.query_one("#c-to").disabled = False
        self.query_one("#c-cc").disabled = False
        self.query_one("#c-bcc").disabled = False
        self.query_one("#c-subject").disabled = False
        self.query_one("#c-body").disabled = False
        self.query_one("#c-attach").disabled = False
        self.query_one("#c-attach-btn", Button).disabled = False
        self.query_one("#send", Button).disabled = False
        self.query_one("#save-draft", Button).disabled = False
        self.query_one("#send-countdown", Static).update("")

    def _cancel_countdown(self) -> None:
        self._countdown_timer.stop()
        self._countdown_timer = None
        self._reenable_fields()

    def _fields(self) -> tuple[str, str, str, str, str]:
        """Current (to, cc, bcc, subject, body) from the form — shared by the
        send and save-draft paths."""
        return (
            self.query_one("#c-to").value.strip(),
            self.query_one("#c-cc").value.strip(),
            self.query_one("#c-bcc").value.strip(),
            self.query_one("#c-subject").value.strip(),
            self.query_one("#c-body").text,
        )

    def _send_now(self) -> None:
        to, cc, bcc, subject, body = self._fields()
        atts = list(self._attachments)
        if not self.app._online:
            # Queue instead of attempting a call with no network to make it
            # over — see GoogleTUI._enqueue_mutation /
            # _replay_pending_mutations_thread for the replay-on-reconnect
            # side of this. Attachments are stored as file paths (re-read at
            # replay time — a since-moved file just fails that one send then).
            if self.mode == "forward":
                mutation = {"type": "forward", "thread_id": self.thread_id, "to": to,
                            "cc": cc, "bcc": bcc, "subject": subject, "body": body,
                            "attachments": atts}
            elif self.mode == "new":
                mutation = {"type": "new", "to": to, "cc": cc, "bcc": bcc,
                            "subject": subject, "body": body, "attachments": atts}
            else:  # reply / reply_all
                mutation = {"type": self.mode, "thread_id": self.thread_id, "to": to,
                            "cc": cc, "bcc": bcc, "subject": subject, "body": body,
                            "attachments": atts}
            self.app._enqueue_mutation(mutation)
            self.app.notify("Offline — queued, will send once reconnected.")
            self.dismiss("queued")
            return
        try:
            if self.mode == "forward":
                gauth.forward(self.svc, self.thread_id, to, body_prefix=body + "\n",
                              cc=cc or None, bcc=bcc or None, subject=subject or None,
                              attachments=atts or None)
            elif self.mode == "new":
                gauth.send_message(self.svc, to=to, subject=subject, body=body,
                                   cc=cc or None, bcc=bcc or None, attachments=atts or None)
            else:
                # Pass the raw field values (not `or None`): the compose form
                # is authoritative for a reply, so an explicitly-cleared Cc
                # must stay cleared rather than falling back to reply_to's
                # header-derived recipients (None is what triggers that
                # fallback — only the offline-replay path relies on it).
                gauth.reply_to(self.svc, self.thread_id, body,
                               reply_all=(self.mode == "reply_all"),
                               to=to, cc=cc, bcc=bcc, subject=subject,
                               attachments=atts or None)
        except Exception as e:
            # Previously uncaught here — an exception from a set_interval
            # timer callback (this runs via _countdown_tick) would otherwise
            # propagate to the App's unhandled-exception handler instead of
            # just failing this one send.
            self.app.notify(f"Send failed: {e}", severity="error")
            self._reenable_fields()
            return
        self.dismiss("sent")

    def _save_draft(self) -> None:
        """Save the current form as a Gmail draft instead of sending. Reply/
        reply-all drafts carry the thread_id so Gmail files them in-thread;
        a forward starts a fresh conversation, so it doesn't. Offline this
        queues just like a send does."""
        if self._countdown_timer is not None:
            return  # a send is already counting down; don't also draft it
        to, cc, bcc, subject, body = self._fields()
        atts = list(self._attachments)
        thread_id = self.thread_id if self.mode in ("reply", "reply_all") else None
        if not self.app._online:
            self.app._enqueue_mutation({
                "type": "draft", "to": to, "cc": cc, "bcc": bcc,
                "subject": subject, "body": body, "thread_id": thread_id,
                "attachments": atts})
            self.app.notify("Offline — queued, will save the draft once reconnected.")
            self.dismiss("queued")
            return
        try:
            gauth.create_draft(self.svc, to=to, subject=subject, body=body,
                               cc=cc or None, bcc=bcc or None, thread_id=thread_id,
                               attachments=atts or None)
        except Exception as e:
            self.app.notify(f"Save draft failed: {e}", severity="error")
            return
        self.app.notify("Draft saved")
        self.dismiss("draft")

    def on_key(self, e) -> None:
        if e.key == "ctrl+enter":
            self._try_send()
            return
        if e.key == "escape":
            suggestions = self.query_one("#c-to-suggestions", ListView)
            if "hidden" not in suggestions.classes:
                suggestions.add_class("hidden")
            elif self._countdown_timer is not None:
                self._cancel_countdown()
            else:
                self.dismiss(None)


class EventModal(ModalScreen):
    """Appointment detail. The description is routed through
    ``render.parse_feed_entry`` into a ``DocumentView`` (ROADMAP P4,
    2026-07-19) instead of being interpolated raw into the fixed-fields
    ``Static`` above it — Google Calendar's rich-text event editor often
    produces HTML descriptions, and this also lights up Markdown
    descriptions for free, same `parse_feed_entry` entry point used by
    ThreadModal/NewsEntryModal. `#ev-desc #doc-title` is hidden via CSS:
    the Summary line above already names the event, so `DocumentView`'s own
    auto-title bar (which would show "(untitled)" for the common case of a
    plain-text description with no `#`-heading) would just be redundant/ugly
    noise here.
    """

    def __init__(self, event: dict):
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        with Container(id="ev-box", classes="pane"):
            yield Label("APPOINTMENT DETAIL", classes="pane-title-text")
            yield Static(id="ev-detail")
            yield DocumentView(id="ev-desc")
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        e = self.event
        start = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
        end = _fmt_date(e.get("end", {}).get("dateTime") or e.get("end", {}).get("date", ""))
        det = (f"Summary: {e.get('summary','')}\n"
               f"Start:   {start}\nEnd:     {end}\n"
               f"Location:{e.get('location','')}\n"
               f"Link:    {e.get('htmlLink','')}")
        self.query_one("#ev-detail").update(det)
        doc = render.parse_feed_entry("", e.get("description", "") or "",
                                       ascii_mode=self.app.settings.ascii_mode)
        self.query_one("#ev-desc", DocumentView).document = doc

    def on_button_pressed(self, e):
        self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape":
            self.dismiss(None)


class CreateEventModal(ModalScreen):
    """New-event creation form (P2, 2026-07-15 — Calendar was read-only
    before this).

    A NEW modal, not an EventModal "create mode" the way ComposeModal
    gained `mode == "new"` (P1 M5): ComposeModal's reply/reply_all/
    forward/new modes all share the exact same three widgets (`#c-to`,
    `#c-subject`, `#c-body`) and only change what pre-fills them, so
    folding "new" into the existing class cost nothing. EventModal's VIEW
    is a single read-only `Static` detail block with no input widgets at
    all — reusing it for creation would mean composing every one of this
    form's Input/Switch widgets even for the common view path and hiding
    them with CSS, just to share a "Close" button. Not worth it; a
    separate class with its own compose() is simpler and matches this
    app's other minimal `.pane`-Container modals (ContactModal,
    NewsEntryModal).

    Date/time input is plain-text `Input`s (`YYYY-MM-DD` / `HH:MM`, 24h),
    same "no native picker widget" precedent as the Navigation tab's
    origin/destination address inputs — Textual has no built-in date
    picker, and this app doesn't add one from scratch for a single form.
    An all-day `Switch` disables the two time inputs rather than hiding
    them, so a mis-tap doesn't lose already-typed times.
    """

    def __init__(self, svc, default_date: dt.date, default_title: str = "",
                 description: str = ""):
        super().__init__()
        self.svc = svc
        self.default_date = default_date
        # Prefilled by the Email → Event flow (subject as title, a link +
        # snippet back to the thread as the event description); empty for the
        # plain New-Event path.
        self.default_title = default_title
        self.description = description
        self._submitting = False

    def compose(self) -> ComposeResult:
        with Container(id="ce-box", classes="pane"):
            yield Label("NEW EVENT", classes="pane-title-text")
            yield Input(placeholder="Title", id="ce-title")
            with Horizontal(classes="btnrow"):
                yield Label("All-day")
                yield Switch(id="ce-allday")
            yield Input(placeholder="Date (YYYY-MM-DD)", id="ce-date")
            with Horizontal(classes="btnrow"):
                yield Input(placeholder="Start (HH:MM, 24h)", id="ce-start-time")
                yield Input(placeholder="End (HH:MM, 24h)", id="ce-end-time")
            with Horizontal(classes="btnrow"):
                yield Button("Create", id="ce-create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#ce-date", Input).value = self.default_date.isoformat()
        if self.default_title:
            self.query_one("#ce-title", Input).value = self.default_title
        # 9-10am is just a sensible default block, not tied to "now" — the
        # user is very likely to change it, but an empty/zeroed field would
        # be a worse starting point than a plausible one-hour meeting.
        self.query_one("#ce-start-time", Input).value = "09:00"
        self.query_one("#ce-end-time", Input).value = "10:00"
        self.query_one("#ce-title", Input).focus()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "ce-allday":
            self.query_one("#ce-start-time", Input).disabled = event.value
            self.query_one("#ce-end-time", Input).disabled = event.value

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "cancel":
            self.dismiss(None)
        elif e.button.id == "ce-create":
            self._try_create()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        self._try_create()

    def _try_create(self) -> None:
        if self._submitting:
            return  # a create is already in flight -- avoid a double-tap
                     # firing two real inserts against Brad's live calendar
        title = self.query_one("#ce-title", Input).value.strip()
        if not title:
            self.notify("Title is required", severity="warning")
            return
        date_str = self.query_one("#ce-date", Input).value.strip()
        try:
            day = dt.date.fromisoformat(date_str)
        except ValueError:
            self.notify("Date must be YYYY-MM-DD", severity="warning")
            return
        all_day = self.query_one("#ce-allday", Switch).value
        if all_day:
            start: object = day
            # Calendar's all-day `end.date` is EXCLUSIVE (a one-day event
            # spans [date, date+1)), unlike a timed event's end -- without
            # +1 here, a "today, all day" event would show as zero-length.
            end: object = day + dt.timedelta(days=1)
        else:
            start_str = self.query_one("#ce-start-time", Input).value.strip()
            end_str = self.query_one("#ce-end-time", Input).value.strip()
            try:
                start_time = dt.datetime.strptime(start_str, "%H:%M").time()
            except ValueError:
                self.notify("Start time must be HH:MM", severity="warning")
                return
            end_time = None
            if end_str:
                try:
                    end_time = dt.datetime.strptime(end_str, "%H:%M").time()
                except ValueError:
                    self.notify("End time must be HH:MM", severity="warning")
                    return
            # Attach the local timezone -- the user typed a wall-clock time
            # meaning "local time here", and Calendar's API needs a UTC-
            # offset-bearing dateTime (or an explicit timeZone field, which
            # create_event() deliberately omits) to place the event
            # correctly rather than silently treating it as UTC. config.
            # toml's `timezone` overrides OS-local if set (app_config.py).
            local_tz = self.app.app_config.tzinfo or dt.datetime.now().astimezone().tzinfo
            start = dt.datetime.combine(day, start_time, tzinfo=local_tz)
            end = (dt.datetime.combine(day, end_time, tzinfo=local_tz)
                   if end_time is not None else start + dt.timedelta(hours=1))
            if end <= start:
                self.notify("End time must be after start time", severity="warning")
                return
        self._submitting = True
        if not self.app._online:
            # Offline: queue with a temp id instead of blocking. The base
            # screen's reconcile-at-render overlay shows it immediately; a
            # reconnect replays the real insert (see _enqueue_event_create /
            # _replay_one_mutation's create_event branch).
            self.app._enqueue_event_create(title, start, end, all_day,
                                           description=self.description)
            self.dismiss("queued")
            return
        self.run_worker(lambda: self._create_thread(title, start, end, all_day),
                         thread=True, exclusive=True, group="event-create")

    def _create_thread(self, title: str, start, end, all_day: bool) -> None:
        try:
            gauth.create_event(self.svc, title, start, end, all_day=all_day,
                               description=self.description)
        except Exception as ex:
            self.app.call_from_thread(self._create_failed, f"Create event failed: {ex}")
            return
        self.app.call_from_thread(self.dismiss, True)

    def _create_failed(self, msg: str) -> None:
        self._submitting = False
        self.notify(msg, severity="error")

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class EmailToTaskModal(ModalScreen):
    """Turn the highlighted email thread into a Google Task (ROADMAP:
    "create task from email"). Prefills the title from the subject and the
    notes with a link + snippet back to the thread; a Select picks which task
    list to add to (defaulting to the first). Mirrors CreateEventModal's
    submit/queue-offline shape — there was no standalone task-create modal
    before this (tasks were only created as subtasks inside TaskModal)."""

    def __init__(self, svc, tasklists: list[dict], default_title: str = "",
                 notes: str = ""):
        super().__init__()
        self.svc = svc
        self.tasklists = tasklists
        self.default_title = default_title
        self.notes = notes
        self._submitting = False

    def compose(self) -> ComposeResult:
        with Container(id="ett-box", classes="pane"):
            yield Label("EMAIL → TASK", classes="pane-title-text")
            yield Input(placeholder="Task title", id="ett-title")
            first = self.tasklists[0]["id"] if self.tasklists else None
            yield Select([(tl["title"], tl["id"]) for tl in self.tasklists],
                         id="ett-list", value=first, allow_blank=False)
            yield TextArea(id="ett-notes")
            with Horizontal(classes="btnrow"):
                yield Button("Create", id="ett-create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#ett-title", Input).value = self.default_title
        self.query_one("#ett-notes", TextArea).text = self.notes
        self.query_one("#ett-title", Input).focus()

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "cancel":
            self.dismiss(None)
        elif e.button.id == "ett-create":
            self._try_create()

    def _try_create(self) -> None:
        if self._submitting:
            return
        title = self.query_one("#ett-title", Input).value.strip()
        if not title:
            self.notify("Title is required", severity="warning")
            return
        list_id = self.query_one("#ett-list", Select).value
        if not list_id:
            self.notify("No task list to add to", severity="warning")
            return
        notes = self.query_one("#ett-notes", TextArea).text.strip() or None
        self._submitting = True
        if not self.app._online:
            # Offline: queue with a temp id (same reconcile-at-render overlay
            # the offline subtask-create path uses) instead of blocking.
            self.app._enqueue_task_create(list_id, title, parent=None, notes=notes)
            self.dismiss("queued")
            return
        self.run_worker(lambda: self._create_thread(list_id, title, notes),
                         thread=True, exclusive=True, group="task-create")

    def _create_thread(self, list_id: str, title: str, notes: str | None) -> None:
        try:
            gauth.create_task(self.svc, list_id, title, notes=notes)
        except Exception as ex:
            self.app.call_from_thread(self._failed, f"Create task failed: {ex}")
            return
        self.app.call_from_thread(self.dismiss, "created")

    def _failed(self, msg: str) -> None:
        self._submitting = False
        self.notify(msg, severity="error")

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class TaskModal(ModalScreen):
    """Task detail + subtasks (P2, 2026-07-15 — was read-only-nothing before:
    the old version didn't show subtasks at all, despite ROADMAP.md's stale
    claim that it did; see CHANGELOG).

    Notes are routed through ``render.parse_feed_entry`` into a
    ``DocumentView`` (ROADMAP P4, 2026-07-19), same rationale/pattern as
    ``EventModal``'s description — HTML/Markdown-aware rendering instead of
    raw text interpolation, with `#tk-desc #doc-title` hidden via CSS since
    the Title line above already covers it.

    `all_tasks` is the app's full `self._tasks_cache` (every tasklist
    combined) — subtasks are just plain tasks tagged with a `parent` field
    (see `_child_tasks`), so no extra gauth call is needed to find them at
    open time; only a mutation (add/toggle/delete) round-trips to Google, via
    `self.run_worker(..., thread=True)` same as every other gauth call in
    this app (AGENTS.md §2). Every mutation both re-renders THIS modal's own
    subtask list right away AND sets `self._mutated = True`; the Tasks
    pane's flat list (which shows subtasks as ordinary rows) is refreshed
    only once this modal actually DISMISSES, via `dismiss(self._mutated)` +
    the pusher's `_on_task_modal_result` callback calling
    `self.app._refresh_all_thread()` — deliberately NOT while this modal is
    still on top: `_refresh_all_thread` ends up in `_apply_mail_data`, which
    does `self.query_one("#task-list")` etc, and `App.query_one` resolves
    against `self.screen` (the CURRENTLY ACTIVE screen — see AGENTS.md's
    NOTE on query_one and screens), which would be THIS modal, not the base
    screen `#task-list` actually lives on, and raises `NoMatches`. Confirmed
    empirically while building this feature (see CHANGELOG). Mirrors
    `_on_compose_result`'s existing `if result == "sent": run_worker(...)`
    pattern for the exact same reason.
    """

    def __init__(self, svc, task: dict, all_tasks: list[dict] | None = None):
        super().__init__()
        self.svc = svc
        self.task_data = task
        self.subtasks = _child_tasks(task, all_tasks or [])
        self._mutated = False

    def compose(self) -> ComposeResult:
        with Container(id="tk-box", classes="pane"):
            yield Label("TASK DETAIL", classes="pane-title-text")
            yield Static(id="tk-detail")
            yield DocumentView(id="tk-desc")
            yield Label("Subtasks — Space: toggle complete, Delete: remove",
                        classes="muted")
            yield ListView(id="tk-subtask-list")
            with Horizontal(classes="btnrow"):
                yield Input(placeholder="New subtask title…", id="tk-subtask-input")
                yield Button("Add Subtask", id="tk-add-subtask")
            with Horizontal(classes="btnrow"):
                yield Button("Delete Task", id="tk-delete-task")
                yield Button("Close", id="close")

    async def on_mount(self) -> None:
        t = self.task_data
        det = (f"Title: {t.get('title','')}\nStatus: {t.get('status','')}\n"
               f"Due:   {t.get('due','')}")
        self.query_one("#tk-detail").update(det)
        doc = render.parse_feed_entry("", t.get("notes", "") or "",
                                       ascii_mode=self.app.settings.ascii_mode)
        self.query_one("#tk-desc", DocumentView).document = doc
        await self._render_subtasks()

    async def _render_subtasks(self) -> None:
        # `await`ed clear(), not fire-and-forget — ListView.clear() returns
        # an AwaitRemove that is NOT synchronous (see AGENTS.md's
        # ListView.clear() NOTE); this method can run twice in quick
        # succession (e.g. on_mount, then a mutation's reload), and a bare
        # `.clear()` + immediate `.extend()` intermittently raised
        # DuplicateIds because the second populate's items (same ids) were
        # inserted before the first populate's identically-IDed items had
        # actually finished being removed. Confirmed via the scratch test in
        # this change's verification pass.
        lst = self.query_one("#tk-subtask-list", ListView)
        await lst.clear()
        lst.extend(
            ListItem(
                Label(f"{_PENDING_MARK if s.get('_pending') else ''}"
                      f"{'[x]' if s.get('status') == 'completed' else '[ ]'} "
                      f"{s.get('title','')[:50]}"),
                id=_mk_id("sk", s["id"]))
            for s in self.subtasks
        )

    def _highlighted_subtask(self) -> dict | None:
        lst = self.query_one("#tk-subtask-list", ListView)
        if lst.highlighted_child is None:
            return None
        cid = lst.highlighted_child.id or ""
        if not cid.startswith("sk-"):
            return None
        sid = cid[3:]
        for s in self.subtasks:
            if s.get("id") == sid:
                return s
        return None

    # ---- input ----
    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "close":
            self.dismiss(self._mutated)
        elif e.button.id == "tk-add-subtask":
            self._add_subtask()
        elif e.button.id == "tk-delete-task":
            self._confirm_delete_task()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id == "tk-subtask-input":
            self._add_subtask()

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(self._mutated)
            return
        # Space/Delete only act on the subtask list, not while the "new
        # subtask" Input has focus (Space there must type a literal space).
        focused_id = self.focused.id if self.focused is not None else None
        if focused_id == "tk-subtask-input":
            return
        if e.key == "space":
            self._toggle_highlighted_subtask()
        elif e.key == "delete":
            self._delete_highlighted_subtask()

    # ---- add ----
    def _add_subtask(self) -> None:
        inp = self.query_one("#tk-subtask-input", Input)
        title = inp.value.strip()
        if not title:
            return
        inp.value = ""
        if not self.app._online:
            # Queue with a temp id and show it right away. The same temp id is
            # what a later offline delete cancels, and what a reconnect
            # replaces with the real subtask (see _enqueue_task_create).
            temp_id = self.app._enqueue_task_create(
                self.task_data["_list"], title, self.task_data["id"])
            self.subtasks = self.subtasks + [{
                "id": temp_id, "title": title, "status": "needsAction",
                "parent": self.task_data["id"], "notes": "",
                "_list": self.task_data["_list"], "_pending": True,
            }]
            self._mutated = True
            self.run_worker(self._render_subtasks(), exclusive=True, group="task-subtask")
            return
        self.run_worker(lambda: self._add_subtask_thread(title),
                         thread=True, exclusive=True, group="task-subtask")

    def _add_subtask_thread(self, title: str) -> None:
        try:
            gauth.create_task(self.svc, self.task_data["_list"], title, parent=self.task_data["id"])
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Add subtask failed: {e}", severity="error")
            return
        self._reload_subtasks()

    # ---- toggle complete ----
    def _toggle_highlighted_subtask(self) -> None:
        s = self._highlighted_subtask()
        if not s:
            return
        done = s.get("status") != "completed"
        if not self.app._online:
            if _is_temp_id(s["id"]):
                # Toggling a subtask that's itself a not-yet-synced offline
                # create: no server task to PATCH, so record the desired state
                # on the queued create instead of enqueuing a doomed toggle.
                self.app._toggle_pending_task(self.task_data["_list"], s["id"], done)
            else:
                self.app._enqueue_mutation({
                    "type": "toggle_task", "list_id": self.task_data["_list"],
                    "task_id": s["id"], "done": done,
                })
            # s IS the dict inside self.app._tasks_cache (self.subtasks is
            # built by _child_tasks filtering the SAME objects, not copies),
            # so this flips the checkbox in both this modal and the app's
            # Tasks pane once it re-renders (see _on_task_modal_result).
            s["status"] = "completed" if done else "needsAction"
            self._mutated = True
            self.run_worker(self._render_subtasks(), exclusive=True, group="task-subtask")
            self.app.notify("Offline — queued, will apply once reconnected.")
            return
        self.run_worker(lambda: self._toggle_subtask_thread(s["id"], done),
                         thread=True, exclusive=True, group="task-subtask")

    def _toggle_subtask_thread(self, subtask_id: str, done: bool) -> None:
        try:
            # Same call the Tasks pane's Space-to-toggle uses
            # (action_toggle_task -> gauth.set_task_status) — a subtask is
            # still just `tasks().patch` by id under the hood, so no new
            # helper is needed here.
            gauth.set_task_status(self.svc, self.task_data["_list"], subtask_id, done)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Subtask update failed: {e}", severity="error")
            return
        self._reload_subtasks()

    # ---- delete ----
    def _delete_highlighted_subtask(self) -> None:
        s = self._highlighted_subtask()
        if not s:
            return
        if not self.app._online:
            # Queue the delete (or, if this subtask is itself a queued offline
            # create, just cancel that create — _enqueue_task_delete decides).
            self.app._enqueue_task_delete(self.task_data["_list"], s["id"])
            self.subtasks = [x for x in self.subtasks if x.get("id") != s["id"]]
            self._mutated = True
            self.run_worker(self._render_subtasks(), exclusive=True, group="task-subtask")
            self.app.notify("Offline — queued, will delete once reconnected.")
            return
        # No confirm dialog for a subtask delete — consistent with this
        # app's existing no-confirm precedent (AGENTS.md §7) and low stakes
        # (one small item, trivially re-added). See _confirm_delete_task
        # below for why the top-level task DOES get a confirm.
        self.run_worker(lambda: self._delete_subtask_thread(s["id"]),
                         thread=True, exclusive=True, group="task-subtask")

    def _delete_subtask_thread(self, subtask_id: str) -> None:
        try:
            gauth.delete_task(self.svc, self.task_data["_list"], subtask_id)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Delete subtask failed: {e}", severity="error")
            return
        self._reload_subtasks()

    def _confirm_delete_task(self) -> None:
        # Unlike a subtask, deleting the TOP-LEVEL task also cascades to
        # every subtask under it server-side and closes this whole modal —
        # a lightweight confirm here is worth the one extra keypress even
        # though nothing else in this app confirms before a mutation. Still
        # confirmed offline (the queued delete cascades the same on replay).
        n = len(self.subtasks)
        msg = "Delete this task"
        msg += f" and its {n} subtask{'s' if n != 1 else ''}?" if n else "?"
        self.app.push_screen(ConfirmModal(msg), self._on_delete_task_confirm)

    def _on_delete_task_confirm(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        if not self.app._online:
            # Queue the delete (or cancel this task's own queued create if it's
            # a temp item), then close — the queued delete cascades to subtasks
            # server-side on replay, same as the online path.
            self.app._enqueue_task_delete(self.task_data["_list"], self.task_data["id"])
            self.app.notify("Offline — queued, will delete once reconnected.")
            self.call_after_refresh(lambda: self.dismiss(True))
            return
        self.call_after_refresh(self._start_delete_task)

    def _start_delete_task(self) -> None:
        self.run_worker(self._delete_task_thread, thread=True, exclusive=True, group="task-subtask")

    def _delete_task_thread(self) -> None:
        try:
            gauth.delete_task(self.svc, self.task_data["_list"], self.task_data["id"])
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Delete task failed: {e}", severity="error")
            return
        self._mutated = True
        # dismiss(True) here — NOT a direct self.app._refresh_all_thread()
        # call — is what lets the pusher's _on_task_modal_result do the
        # Tasks-pane refresh only after this modal is actually gone; see the
        # class docstring for why refreshing while still on top raises
        # NoMatches.
        self.app.call_from_thread(self.dismiss, True)

    # ---- shared reload ----
    def _reload_subtasks(self) -> None:
        # Runs on the worker thread the caller already started — re-fetch
        # this tasklist so self.subtasks reflects the mutation and hand the
        # render back to the main thread. Does NOT touch the app's Tasks
        # pane itself (see class docstring); that happens once this modal
        # dismisses, via self._mutated + _on_task_modal_result.
        try:
            fresh = gauth.list_tasks(self.svc, self.task_data["_list"])
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Refresh failed: {e}", severity="error")
            return
        for t in fresh:
            t["_list"] = self.task_data["_list"]
        self.subtasks = _child_tasks(self.task_data, fresh)
        self._mutated = True
        self.app.call_from_thread(self._render_subtasks)


class ContactModal(ModalScreen):
    """Contact detail (P1 M5) — modeled on EventModal/TaskModal's minimal
    `.pane` Container + Static detail + button row shape. Unlike those,
    it has a second button ("Compose Email") that dismisses with
    `("compose", email)` instead of `None`, which GoogleTUI._on_contact_
    modal_result relays into `_open_compose_new` (deferred one step via
    call_after_refresh — see the push_screen(callback) timing NOTE in
    AGENTS.md, since the callback fires before this screen is popped)."""

    def __init__(self, contact: dict):
        super().__init__()
        self.contact = contact

    def compose(self) -> ComposeResult:
        with Container(id="contact-box", classes="pane"):
            yield Label("CONTACT DETAIL", classes="pane-title-text")
            yield Static(id="contact-detail")
            with Horizontal(classes="btnrow"):
                yield Button("Compose Email", id="contact-compose")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        c = self.contact
        det = (f"Name:  {c.get('name','')}\n"
               f"Email: {c.get('email','')}\n"
               f"Phone: {c.get('phone','')}")
        self.query_one("#contact-detail").update(det)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "contact-compose":
            self.dismiss(("compose", self.contact.get("email", "")))
        else:
            self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class PendingMutationsModal(ModalScreen):
    """View + cancel the offline mutation queue (Settings -> General ->
    "View queued actions" — see GoogleTUI._enqueue_mutation /
    _replay_pending_mutations_thread / _cancel_mutation). `mutations` is the
    SAME dict object as GoogleTUI._pending_mutations, not a copy — cancelling
    an item here pops it from that dict directly (via app._cancel_mutation),
    so re-rendering afterward just re-reads self.mutations. Cancelling drops
    the queued item without ever sending it; it does NOT undo any optimistic
    local update the item already applied (see _cancel_mutation's docstring).
    """

    def __init__(self, mutations: dict[str, dict]):
        super().__init__()
        self.mutations = mutations

    def compose(self) -> ComposeResult:
        with Container(id="pending-mutations-box", classes="pane"):
            yield Label("QUEUED OFFLINE ACTIONS", classes="pane-title-text")
            yield Label("Delete: cancel selected  ·  Esc/Close: dismiss",
                       classes="muted")
            yield ListView(id="pending-mutations-list")
            yield Button("Close", id="close")

    async def on_mount(self) -> None:
        await self._render_queue()

    def _items(self) -> list[tuple[str, dict]]:
        return sorted(self.mutations.items(), key=lambda kv: kv[1].get("created_at", ""))

    async def _render_queue(self) -> None:
        # NOTE: not named `_render` — that shadows Widget._render() (the
        # internal method Textual's own compositor calls to get this
        # widget's paint content), which breaks rendering with an opaque
        # "'coroutine' object has no attribute 'render_strips'" crash.
        lst = self.query_one("#pending-mutations-list", ListView)
        await lst.clear()  # AwaitRemove, not synchronous — see AGENTS.md
        items = self._items()
        if not items:
            await lst.append(ListItem(Label("Nothing queued."), id="pm-empty"))
            return
        lst.extend(
            ListItem(
                Label(f"{m.get('created_at', '')[:16].replace('T', ' ')}  "
                      f"{_pending_mutation_summary(m)}"),
                id=_mk_id("pm", key))
            for key, m in items
        )

    def _highlighted_key(self) -> str | None:
        lst = self.query_one("#pending-mutations-list", ListView)
        if lst.highlighted_child is None:
            return None
        cid = lst.highlighted_child.id or ""
        if not cid.startswith("pm-") or cid == "pm-empty":
            return None
        return cid[3:]  # uuid4 keys are hex+dashes only, so _mk_id's
                        # sanitizing is a lossless round-trip here.

    def _cancel_highlighted(self) -> None:
        key = self._highlighted_key()
        if key is None:
            return
        self.app._cancel_mutation(key)
        self.run_worker(self._render_queue(), exclusive=True, group="pending-mutations-render")
        self.app.notify("Cancelled queued action.")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)
        elif e.key == "delete":
            self._cancel_highlighted()


class NewsEntryModal(ModalScreen):
    """Feed-entry detail — modeled closely on EventModal/TaskModal's shape
    (a `.pane` Container + Close button, pushed WITHOUT a callback since,
    unlike ThreadModal, there's no follow-up action to relay back to the app
    after Close). The entry body is parsed via `render.parse_feed_entry()`
    (M1) into a `Document` and shown in a `render.DocumentView` — the same
    widget the Browser tab uses — rather than a plain RichLog, so HTML-ish
    feed content (links, headings, paragraphs) renders properly instead of
    being dumped as raw markup.
    """

    def __init__(self, entry: dict):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        with Container(id="news-box", classes="pane"):
            yield Label("NEWS ENTRY", classes="pane-title-text")
            yield Static(id="news-entry-meta", classes="muted", markup=False)
            yield DocumentView(id="news-entry-doc")
        with Horizontal(classes="btnrow"):
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        e = self.entry
        feed_title = e.get("feed_title") or ""
        published = _fmt_date(e.get("published", ""))
        # feed_title is untrusted external text — see the markup=False NOTE
        # in _feed_list_item; #news-entry-meta is constructed with
        # markup=False in compose() below for exactly this reason (the flag
        # is set once at construction and honored by every later update()
        # call — there's no public setter to flip it after the fact).
        self.query_one("#news-entry-meta", Static).update(f"{feed_title}   {published}".strip())
        title = e.get("title") or "(untitled)"
        body = e.get("summary") or ""
        doc = render.parse_feed_entry(title, body, base_url=e.get("link", ""),
                                       ascii_mode=self.app.settings.ascii_mode)
        self.query_one("#news-entry-doc", DocumentView).document = doc

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class DayEventsModal(ModalScreen):
    def __init__(self, day: int, month: int, year: int, events: list[dict]):
        super().__init__()
        self.day, self.month, self.year, self.events = day, month, year, events

    def compose(self) -> ComposeResult:
        label = dt.date(self.year, self.month, self.day).strftime("%A, %B %d")
        with Container(id="day-events-box", classes="pane"):
            yield Label(f"EVENTS — {label}", classes="pane-title-text")
            yield ListView(id="day-events-list")
        yield Button("Close", id="close")

    def on_mount(self) -> None:
        lst = self.query_one("#day-events-list")
        for e in self.events:
            start = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
            lst.append(ListItem(Label(f"{start}  {e.get('summary','')[:50]}"), id=_mk_id("e", e["id"])))

    def on_list_view_selected(self, e: ListView.Selected) -> None:
        cid = e.item.id or ""
        if not cid.startswith("e-"):
            return
        eid = cid[2:]
        ev = next((x for x in self.events if x["id"] == eid), None)
        if ev:
            self.dismiss(None)
            self.app.push_screen(EventModal(ev))

    def on_button_pressed(self, e):
        self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape":
            self.dismiss(None)


class HelpModal(ModalScreen):
    def compose(self) -> ComposeResult:
        with Container(id="help-modal-box", classes="pane"):
            yield Label("KEYBOARD REFERENCE", classes="pane-title-text")
            # A fresh HelpModal is composed every time Ctrl+H is pressed (it's
            # never kept around and reused), so it always picks up the
            # CURRENT Settings.ascii_mode here — no live-refresh needed the
            # way the persistent #help-context/#help-global Statics need
            # _apply_ascii_mode() for.
            text = bindings.ascii_safe(HELP_TEXT) if self.app.settings.ascii_mode else HELP_TEXT
            yield Static(text, id="help-modal-text")
        yield Button("Close", id="close")

    def on_button_pressed(self, e):
        self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape":
            self.dismiss(None)


class HermesAskModal(ModalScreen):
    """Ctrl+K quick-ask popup (GoogleTUI.action_hermes_popup) -- lets you ask
    the configured AI provider (Settings -> AI Provider) a question from ANY
    tab without navigating to the Dashboard. Title and Input placeholder both
    name the provider (ask.display_name), matching the Dashboard Hermes
    card's own title (GoogleTUI._hermes_ask_title / _update_hermes_labels).

    Submission reuses GoogleTUI._hermes_submit/_hermes_thread wholesale --
    those already take the target RichLog as a parameter, so this modal's own
    #hermes-popup-log is a drop-in, no duplicated provider/context/LLM-calling
    logic. This is a FRESH conversation every time the modal opens -- it does
    not share history with the Dashboard card's #hermes-log, and nothing here
    is persisted once closed. Esc closes.
    """

    def compose(self) -> ComposeResult:
        with Container(id="hermes-popup-box", classes="pane"):
            yield Label(self.app._hermes_ask_title(), id="hermes-popup-title",
                       classes="pane-title-text")
            yield RichLog(id="hermes-popup-log", markup=False, wrap=True)
            yield Input(placeholder=f"Ask {ask.display_name(self.app.settings.ai_provider)} "
                                     f"about your Google stuff…",
                       id="hermes-popup-input")

    def on_mount(self) -> None:
        self.query_one("#hermes-popup-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "hermes-popup-input":
            self.app._hermes_submit(event, log=self.query_one("#hermes-popup-log", RichLog))

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class GeminiInputModal(ModalScreen):
    """Gemini status 10/11 ("input required") prompt: collects one line of
    text (masked if ``sensitive``, per status 11) to append as the retried
    request's query string. Dismisses with the entered string, or None if
    cancelled. See ``fetchers.GeminiInputRequired``.
    """

    def __init__(self, prompt: str, sensitive: bool = False):
        super().__init__()
        self._prompt = prompt
        self._sensitive = sensitive

    def compose(self) -> ComposeResult:
        with Container(id="gemini-input-box", classes="pane"):
            yield Label("INPUT REQUESTED", classes="pane-title-text")
            yield Static(self._prompt or "(no prompt given)")
            yield Input(password=self._sensitive, id="gemini-input-value")
        with Horizontal(classes="btnrow"):
            yield Button("Submit", id="submit")
            yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#gemini-input-value", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.dismiss(self.query_one("#gemini-input-value", Input).value)
        else:
            self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class BookmarkLabelModal(ModalScreen):
    """Ctrl+B (Browser tab): prompts for a label for the page being
    bookmarked, prefilled with its domain. Dismisses with the entered label
    (falling back to the prefilled default if submitted blank), or None if
    cancelled.
    """

    def __init__(self, default_label: str):
        super().__init__()
        self._default_label = default_label

    def compose(self) -> ComposeResult:
        with Container(id="bookmark-label-box", classes="pane"):
            yield Label("BOOKMARK THIS PAGE", classes="pane-title-text")
            yield Input(value=self._default_label, id="bookmark-label-value")
        with Horizontal(classes="btnrow"):
            yield Button("Save", id="save")
            yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#bookmark-label-value", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or self._default_label)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.dismiss(self.query_one("#bookmark-label-value", Input).value.strip() or self._default_label)
        else:
            self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class RemoteHostModal(ModalScreen):
    """Drive tab: add a new remote-filesystem source (FTP or SSH), or
    re-prompt for credentials after a login is refused
    (``drive_sources.RemoteAuthRequired``). Dismisses with ``(protocol, host,
    port, username, password, save: bool)``, or None if cancelled.
    Generalizes the former Browser-tab-only ``FtpLoginModal`` — FTP/SSH
    remote-filesystem browsing lives in the Drive tab now (source picker),
    reached both from there directly and via a Browser ftp://sftp:// address
    redirecting in. See drive_sources.py / ROADMAP.
    """

    def __init__(self, protocol: str = "ftp", host: str = "", port: int | None = None,
                 username: str = ""):
        super().__init__()
        self._protocol = protocol
        self._host = host
        self._port = port if port is not None else (
            drive_sources.FTP_DEFAULT_PORT if protocol == "ftp" else drive_sources.SSH_DEFAULT_PORT)
        self._username = username

    def compose(self) -> ComposeResult:
        with Container(id="remote-host-box", classes="pane"):
            yield Label("ADD REMOTE HOST", classes="pane-title-text")
            with RadioSet(id="remote-host-protocol"):
                yield RadioButton("FTP", value=(self._protocol == "ftp"), id="rb-remote-ftp")
                yield RadioButton("SSH (SFTP/SCP)", value=(self._protocol == "ssh"), id="rb-remote-ssh")
            yield Input(value=self._host, placeholder="Host", id="remote-host-host")
            yield Input(value=str(self._port), placeholder="Port", id="remote-host-port")
            yield Input(value=self._username, placeholder="Username (blank = anonymous, FTP only)",
                        id="remote-host-user")
            yield Input(placeholder="Password", password=True, id="remote-host-pass")
            with Horizontal(classes="settings-row"):
                yield Label("Save this host")
                yield Switch(value=True, id="remote-host-save")
        with Horizontal(classes="btnrow"):
            yield Button("Connect", id="connect")
            yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        target = "remote-host-host" if not self._host else "remote-host-user"
        self.query_one(f"#{target}", Input).focus()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "remote-host-protocol":
            return
        new_protocol = "ftp" if event.pressed.id == "rb-remote-ftp" else "ssh"
        old_default = str(drive_sources.SSH_DEFAULT_PORT if new_protocol == "ftp" else drive_sources.FTP_DEFAULT_PORT)
        new_default = str(drive_sources.FTP_DEFAULT_PORT if new_protocol == "ftp" else drive_sources.SSH_DEFAULT_PORT)
        port_input = self.query_one("#remote-host-port", Input)
        # Only auto-fill if the port still holds the OTHER protocol's
        # default (or is empty) -- don't clobber a port the user already
        # typed themselves.
        if port_input.value.strip() in ("", old_default):
            port_input.value = new_default
        self._protocol = new_protocol

    def _submit(self) -> None:
        host = self.query_one("#remote-host-host", Input).value.strip()
        if not host:
            self.notify("Enter a host.", severity="warning")
            return
        try:
            port = int(self.query_one("#remote-host-port", Input).value.strip())
        except ValueError:
            port = drive_sources.FTP_DEFAULT_PORT if self._protocol == "ftp" else drive_sources.SSH_DEFAULT_PORT
        username = self.query_one("#remote-host-user", Input).value.strip()
        password = self.query_one("#remote-host-pass", Input).value
        save = self.query_one("#remote-host-save", Switch).value
        self.dismiss((self._protocol, host, port, username, password, save))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect":
            self._submit()
        else:
            self.dismiss(None)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class ConfirmModal(ModalScreen):
    """Reusable Yes/No confirmation — used by the Browser tab for Gemini
    cross-host/cross-scheme redirect confirmation, but generic enough for
    any future yes/no prompt. Dismisses with a bool.
    """

    def __init__(self, message: str):
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box", classes="pane"):
            yield Label("CONFIRM", classes="pane-title-text")
            yield Static(self._message, id="confirm-text")
        with Horizontal(classes="btnrow"):
            yield Button("Yes", id="yes")
            yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(False)


def main():
    # Update check runs on the console, BEFORE the TUI takes over the screen —
    # it prints plain status lines, and a successful update has to re-exec (this
    # interpreter has already imported the old modules, so pulling new code
    # without restarting would claim an update that isn't the one running).
    # Skippable with --no-update, GOOGLE_TUI_NO_UPDATE=1, or the
    # check_for_updates setting; see updater.py for the safety rules.
    #
    # updater.restart()'s re-exec runs this same main() again from scratch, so
    # without the marker below, a successful update would print "Downloading
    # update... updated to vX" immediately followed by the RESTARTED process's
    # own "No update found, loading application" -- both individually correct
    # (two different process invocations, milliseconds apart) but genuinely
    # contradictory-looking as one block of terminal output (confirmed
    # confusing a user 2026-07-22). _GOOGLE_TUI_JUST_UPDATED, set right before
    # the re-exec, survives into the new process via os.execv's environment
    # inheritance and skips exactly one (already-redundant) recheck silently.
    if os.environ.pop("_GOOGLE_TUI_JUST_UPDATED", None):
        pass
    elif _update_check_enabled():
        try:
            if updater.check_for_update():
                os.environ["_GOOGLE_TUI_JUST_UPDATED"] = "1"
                updater.restart()  # does not return
        except Exception as e:
            # A broken update check must never be the reason you can't read your
            # mail. Report it and carry on into the app regardless.
            _logger.exception("Update check failed")
            print(f"Can't reach update server, skipping update check. ({e})", flush=True)
    try:
        GoogleTUI().run()
    except Exception:
        # GoogleTUI._handle_exception only sees crashes from message handlers
        # and workers, once Textual's event loop is already pumping. A crash
        # earlier than that -- e.g. hitting a half-settled editable-install
        # right after a relaunch (see updater.restart) -- propagates straight
        # out here uncaught, and previously just dumped a bare traceback to a
        # terminal that may already be mid alt-screen-teardown and vanished,
        # leaving zero record of what happened. Log it before it's gone.
        _logger.exception("App failed to start")
        raise


def _update_check_enabled() -> bool:
    if "--no-update" in sys.argv or os.environ.get("GOOGLE_TUI_NO_UPDATE") == "1":
        return False
    try:
        return load_settings().check_for_updates
    except Exception:
        return True


if __name__ == "__main__":
    main()
