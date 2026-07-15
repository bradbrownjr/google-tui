"""google-tui — multi-pane TUI for Gmail / Calendar / Tasks / Drive / Browser / News / Navigation / Hermes.

Top-level layout is seven full-width TABS in the blue bar: Mail, Calendar,
Drive, Browser, News, Navigation, Settings (Ctrl+1..7). The Mail tab holds
four PANES: Email, Events, Tasks, Hermes (Alt+1..4, or Alt+arrows to move
relatively). See AGENTS.md for the full keybinding reference and the
PANE_ADJACENCY rationale.
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
import urllib.parse
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import platformdirs
from rapidfuzz import fuzz
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Header, Input, Label, ListItem, ListView,
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
from . import render
from . import updater
from .render import DocumentView
from .cache import Cache, derive_key_from_passphrase, make_canary, new_salt, read_or_create_keyfile, verify_canary
from . import cache as cache_mod
from .settings import Settings, load_settings, save_settings

PANE_IDS = ["email", "events", "tasks", "hermes"]
PANE_TITLES = {
    "email": "EMAIL",
    "events": "EVENTS",
    "tasks": "TASKS",
    "hermes": "HERMES ASK",
}
# Email spans the full left column; Events/Tasks/Hermes stack in the right
# column. This is NOT a symmetric 2x2 grid, so left/right/up/down are an
# explicit adjacency map rather than arithmetic on a flat pane index.
PANE_ADJACENCY = {
    "email":  {"right": "events"},
    "events": {"left": "email", "down": "tasks"},
    "tasks":  {"left": "email", "up": "events", "down": "hermes"},
    "hermes": {"left": "email", "up": "tasks"},
}

TAB_ORDER = ["tab-mail", "tab-calendar", "tab-drive", "tab-browser", "tab-news", "tab-navigation", "tab-contacts",
             "tab-settings"]
SETTINGS_TAB_ORDER = ["settings-tab-general", "settings-tab-ai", "settings-tab-feeds", "settings-tab-search",
                       "settings-tab-navigation"]

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

_SUPERSCRIPT = {1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸"}

NAV_EXPORT_DIR = Path(platformdirs.user_documents_dir()) / "google-tui"

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

# Seconds the Drive cursor must sit still before we fetch a preview, and the
# Contacts/Email/Tasks search boxes must sit idle before we re-filter. All of
# these handlers fire on every keypress and each used to do its (expensive)
# work synchronously on each one; long enough to swallow a held-down arrow key
# or a fast typist, short enough to still feel immediate once you stop.
_DRIVE_PREVIEW_DEBOUNCE = 0.25
_CONTACTS_SEARCH_DEBOUNCE = 0.15
_EMAIL_SEARCH_DEBOUNCE = 0.15
_TASKS_SEARCH_DEBOUNCE = 0.15
_EVENTS_SEARCH_DEBOUNCE = 0.15
_DRIVE_SEARCH_DEBOUNCE = 0.15
_NEWS_SEARCH_DEBOUNCE = 0.15

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


def _fmt_date(s: str) -> str:
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.strftime("%m/%d %I:%M%p")
    except Exception:
        return s


def _mk_id(prefix: str, raw: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)
    return f"{prefix}-{safe}"


def _feed_list_item(url: str) -> ListItem:
    """Build one row for Settings' feed-subscription ListView.

    Feed URLs are full of ``:``/``/`` characters that ``_mk_id`` sanitizes
    away, so the widget id alone can't be reversed back to the original URL
    (unlike, say, a Google Calendar event id). The raw URL is stashed as a
    plain attribute on the ``ListItem`` instead — Textual widgets are regular
    objects, so this is just attribute assignment, not a special API — and
    read back by ``_remove_selected_feed``.
    """
    # markup=False: Label parses its string as Textual/Rich console markup by
    # default (see AGENTS.md's TabPane-title NOTE for the flip side of this),
    # and a feed URL is arbitrary external input that could legitimately
    # contain "[" — left as markup, Textual's Content.from_markup() treats
    # "[anything]" as a style-tag span and silently drops the brackets
    # instead of displaying them literally. Confirmed `rich.markup.escape()`
    # doesn't reliably fix this (it didn't even touch "[Feed One]" — its
    # tag-detection regex doesn't consider a space tag-like — and
    # Content.from_markup() ate it anyway); markup=False sidesteps the
    # question entirely.
    item = ListItem(Label(url, markup=False), id=_mk_id("sf", url))
    item.feed_url = url
    return item


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
    ("tab-mail", "Mail", 1),
    ("tab-calendar", "Calendar", 2),
    ("tab-drive", "Drive", 3),
    ("tab-browser", "Browser", 4),
    ("tab-news", "News", 5),
    ("tab-navigation", "Navigation", 6),
    ("tab-contacts", "Contacts", 7),
    ("tab-settings", "Settings", 8),
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
    NAV_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = NAV_EXPORT_DIR / _nav_export_filename(result)
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


def _email_collapsed_line(th: dict, show_sender_address: bool = False) -> str:
    mark = "•" if th["unread"] else " "
    subj = th["subject"] or "(no subject)"
    frm = _format_sender(th["from"], show_sender_address)
    return f"{mark} {frm[:36]:<36} {subj[:60]}  ({th['count']})"


def _thread_expanded_text(th: dict, msgs: list[dict], show_sender_address: bool = False) -> str:
    """Space-expand preview for a multi-message thread: one line per message
    (From + a short body snippet), so a "(N)" thread actually shows all N
    messages inline instead of just the latest one's snippet."""
    lines = [_email_collapsed_line(th, show_sender_address)]
    for m in msgs:
        frm = _format_sender((m.get("from") or "").strip(), show_sender_address)
        snippet = (m.get("body") or "").strip().replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:80].rstrip() + "…"
        lines.append(f"    {frm[:36]:<36} {snippet}")
    return "\n".join(lines)


def _append_email_items(email_list, threads, show_sender_address: bool = False) -> None:
    # extend(), not append()-in-a-loop: ListView.append mounts ONE widget per
    # call (a mount + layout + repaint each), so an 80-thread inbox paid for 80
    # separate mount cycles. extend() batches the whole list into a single one.
    # Same reason every other list in this file builds its items first and
    # extends once.
    email_list.extend(
        ListItem(Label(_email_collapsed_line(th, show_sender_address)),
                 id=_mk_id("t", th["threadId"]))
        for th in threads
    )


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


def _append_task_items(task_list, tasks) -> None:
    # Same extend()-once rationale as _append_email_items above.
    task_list.extend(
        ListItem(
            Label(f"{'[x]' if t.get('status') == 'completed' else '[ ]'} {t.get('title','')[:50]}"),
            id=_mk_id("k", f"{t['_list']}-{t['id']}"))
        for t in tasks
    )


def _append_event_items(event_list, events) -> None:
    # Same extend()-once rationale as _append_email_items/_append_task_items
    # above.
    event_list.extend(
        ListItem(
            Label(f"{_fmt_date(e.get('start', {}).get('dateTime') or e.get('start', {}).get('date', ''))}"
                  f"  {e.get('summary','')[:40]}"),
            id=_mk_id("e", e["id"]))
        for e in events
    )


def _append_drive_items(drive_list, files, path: str) -> None:
    # Same extend()-once rationale as _append_email_items/_append_task_items/
    # _append_event_items above. The "up" row is NOT part of the filterable
    # file list — it's chrome, always present (except at "/") regardless of
    # what #drive-search's current query is, same as it always was before
    # search existed.
    items = []
    if path != "/":
        items.append(ListItem(Label("📂 .. (up)"), id="d-up"))
    for f in files:
        icon = "📁" if f["mimeType"] == "application/vnd.google-apps.folder" else "📄"
        items.append(ListItem(Label(f"{icon} {f['name'][:50]}"), id=_mk_id("d", f["id"])))
    drive_list.extend(items)


def _event_day(e: dict) -> int | None:
    s = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).day
    except Exception:
        return None


def _is_previewable(mime: str) -> bool:
    return mime.startswith(_PREVIEWABLE_PREFIXES) or mime in _PREVIEWABLE_EXTRA


# ---------------------------------------------------------------------------
# Browser tab glue (M2) — address classification + search-result linkifying.
# These live here (not render.py) because they're specific to this app's
# omnibox behavior / one opaque CLI's (hermes web search) output shape, not
# general protocol parsing. See ROADMAP M2 design notes.
# ---------------------------------------------------------------------------

_SCHEME_PREFIXES = ("http://", "https://", "gopher://", "gemini://")
_BARE_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+(:\d+)?(/\S*)?$"
)


def _classify_address(raw: str) -> tuple[str, str]:
    """Omnibox-style classification of Browser-tab address-bar input.

    -> (mode, target) where mode is 'http'|'gopher'|'gemini'|'search'.
    An explicit scheme always wins; a single dotted-word-with-no-space is
    treated as a bare domain and gets "https://" prepended; everything else
    (including any input containing a space) is a web search via
    ``fetchers.run_search``. ``search:`` is an explicit escape hatch for the
    rare case of wanting to search for literally "example.com".
    """
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return "http", raw
    if raw.startswith("gopher://"):
        return "gopher", raw
    if raw.startswith("gemini://"):
        return "gemini", raw
    if raw.startswith("search:"):
        return "search", raw[len("search:"):].strip()
    if " " not in raw and _BARE_DOMAIN_RE.match(raw):
        return "http", "https://" + raw
    return "search", raw


# Browser tab "new tab page" bookmarks — starter destinations covering all
# three non-search protocols this tab speaks (see fetchers.py). Shown as a
# button row under the address bar until the user navigates anywhere (first
# successful page load or search), then hidden for the rest of the session.
_BROWSER_BOOKMARKS = [
    ("Google", "https://www.google.com"),
    ("Wikipedia", "https://en.wikipedia.org"),
    ("Gopherpedia", "gopher://gopher.floodgap.com"),
    ("Gemini Protocol", "gemini://geminiprotocol.net/"),
]


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
    #main-tabs { height: 1fr; }
    #main-tabs > ContentTabs { height: 1; background: $primary; }
    #main-tabs > ContentTabs Underline { display: none; }
    #main-tabs > ContentTabs Tab { color: $text; }
    #main-tabs > ContentTabs Tab.-active { background: $accent; color: $text; text-style: bold; }
    #body { height: 1fr; }
    #left { width: 65%; }
    #right { width: 1fr; }
    .pane { height: 1fr; border: round $panel-darken-2; padding: 0 1; }
    .pane-active { border: round $accent; }
    .pane-title-row { height: 1; }
    .pane-title-text { text-style: bold; color: $accent; width: 1fr; }
    .pane-title-num { color: $text-muted; width: auto; }
    #email-label-select { height: 3; }
    #email-search, #tasks-search, #events-search { width: 1fr; }
    #email-list { height: 1fr; }
    #event-list, #task-list { height: 1fr; }
    #hermes-log { height: 1fr; border: round $panel-darken-1; }
    #hermes-input { dock: bottom; }
    .muted { color: $text-muted; }
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
    #drive-search { width: 1fr; }
    #browser-bar { height: 3; align: left middle; }
    #browser-mode { width: 10; color: $accent; text-style: bold; content-align: center middle; }
    #browser-url { width: 1fr; }
    #browser-status { width: auto; color: $text-muted; margin-left: 1; }
    #browser-bookmarks { height: 3; align: left middle; }
    #browser-bookmarks Button { min-width: 3; width: auto; height: 3; margin-right: 1; }
    #browser-doc { height: 1fr; border: round $panel-darken-1; padding: 0 1; }
    #news-search { width: 1fr; }
    #news-list { height: 1fr; }
    #nav-origin, #nav-destination { width: 1fr; margin-right: 1; }
    #nav-summary { color: $accent; text-style: bold; height: 1; margin: 1 0; }
    #nav-log { height: 1fr; border: round $panel-darken-1; }
    #thread-messages { height: 1fr; }
    #thread-search { margin-bottom: 1; }
    #thread-help { color: $text-muted; height: auto; margin-top: 1; }
    #labelpick-box { height: auto; max-height: 80%; }
    #labelpick-list { height: auto; max-height: 20; border: round $panel-darken-1; margin-bottom: 1; }
    .thread-msg-header { color: $text-muted; text-style: bold; margin-top: 1; border-bottom: solid $panel-darken-2; }
    #help-bar { height: auto; background: $panel; padding: 0 1; }
    #help-context { color: $text; }
    #help-global { color: $text-muted; }
    .settings-row { height: 3; align: left middle; }
    .settings-row Label { width: auto; margin-right: 2; }
    #settings-nous-key { width: 40; margin-right: 2; }
    .hidden { display: none; }
    #settings-key-method { height: auto; margin: 1 0; }
    #settings-cache-info { margin-top: 1; }
    #settings-feed-list { height: 8; border: round $panel-darken-1; margin-bottom: 1; }
    #settings-feed-url { width: 1fr; margin-right: 2; }
    #settings-google-cse-key, #settings-google-cse-id, #settings-searxng-url { width: 40; margin-right: 2; }
    #settings-google-group, #settings-searxng-group { height: auto; }
    #settings-routes-key { width: 40; margin-right: 2; }
    #contacts-search { width: 1fr; margin-right: 1; }
    #contacts-list { height: 1fr; }
    #c-to-suggestions { height: auto; max-height: 6; border: round $panel-darken-1; }
    #unlock-box { height: auto; }
    #onboarding-box { width: 90%; height: 80%; }
    /* The re-auth box grew a Copy URL / Save to file row; give it room to
       scroll rather than pushing the paste Input off the bottom of the screen
       on a short terminal. The URL itself is ~400 chars and must wrap, not be
       clipped — a clipped URL is an unusable URL. */
    #reauth-box { width: 90%; height: auto; max-height: 90%; overflow-y: auto; }
    #reauth-url { width: 1fr; height: auto; color: $accent; margin: 1 0; }
    #reauth-copy-help { height: auto; margin-bottom: 1; }
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
    #hermes-log.ascii-border { border: ascii $panel-darken-1; }
    #drive-list-col.ascii-border { border: ascii $panel-darken-1; }
    #drive-preview-col.ascii-border { border: ascii $panel-darken-1; }
    #browser-doc.ascii-border { border: ascii $panel-darken-1; }
    #nav-log.ascii-border { border: ascii $panel-darken-1; }
    #settings-feed-list.ascii-border { border: ascii $panel-darken-1; }
    #c-to-suggestions.ascii-border { border: ascii $panel-darken-1; }
    #drive-preview-meta.ascii-border { border-bottom: ascii $panel-darken-2; }
    .thread-msg-header.ascii-border { border-bottom: ascii $panel-darken-2; }

    /* Narrow-terminal responsive layout (P2, 2026-07-15) -- see the
       NARROW_WIDTH_THRESHOLD comment above for the breakpoint mechanism.

       Drive tab: STACK list-over-preview rather than hide either one. Both
       are genuinely useful at once even at 80 columns (the list to keep
       browsing, the preview's who/what/where/when + text to actually read
       something) and, unlike the Mail tab's four panes, there are only two
       of them, so a 60/40 height split still leaves each one usable in a
       25-row terminal -- hiding the preview would leave Drive as a bare
       filename list with no way to see what's selected without opening it.
    */
    Screen.-narrow #drive-body { layout: vertical; height: 1fr; }
    Screen.-narrow #drive-list-col { width: 1fr; height: 60%; }
    Screen.-narrow #drive-preview-col { width: 1fr; height: 1fr; }

    /* Mail tab: HIDE the inactive column instead of stacking. Email vs.
       Events/Tasks/Hermes is a 1-vs-3 split, and Events/Tasks/Hermes are
       already themselves stacked inside #right -- stacking a 4th thing
       (Email) on top would quarter an already-scarce 25 rows into
       unreadable slivers. Showing exactly ONE pane full width/full height
       (whichever is "active" -- Alt+1..4/Tab/arrows already track that via
       _focus_pane, this just also hides the rest when narrow) keeps the
       primary content dominant instead of squeezed. See
       GoogleTUI._apply_narrow_layout, which toggles this class. */
    .narrow-hidden { display: none; }
    /* #left/#right keep their normal 65%/1fr split (see the CSS block
       above) even when one of them is display:none'd by .narrow-hidden --
       display:none doesn't relinquish the width, it just leaves the other
       column's dead space empty. Whichever one IS visible needs to claim
       the full row. */
    Screen.-narrow #left, Screen.-narrow #right { width: 1fr; }
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
        self._tasklists = []
        now = dt.datetime.now()
        self._cal_year, self._cal_month = now.year, now.month
        self._cal_by_day: dict[int, list[dict]] = {}
        self._cal_week_cells: dict[tuple[int, int], list[dict]] = {}
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
        self._drive_files: list[dict] = []
        # Drive preview is debounced (see _drive_on_highlight) and memoised for
        # the session: highlighting a row costs a metadata round-trip + a file
        # download, so we neither fire one per arrow keypress nor re-fetch a
        # row the cursor has already visited.
        self._drive_preview_timer = None
        self._drive_preview_gen = 0
        self._drive_preview_cache: dict[str, tuple[str, str]] = {}
        self.settings: Settings = load_settings()
        self._current_label_id = self.settings.default_label_id
        # Full Gmail label list from the last _apply_labels call — backs both
        # the Email pane's folder Select and ThreadModal's "L" label picker.
        self._labels_cache: list[dict] = []
        self._cache: Cache | None = None
        self._online = False
        self._loading_modal: LoadingModal | None = None
        self._mail_apply_gen = 0
        self._drive_apply_gen = 0
        self._news_apply_gen = 0
        self._news_by_cid: dict[str, dict] = {}
        # Full combined-feed entry list from the last _apply_news_data call —
        # backs the News tab's search filter (Input#news-search) the same
        # way self._threads_cache/self._tasks_cache back Email/Tasks search.
        self._news_entries_cache: list[dict] = []
        self._browser_history: list[BrowserHistoryEntry] = []
        self._browser_hist_pos: int = -1
        self._browser_tofu: fetchers.GeminiTofuStore | None = None
        self._browser_started: bool = False
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
        # Full per-message fetch backing the Space-expand preview once a
        # thread has >1 message (see _toggle_thread_expand) — keyed by
        # thread_id, populated lazily on first expand, kept for the rest of
        # the session (naturally reset on app restart, same as the caches
        # above it).
        self._thread_full_cache: dict[str, list[dict]] = {}
        self._nav_last_result: "fetchers.RouteResult | None" = None
        # Contacts tab (P1 M5) — lazy-fetched: contacts change rarely, so
        # (unlike mail/calendar/drive/news) they're NOT pulled on every
        # startup/Ctrl+R, only once, the first time the Contacts tab is
        # activated (see on_tabbed_content_tab_activated). ComposeModal's
        # To-field autocomplete reads self._contacts_cache directly.
        self._contacts_cache: list[dict] = []
        self._contacts_by_cid: dict[str, dict] = {}
        self._contacts_apply_gen = 0
        self._contacts_fetch_started = False
        self._contacts_search_timer = None
        self._email_search_timer = None
        self._tasks_search_timer = None
        self._tasks_apply_gen = 0
        self._events_search_timer = None
        self._events_apply_gen = 0
        self._drive_search_timer = None
        self._drive_search_apply_gen = 0
        self._news_search_timer = None
        self._news_search_apply_gen = 0
        # F2 hands the mouse back to the terminal so its native click-drag
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

    def notify(self, message: str, *, title: str = "", severity: str = "information",
               timeout: float | None = None, markup: bool = True) -> None:
        """Every notify() call in this app — including from ModalScreens,
        which proxy through Widget.notify -> self.app.notify — funnels
        through here, so this is the one place that can catch all of them
        without touching 20+ call sites. Toasts are ephemeral (easy to miss,
        and worse when a bug fires the same one twice); error/warning ones
        also get a durable line in LOG_FILE so a missed toast isn't gone
        for good.
        """
        if severity in ("error", "warning"):
            _logger.log(logging.ERROR if severity == "error" else logging.WARNING, message)
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

    # ---- pane helpers (Mail tab) ----
    def _pane_title_row(self, text: str, num: int) -> Horizontal:
        return Horizontal(
            Label(text, classes="pane-title-text"),
            Label(str(num), classes="pane-title-num"),
            classes="pane-title-row",
        )

    def _main_tabs(self) -> TabbedContent:
        return self.query_one("#main-tabs", TabbedContent)

    def _focus_pane(self, idx: int) -> None:
        self.active = idx % len(PANE_IDS)
        for pid in PANE_IDS:
            try:
                self.query_one(f"#{pid}").remove_class("pane-active")
            except Exception:
                pass
        pane_id = PANE_IDS[self.active]
        try:
            self.query_one(f"#{pane_id}").add_class("pane-active")
        except Exception:
            pass
        targets = {
            "email": "#email-list",
            "events": "#event-list",
            "tasks": "#task-list",
            "hermes": "#hermes-input",
        }
        try:
            self.query_one(targets[pane_id]).focus()
        except Exception:
            pass
        self._apply_narrow_layout()
        self._update_help_bar()

    # ---- narrow-terminal responsive layout (P2, 2026-07-15) ----
    # See the NARROW_WIDTH_THRESHOLD / HORIZONTAL_BREAKPOINTS comments above
    # for the overall mechanism. This method handles only the part CSS
    # can't: which single Mail-tab pane should be visible depends on
    # runtime state (self.active), not just terminal width.
    def _apply_narrow_layout(self) -> None:
        """When narrow, show only the active Mail pane (Email, OR the
        Events/Tasks/Hermes column) full width/full height; when not
        narrow, restore the normal Email+stack side-by-side layout. Safe to
        call any time (pane switch, resize, startup) — a no-op query
        failure (e.g. called before compose() has mounted anything) is
        swallowed the same way _apply_ascii_mode's widget lookups are.
        """
        try:
            left = self.query_one("#left")
            right = self.query_one("#right")
        except Exception:
            return
        narrow = self._narrow
        active_pane = PANE_IDS[self.active] if narrow else None
        left.set_class(narrow and active_pane != "email", "narrow-hidden")
        right.set_class(narrow and active_pane == "email", "narrow-hidden")
        for pid in PANE_IDS[1:]:  # events / tasks / hermes, inside #right
            try:
                self.query_one(f"#{pid}").set_class(narrow and pid != active_pane, "narrow-hidden")
            except Exception:
                pass

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

    def _adjacent(self, direction: str) -> None:
        if self._main_tabs().active != "tab-mail":
            return
        current_id = PANE_IDS[self.active]
        target_id = PANE_ADJACENCY.get(current_id, {}).get(direction)
        if target_id:
            self._focus_pane(PANE_IDS.index(target_id))

    # ---- help bar ----
    def _context_help_scope(self) -> str:
        tab = self._main_tabs().active
        return f"pane:{PANE_IDS[self.active]}" if tab == "tab-mail" else f"tab:{tab}"

    def _context_help_text(self) -> str:
        """Plain (non-clickable) context help text. Used only as the basis
        for narrow-mode line wrapping (see _narrow_wrap_help) — wrapping
        must be computed against the VISIBLE width, not one inflated by
        invisible [@click=...] markup tags."""
        text = bindings.CONTEXT_HELP.get(self._context_help_scope(), "")
        return bindings.ascii_safe(text) if self.settings.ascii_mode else text

    def _narrow_wrap_help(self) -> str:
        """Like _narrow_wrap, but keeps the context help row's shortcuts
        clickable (bindings.help_markup — the same affordance ThreadModal's
        help bar has) even when narrow. Line breaks are computed from the
        PLAIN text first, then each already-wrapped line gets its "Key
        Label" spans turned into action links — doing it in that order
        means wrap width isn't thrown off by markup tags that render as zero
        width. A span that happens to straddle a wrap boundary is just left
        plain on that occasion (rare, and harmless: it already wraps
        mid-phrase today without clickability).
        """
        scope = self._context_help_scope()
        plain = self._context_help_text()
        if not self._narrow or not plain:
            return bindings.help_markup(scope, self.settings.ascii_mode)
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
        "#drive-preview-col", "#browser-doc", "#nav-log", "#settings-feed-list",
        "#c-to-suggestions", "#drive-preview-meta", ".thread-msg-header",
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
        with TabbedContent(id="main-tabs", initial="tab-mail"):
            with TabPane(_tab_label("Mail", 1, self.settings.ascii_mode), id="tab-mail"):
                with Horizontal(id="body"):
                    with Vertical(id="left"):
                        with Container(id="email", classes="pane"):
                            yield self._pane_title_row("EMAIL  (threads)", 1)
                            yield Select(
                                [("All Mail", "ALL"), ("Inbox", "INBOX")],
                                value=self.settings.default_label_id
                                if self.settings.default_label_id in ("ALL", "INBOX") else "INBOX",
                                allow_blank=False, id="email-label-select",
                            )
                            with Horizontal(id="email-bar", classes="btnrow hidden"):
                                yield Input(placeholder="Search email (subject/from/snippet)… (/)",
                                            id="email-search")
                            yield ListView(id="email-list")
                    with Vertical(id="right"):
                        with Container(id="events", classes="pane"):
                            yield self._pane_title_row("EVENTS  (upcoming)", 2)
                            with Horizontal(id="events-bar", classes="btnrow hidden"):
                                yield Input(placeholder="Search events (summary/description)… (/)",
                                            id="events-search")
                            yield ListView(id="event-list")
                        with Container(id="tasks", classes="pane"):
                            yield self._pane_title_row("TASKS  (space=done, enter=detail)", 3)
                            with Horizontal(id="tasks-bar", classes="btnrow hidden"):
                                yield Input(placeholder="Search tasks (title/notes)… (/)", id="tasks-search")
                            yield ListView(id="task-list")
                        with Container(id="hermes", classes="pane"):
                            yield self._pane_title_row("HERMES ASK  (type a question, Enter)", 4)
                            yield RichLog(id="hermes-log", markup=False, wrap=True)
                            yield Input(placeholder="Ask Hermes about your Google stuff…", id="hermes-input")
            with TabPane(_tab_label("Calendar", 2, self.settings.ascii_mode), id="tab-calendar"):
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
            with TabPane(_tab_label("Drive", 3, self.settings.ascii_mode), id="tab-drive"):
                with Container(id="drive-section", classes="section"):
                    yield Label("/", id="drive-path", classes="muted")
                    with Horizontal(id="drive-search-bar", classes="btnrow hidden"):
                        yield Input(placeholder="Search this folder (name)… (/)", id="drive-search")
                    with Horizontal(id="drive-body"):
                        with Vertical(id="drive-list-col"):
                            yield ListView(id="drive-list")
                        with VerticalScroll(id="drive-preview-col"):
                            yield Static(id="drive-preview-meta")
                            yield RichLog(id="drive-preview-text", markup=False, wrap=True)
            with TabPane(_tab_label("Browser", 4, self.settings.ascii_mode), id="tab-browser"):
                with Container(id="browser-section", classes="section"):
                    with Horizontal(id="browser-bar"):
                        yield Static("WEB", id="browser-mode")
                        yield TabCyclingInput(placeholder="URL, or type to search…", id="browser-url")
                        yield Button("Go", id="browser-go")
                        yield Static("", id="browser-status")
                    with Horizontal(id="browser-bookmarks"):
                        for i, (label, _url) in enumerate(_BROWSER_BOOKMARKS):
                            yield Button(label, id=f"browser-bookmark-{i}")
                    yield DocumentView(id="browser-doc")
            with TabPane(_tab_label("News", 5, self.settings.ascii_mode), id="tab-news"):
                with Container(id="news-section", classes="section"):
                    yield Label("NEWS  (all subscribed feeds, newest first)", classes="pane-title-text")
                    with Horizontal(id="news-search-bar", classes="btnrow hidden"):
                        yield Input(placeholder="Search entries (title/summary)… (/)", id="news-search")
                    yield ListView(id="news-list")
            with TabPane(_tab_label("Navigation", 6, self.settings.ascii_mode), id="tab-navigation"):
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
            with TabPane(_tab_label("Contacts", 7, self.settings.ascii_mode), id="tab-contacts"):
                with Container(id="contacts-section", classes="section"):
                    yield Label("CONTACTS", classes="pane-title-text")
                    with Horizontal(id="contacts-bar", classes="btnrow"):
                        yield Input(placeholder="Search contacts (name or email)…", id="contacts-search")
                        yield Button("Refresh", id="contacts-refresh")
                    yield ListView(id="contacts-list")
            with TabPane(_tab_label("Settings", 8, self.settings.ascii_mode), id="tab-settings"):
                with Container(id="settings-section", classes="section"):
                    yield Label("SETTINGS", classes="pane-title-text")
                    with TabbedContent(id="settings-tabs"):
                        with TabPane("General", id="settings-tab-general"):
                            with VerticalScroll(id="settings-general-scroll"):
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
                                with Horizontal(classes="settings-row"):
                                    yield Label("Show sender address in list")
                                    yield Switch(value=self.settings.show_sender_address,
                                                 id="settings-show-sender-address-switch")
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
                                yield Label("Browser", classes="pane-title-text")
                                with Horizontal(classes="settings-row"):
                                    yield Label("Home page (Alt+H)")
                                    yield Input(value=self.settings.browser_home_url,
                                                placeholder="https://www.google.com",
                                                id="settings-browser-home-url")
                                    yield Button("Save", id="settings-save-browser-home")
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
                                    yield Button("Save", id="settings-save-nous-key")
                        with TabPane("News Feeds", id="settings-tab-feeds"):
                            with VerticalScroll(id="settings-feeds-scroll"):
                                yield Label("News feeds (RSS/Atom)", classes="pane-title-text")
                                yield ListView(
                                    *[_feed_list_item(u) for u in self.settings.feed_urls],
                                    id="settings-feed-list",
                                )
                                with Horizontal(classes="settings-row"):
                                    yield Input(placeholder="https://example.com/feed.xml", id="settings-feed-url")
                                    yield Button("Add feed", id="settings-add-feed")
                                yield Button("Remove selected feed", id="settings-remove-feed")
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
        self._focus_pane(0)
        self._apply_ascii_mode()  # applies whatever Settings.ascii_mode loaded from disk; also updates the help bar
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
        if not ask.any_provider_reachable(nous_api_key=self.settings.nous_api_key):
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
        self._cache = Cache(key)
        self._browser_tofu = fetchers.GeminiTofuStore(self._cache)
        if reset:
            self._cache.clear_all()
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
        self.sub_title = "Connecting…"
        # thread=True: the gauth/googleapiclient calls below are blocking
        # (synchronous httplib2), so fetching on a worker THREAD keeps the
        # asyncio event loop free to actually paint the loading/connecting
        # state instead of freezing the whole app until the fetch completes.
        self.run_worker(self._live_refresh_thread, thread=True, exclusive=True)

    def _load_from_cache(self) -> bool:
        thread_summaries = list(self._cache.get_all(f"thread_summary:{self._current_label_id}").values())
        events = list(self._cache.get_all("event").values())
        tasks = list(self._cache.get_all("task").values())
        tasklists = list(self._cache.get_all("tasklist").values())
        had_mail = bool(thread_summaries or events or tasks)
        if had_mail:
            self._apply_mail_data(thread_summaries, events, tasks, tasklists)

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

    def _write_mail_cache(self, label_id, threads, events, tasks, tasklists) -> None:
        if not self._cache:
            return
        self._cache.put_many(f"thread_summary:{label_id}", {t["threadId"]: t for t in threads})
        self._cache.put_many("event", {e["id"]: e for e in events})
        self._cache.put_many("task", {f"{t['_list']}-{t['id']}": t for t in tasks})
        self._cache.put_many("tasklist", {tl["id"]: tl for tl in tasklists})

    def _live_refresh_thread(self) -> None:
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
            cal_month = self._fetch_cal_month()
            self._cache.put("cal_month", f"{self._cal_year:04d}-{self._cal_month:02d}", cal_month)
        except Exception as e:
            ok = False
            self.call_from_thread(self.notify, f"Calendar error: {e}", severity="error")
        try:
            cal_week = self._fetch_cal_week()
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
            _, threads, events, tasks, tasklists = mail
            self._apply_mail_data(threads, events, tasks, tasklists)
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
        self.sub_title = f"Synced {now}" if ok else f"Offline (cached {now})"

    # ---- refresh ----
    def _fetch_mail_data(self):
        label_id = self._current_label_id
        label_ids = None if label_id in (None, "ALL") else [label_id]
        # Hand list_threads what we already have. It revalidates each listed
        # thread's historyId against the cached row and only refetches the ones
        # that actually changed — a refresh where nothing moved costs a single
        # API call instead of re-pulling all 80 thread summaries.
        known = self._cached_thread_summaries(label_id)
        threads = gauth.list_threads(self.svc, max_results=80, label_ids=label_ids,
                                     known=known)
        events = gauth.list_events(self.svc, days=21)
        tasklists = gauth.list_tasklists(self.svc)
        tasks = []
        for tl in tasklists:
            for t in gauth.list_tasks(self.svc, tl["id"], show_completed=True):
                tasks.append({**t, "_list": tl["id"]})
        return label_id, threads, events, tasks, tasklists

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
        try:
            label_ids = None if label_id in (None, "ALL") else [label_id]
            # Same historyId revalidation as _fetch_mail_data: switching back to
            # a label you've already opened re-reads it from cache rather than
            # re-pulling 80 thread summaries from Gmail.
            threads = gauth.list_threads(self.svc, max_results=80, label_ids=label_ids,
                                         known=self._cached_thread_summaries(label_id))
            if self._cache:
                self._cache.put_many(f"thread_summary:{label_id}", {t["threadId"]: t for t in threads})
        except Exception as e:
            self.call_from_thread(self.notify, f"Label refresh error: {e}", severity="error")
            return
        self.call_from_thread(self._apply_email_list, threads)

    def _apply_email_list(self, threads) -> None:
        self._mail_apply_gen += 1
        gen = self._mail_apply_gen
        self.run_worker(self._apply_email_list_async(gen, threads), exclusive=True, group="mail-apply")

    async def _apply_email_list_async(self, gen, threads) -> None:
        await self.query_one("#email-list").clear()
        if gen != self._mail_apply_gen:
            return  # superseded by a newer apply call
        self._threads_cache = {t["threadId"]: t for t in threads}
        try:
            query = self.query_one("#email-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_threads(threads, query) if query.strip() else threads
        _append_email_items(self.query_one("#email-list"), visible, self.settings.show_sender_address)

    def _apply_mail_data(self, threads, events, tasks, tasklists) -> None:
        self._tasklists = tasklists
        # ListView.clear() returns an AwaitRemove that can take LONGER than a
        # single call_after_refresh cycle to actually finish removing widgets
        # (confirmed empirically: a bulk removal of 80 items was still
        # in-flight one refresh later). Mail data can now be applied twice
        # per session (cache, then live refresh) with the SAME thread IDs, so
        # re-inserting before the prior removal truly completes raises
        # DuplicateIds. Run clear+repopulate as a properly-awaited worker
        # instead of a fire-and-forget deferred call; the generation counter
        # is a second safety net in case a superseded call still slips through.
        self._mail_apply_gen += 1
        gen = self._mail_apply_gen
        self.run_worker(
            self._apply_mail_data_async(gen, threads, events, tasks, tasklists),
            exclusive=True, group="mail-apply")

    async def _apply_mail_data_async(self, gen, threads, events, tasks, tasklists) -> None:
        await self.query_one("#email-list").clear()
        await self.query_one("#event-list").clear()
        await self.query_one("#task-list").clear()
        if gen != self._mail_apply_gen:
            return  # superseded by a newer _apply_mail_data call

        self._threads_cache = {t["threadId"]: t for t in threads}
        try:
            email_query = self.query_one("#email-search", Input).value
        except Exception:
            email_query = ""
        visible_threads = _fuzzy_filter_threads(threads, email_query) if email_query.strip() else threads
        _append_email_items(self.query_one("#email-list"), visible_threads, self.settings.show_sender_address)

        self._events_cache = events
        try:
            events_query = self.query_one("#events-search", Input).value
        except Exception:
            events_query = ""
        visible_events = _fuzzy_filter_events(events, events_query) if events_query.strip() else events
        _append_event_items(self.query_one("#event-list"), visible_events)

        self._tasks_cache = tasks
        try:
            tasks_query = self.query_one("#tasks-search", Input).value
        except Exception:
            tasks_query = ""
        visible_tasks = _fuzzy_filter_tasks(tasks, tasks_query) if tasks_query.strip() else tasks
        _append_task_items(self.query_one("#task-list"), visible_tasks)

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
        _, threads, events, tasks, tasklists = mail
        self._write_mail_cache(*mail)
        self.call_from_thread(self._apply_mail_data, threads, events, tasks, tasklists)
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
        tab_id = event.tabbed_content.active
        if tab_id == "tab-mail":
            self._focus_pane(self.active)
        elif tab_id == "tab-calendar":
            self.query_one("#cal-grid").focus()
        elif tab_id == "tab-drive":
            self.query_one("#drive-list").focus()
        elif tab_id == "tab-browser":
            self.query_one("#browser-url").focus()
        elif tab_id == "tab-news":
            self.query_one("#news-list").focus()
        elif tab_id == "tab-navigation":
            self.query_one("#nav-origin").focus()
        elif tab_id == "tab-settings":
            self._update_settings_cache_info()
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

    # ---- pane switching (Mail tab) ----
    def _goto_pane(self, idx: int) -> None:
        self._goto_tab("tab-mail")
        self._focus_pane(idx)

    def action_goto_pane_email(self):  self._goto_pane(0)
    def action_goto_pane_events(self): self._goto_pane(1)
    def action_goto_pane_tasks(self):  self._goto_pane(2)
    def action_goto_pane_hermes(self): self._goto_pane(3)

    def action_switch_left(self):
        if self._main_tabs().active == "tab-browser":
            self._browser_back()
        elif self._main_tabs().active == "tab-settings":
            self._cycle_settings_tab(-1)
        else:
            self._adjacent("left")

    def action_switch_right(self):
        if self._main_tabs().active == "tab-browser":
            self._browser_forward()
        elif self._main_tabs().active == "tab-settings":
            self._cycle_settings_tab(1)
        else:
            self._adjacent("right")

    def action_switch_up(self):    self._adjacent("up")
    def action_switch_down(self):  self._adjacent("down")

    def action_browser_home(self) -> None:
        """Alt+H: jump the Browser tab to the configured home URL.

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

    def action_cycle(self):
        tab = self._main_tabs().active
        if tab == "tab-mail":
            self._focus_pane((self.active + 1) % len(PANE_IDS))
        elif tab == "tab-browser":
            self._browser_toggle_focus()

    def action_cycle_back(self):
        tab = self._main_tabs().active
        if tab == "tab-mail":
            self._focus_pane((self.active - 1) % len(PANE_IDS))
        elif tab == "tab-browser":
            self._browser_toggle_focus()

    def action_refresh(self) -> None:
        self.sub_title = "Connecting…"
        self.run_worker(self._live_refresh_thread, thread=True, exclusive=True)

    def action_help(self): self.push_screen(HelpModal())

    def action_toggle_mouse(self) -> None:
        """Release/recapture the mouse (F2).

        While a TUI has mouse reporting enabled the terminal hands drag events
        to the app instead of drawing its own selection, which is why you can't
        just swipe over a URL and copy it the way you would in any other
        program. Turning reporting off hands the mouse back to the terminal:
        native click-drag selection and the terminal's own copy work exactly as
        they normally do, anywhere in the app. Clicking widgets stops working
        until you press F2 again — keyboard navigation is unaffected either way.

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
                "usual. Press F2 to give it back to the app.",
                timeout=6)
        else:
            self.notify("Mouse captured by the app again.")

    # ---- email reply/forward from lightbar ----
    def _selected_thread(self) -> str | None:
        el = self.query_one("#email-list")
        if el.highlighted_child is None:
            return None
        cid = el.highlighted_child.id or ""
        return cid[2:] if cid.startswith("t-") else None

    def _email_thread_order(self) -> list[str]:
        """The thread ids currently shown in #email-list, in display order.
        Backs ThreadModal's Left/Right prev/next-message navigation so it can
        page through the same (possibly search-filtered) list the user is
        looking at, without reopening the modal."""
        try:
            el = self.query_one("#email-list")
        except Exception:
            return []
        return [c.id[2:] for c in el.children
                if getattr(c, "id", "") and c.id.startswith("t-")]

    def action_reply(self):
        if not self._require_online():
            return
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="reply"), self._on_compose_result)
    def action_reply_all(self):
        if not self._require_online():
            return
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="reply_all"), self._on_compose_result)
    def action_forward(self):
        if not self._require_online():
            return
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="forward"), self._on_compose_result)
    def action_compose_new(self):
        if self._main_tabs().active != "tab-mail" or PANE_IDS[self.active] != "email":
            return
        self._open_compose_new()

    def action_mark_unread(self) -> None:
        """Mark the highlighted Email-pane thread UNREAD again, from the list
        (no need to open it). Email pane only; no-op elsewhere. Runs the
        network write on a worker thread per the fetch/apply split, then
        refreshes so the • unread bullet reappears."""
        if self._main_tabs().active != "tab-mail" or PANE_IDS[self.active] != "email":
            return
        if not self._require_online():
            return
        tid = self._selected_thread()
        if not tid:
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

    def action_focus_label_select(self) -> None:
        if self._main_tabs().active != "tab-mail" or PANE_IDS[self.active] != "email":
            return
        try:
            sel = self.query_one("#email-label-select", Select)
            sel.focus()
            sel.expanded = True
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
            pane = PANE_IDS[self.active]
            if pane == "email":
                self._show_pane_search("email-search")
            elif pane == "tasks":
                self._show_pane_search("tasks-search")
            elif pane == "events":
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
    # UI — see ROADMAP's separate P2 "Threading depth" item). Mutates just the
    # one highlighted ListItem's Label text in place; deliberately does NOT
    # call ListView.clear()/repopulate (see AGENTS.md's ListView.clear() NOTE
    # for why that's a trap this sidesteps entirely by not going there).
    # For a >1-message thread this fetches the full thread (same gauth call
    # as ThreadModal/Enter) so the inline preview shows every message, not
    # just the latest one's snippet — cached in self._thread_full_cache so
    # repeated collapse/expand of the same thread doesn't re-fetch. ----
    def _set_thread_label(self, thread_id: str, text: str) -> None:
        try:
            self.query_one(f"#{_mk_id('t', thread_id)} Label", Label).update(text)
        except Exception:
            pass

    def _toggle_thread_expand(self, thread_id: str) -> None:
        th = self._threads_cache.get(thread_id)
        if not th:
            return
        show_addr = self.settings.show_sender_address
        if thread_id in self._expanded_thread_ids:
            self._expanded_thread_ids.discard(thread_id)
            self._set_thread_label(thread_id, _email_collapsed_line(th, show_addr))
            return
        self._expanded_thread_ids.add(thread_id)
        if th.get("count", 1) > 1:
            cached = self._thread_full_cache.get(thread_id)
            if cached is not None:
                self._set_thread_label(thread_id, _thread_expanded_text(th, cached, show_addr))
            else:
                self._set_thread_label(thread_id, _email_collapsed_line(th, show_addr) + "\n    Loading messages…")
                self.run_worker(lambda: self._fetch_thread_preview(thread_id),
                                 thread=True, exclusive=False, group="thread-preview")
            return
        snippet = (th.get("snippet") or "").strip()
        if len(snippet) > 100:
            snippet = snippet[:100].rstrip() + "…"
        text = _email_collapsed_line(th, show_addr) + (("\n    " + snippet) if snippet else "")
        self._set_thread_label(thread_id, text)

    def _fetch_thread_preview(self, thread_id: str) -> None:
        try:
            msgs = gauth.get_thread(self.svc, thread_id)
        except Exception:
            self.call_from_thread(self._apply_thread_preview_error, thread_id)
            return
        self.call_from_thread(self._apply_thread_preview, thread_id, msgs)

    def _apply_thread_preview(self, thread_id: str, msgs: list[dict]) -> None:
        self._thread_full_cache[thread_id] = msgs
        th = self._threads_cache.get(thread_id)
        if not th or thread_id not in self._expanded_thread_ids:
            return
        self._set_thread_label(thread_id, _thread_expanded_text(th, msgs, self.settings.show_sender_address))

    def _apply_thread_preview_error(self, thread_id: str) -> None:
        th = self._threads_cache.get(thread_id)
        if not th or thread_id not in self._expanded_thread_ids:
            return
        snippet = (th.get("snippet") or "").strip()
        if len(snippet) > 100:
            snippet = snippet[:100].rstrip() + "…"
        extra = (f"\n    {snippet}  " if snippet else "\n    ") + f"({th['count']} messages — press Enter for full thread)"
        self._set_thread_label(thread_id, _email_collapsed_line(th, self.settings.show_sender_address) + extra)

    # ---- tasks ----
    def _selected_task(self) -> dict | None:
        tl = self.query_one("#task-list")
        if tl.highlighted_child is None:
            return None
        cid = tl.highlighted_child.id or ""
        if not cid.startswith("k-"):
            return None
        raw = cid[2:]  # "<list>-<id>"
        lid, _, tid = raw.rpartition("-")
        for t in getattr(self, "_tasks_cache", []):
            if t.get("_list") == lid and t.get("id") == tid:
                return t
        return None

    def _refresh_task_list(self) -> None:
        # Debounced keystroke path for #tasks-search — re-renders from the
        # already-fetched self._tasks_cache, no Google Tasks call per
        # keystroke. Deliberately its OWN exclusive group, not "mail-apply":
        # _apply_mail_data_async is a single coroutine that (re)builds
        # email+event+task lists together, clearing #task-list before it
        # re-populates it. If this ran in the same group, a keystroke here
        # could cancel an in-flight _apply_mail_data_async worker AFTER its
        # clear() but before its email/event repopulate, leaving those
        # panes blank. Own group + own generation counter keeps this path
        # from ever superseding that one.
        self._tasks_apply_gen += 1
        gen = self._tasks_apply_gen
        self.run_worker(self._apply_task_list_async(gen, self._tasks_cache),
                        exclusive=True, group="task-search-apply")

    async def _apply_task_list_async(self, gen: int, tasks: list[dict]) -> None:
        await self.query_one("#task-list").clear()
        if gen != self._tasks_apply_gen:
            return  # superseded by a newer apply call
        try:
            query = self.query_one("#tasks-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_tasks(tasks, query) if query.strip() else tasks
        _append_task_items(self.query_one("#task-list"), visible)

    def action_toggle_task(self):
        if not self._require_online():
            return
        t = self._selected_task()
        if not t:
            return
        done = t.get("status") != "completed"
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
        # Debounced keystroke path for #events-search — near-copy of
        # _refresh_task_list above (own exclusive group + own generation
        # counter, same reason: _apply_mail_data_async rebuilds email+event+
        # task together, and sharing its group would let a keystroke here
        # cancel an in-flight full apply mid-rebuild).
        self._events_apply_gen += 1
        gen = self._events_apply_gen
        self.run_worker(self._apply_event_list_async(gen, getattr(self, "_events_cache", [])),
                        exclusive=True, group="event-search-apply")

    async def _apply_event_list_async(self, gen: int, events: list[dict]) -> None:
        await self.query_one("#event-list").clear()
        if gen != self._events_apply_gen:
            return  # superseded by a newer apply call
        try:
            query = self.query_one("#events-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_events(events, query) if query.strip() else events
        _append_event_items(self.query_one("#event-list"), visible)

    def _highlighted_event_id(self) -> str | None:
        el = self.query_one("#event-list")
        if el.highlighted_child is None:
            return None
        cid = el.highlighted_child.id or ""
        return cid[2:] if cid.startswith("e-") else None

    def _open_event_by_id(self, eid: str) -> None:
        for e in getattr(self, "_events_cache", []):
            if e.get("id") == eid:
                self.push_screen(EventModal(e))
                return

    # ---- contextual space ----
    def action_context_space(self) -> None:
        tab = self._main_tabs().active
        if tab == "tab-news":
            lst = self.query_one("#news-list")
            item = lst.highlighted_child
            entry = self._news_by_cid.get(item.id or "") if item is not None else None
            if entry:
                self.push_screen(NewsEntryModal(entry))
            return
        if tab == "tab-contacts":
            lst = self.query_one("#contacts-list")
            item = lst.highlighted_child
            if item is not None and item.id:
                self._open_contact_detail(item.id)
            return
        if tab != "tab-mail":
            return
        pane = PANE_IDS[self.active]
        if pane == "tasks":
            self.action_toggle_task()
        elif pane == "email":
            tid = self._selected_thread()
            if tid:
                self._toggle_thread_expand(tid)
        elif pane == "events":
            eid = self._highlighted_event_id()
            if eid:
                self._open_event_by_id(eid)

    # ---- list selections (Enter) ----
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        cid = event.item.id or ""
        if cid.startswith("t-"):
            tid = cid[2:]
            order = self._email_thread_order()
            try:
                index = order.index(tid)
            except ValueError:
                order, index = [tid], 0
            self.push_screen(ThreadModal(self.svc, tid, thread_ids=order, index=index),
                              self._on_thread_modal_result)
        elif cid.startswith("e-"):
            self._open_event_by_id(cid[2:])
        elif cid.startswith("k-"):
            t = self._selected_task()
            if t:
                self.push_screen(TaskModal(self.svc, t, getattr(self, "_tasks_cache", [])),
                                  self._on_task_modal_result)
        elif cid.startswith("d-") or cid == "d-up":
            self._drive_open_selected()
        elif cid.startswith("n-"):
            entry = self._news_by_cid.get(cid)
            if entry:
                self.push_screen(NewsEntryModal(entry))
        elif cid.startswith("ct-"):
            self._open_contact_detail(cid)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "drive-list":
            return
        self._drive_on_highlight(event.item)

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
        if result == "sent":
            self.run_worker(self._refresh_all_thread, thread=True, exclusive=True)

    def _on_task_modal_result(self, mutated) -> None:
        # TaskModal (P2, 2026-07-15 subtask add/toggle/delete) dismisses
        # with whether it mutated anything; only then is it safe to touch
        # #task-list — see TaskModal's class docstring for the NoMatches
        # this avoids by NOT refreshing while the modal was still on top.
        if mutated:
            self.run_worker(self._refresh_all_thread, thread=True, exclusive=True)

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

    def _hermes_submit(self, event: Input.Submitted) -> None:
        q = event.value.strip()
        if not q:
            return
        event.input.value = ""
        log = self.query_one("#hermes-log")
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
        provider = ask.get_provider(self.settings.ai_provider, nous_api_key=self.settings.nous_api_key)
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
            threads = gauth.list_threads(self.svc, max_results=10)
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

    def _browser_fetch_dispatch(self, mode: str, target: str) -> render.Document:
        if mode == "http":
            return fetchers.fetch_http(target, ascii_mode=self.settings.ascii_mode)
        if mode == "gopher":
            return fetchers.fetch_gopher(target)
        if mode == "gemini":
            return fetchers.fetch_gemini(target, self._browser_tofu)
        return fetchers.run_search(target, self.settings)

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

        if not self._browser_started:
            self._browser_started = True
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
    def action_cal_prev(self) -> None:
        if self._main_tabs().active != "tab-calendar":
            return
        if self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week":
            self._cal_week_start -= dt.timedelta(days=7)
            self._build_cal_week()
        else:
            self._cal_month -= 1
            if self._cal_month == 0:
                self._cal_month = 12
                self._cal_year -= 1
            self._build_cal_month()

    def action_cal_next(self) -> None:
        if self._main_tabs().active != "tab-calendar":
            return
        if self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week":
            self._cal_week_start += dt.timedelta(days=7)
            self._build_cal_week()
        else:
            self._cal_month += 1
            if self._cal_month == 13:
                self._cal_month = 1
                self._cal_year += 1
            self._build_cal_month()

    def _day_cell_text(self, day: int, events: list[dict]) -> str:
        lines = [str(day)]
        for e in events[:2]:
            start = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
            time_part = start.split()[-1] if " " in start else ""
            lines.append(f"{time_part} {e.get('summary','')[:14]}"[:18])
        if len(events) > 2:
            lines.append(f"+{len(events) - 2} more")
        while len(lines) < 4:
            lines.append("")
        return "\n".join(lines)

    def _fetch_cal_month(self) -> list[dict]:
        return gauth.month_events(self.svc, self._cal_year, self._cal_month)

    def _build_cal_month(self) -> None:
        self._apply_cal_month(self._fetch_cal_month())

    def _apply_cal_month(self, events: list[dict]) -> None:
        grid = self.query_one("#cal-grid")
        grid.clear(columns=True)
        grid.add_columns("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        by_day: dict[int, list[dict]] = {}
        for e in events:
            d = _event_day(e)
            if d:
                by_day.setdefault(d, []).append(e)
        self._cal_by_day = by_day
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
        for i in range(0, len(cells), 7):
            row = [self._day_cell_text(d, by_day.get(d, [])) if d else "" for d in cells[i:i + 7]]
            grid.add_row(*row, height=4)

    def _fetch_cal_week(self) -> list[dict]:
        start = dt.datetime.combine(self._cal_week_start, dt.time.min).replace(tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=7)
        return gauth.events_between(self.svc, start, end)

    def _build_cal_week(self) -> None:
        self._apply_cal_week(self._fetch_cal_week())

    def _apply_cal_week(self, events: list[dict]) -> None:
        grid = self.query_one("#cal-week-grid")
        grid.clear(columns=True)
        grid.add_column("Hour")
        for label in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            grid.add_column(label)
        cells: dict[tuple[int, int], list[dict]] = {}
        for e in events:
            s = e.get("start", {}).get("dateTime")
            en = e.get("end", {}).get("dateTime")
            if not s or not en:
                continue  # all-day events show up in the Month view instead
            try:
                sdt = dt.datetime.fromisoformat(s)
                edt = dt.datetime.fromisoformat(en)
            except Exception:
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
        for hour in range(24):
            label = dt.time(hour).strftime("%I %p").lstrip("0")
            row = [label]
            for col in range(7):
                evs = cells.get((hour, col), [])
                if not evs:
                    row.append("")
                elif len(evs) == 1:
                    row.append(evs[0].get("summary", "")[:16])
                else:
                    row.append(f"{len(evs)} events")
            grid.add_row(*row)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        table_id = event.data_table.id
        if table_id == "cal-grid":
            self._cal_month_cell_selected(event)
        elif table_id == "cal-week-grid":
            self._cal_week_cell_selected(event)

    def _cal_month_cell_selected(self, event: DataTable.CellSelected) -> None:
        first_line = str(event.value).split("\n")[0]
        if not first_line.isdigit():
            return
        day = int(first_line)
        events = self._cal_by_day.get(day, [])
        if events:
            self.push_screen(DayEventsModal(day, self._cal_month, self._cal_year, events))

    def _cal_week_cell_selected(self, event: DataTable.CellSelected) -> None:
        col = event.coordinate.column - 1  # column 0 is the Hour label
        if col < 0:
            return
        evs = self._cal_week_cells.get((event.coordinate.row, col), [])
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
        # (0..6) maps to DataTable column col+1; the row IS the hour. A
        # multi-hour event spans several hour-cells, each a distinct jump
        # target — deliberate, so find-next steps through the block hour by hour.
        matches: list[tuple[int, int]] = []
        for (hour, col) in sorted(self._cal_week_cells):
            if any(self._event_matches(e, query_lower, threshold)
                   for e in self._cal_week_cells[(hour, col)]):
                matches.append((hour, col + 1))
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

    # ---- new event (Calendar tab, and the Mail tab's Events pane) ----
    def action_new_event(self) -> None:
        tab = self._main_tabs().active
        if tab == "tab-calendar":
            default_date = self._cal_default_day()
        elif tab == "tab-mail" and PANE_IDS[self.active] == "events":
            default_date = dt.date.today()
        else:
            return
        if not self._require_online():
            return
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
        try:
            cal_active_week = self.query_one("#cal-tabs", TabbedContent).active == "cal-tab-week"
        except Exception:
            cal_active_week = False
        self.run_worker(lambda: self._after_create_event_thread(cal_active_week),
                         thread=True, exclusive=True)

    def _after_create_event_thread(self, cal_active_week: bool) -> None:
        """Runs on its own worker thread (see AGENTS.md's fetch/apply-split
        NOTE) -- refreshes both places a newly-created event needs to show
        up: the Mail tab's Events pane (via the same _refresh_all_thread
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
    def _fetch_drive_files(self, folder_id: str) -> list[dict]:
        return gauth.list_drive(self.svc, folder_id)

    def _apply_drive_files(self, files: list[dict], folder_id: str, path: str) -> None:
        self._drive_folder_id = folder_id
        self._drive_path = path
        self._drive_files = files
        self.query_one("#drive-path").update(path)
        # See the NOTE in _apply_mail_data_async: ListView.clear() must
        # actually be awaited (a fire-and-forget clear + deferred populate
        # isn't reliably finished within one refresh cycle for a bulk
        # removal), since this can be called twice per session (cache, then
        # live refresh) with the same file IDs.
        self._drive_apply_gen += 1
        gen = self._drive_apply_gen
        self.run_worker(self._apply_drive_files_async(gen, files, path), exclusive=True, group="drive-apply")

    async def _apply_drive_files_async(self, gen: int, files: list[dict], path: str) -> None:
        await self.query_one("#drive-list").clear()
        if gen != self._drive_apply_gen:
            return  # superseded by a newer _apply_drive_files call
        try:
            query = self.query_one("#drive-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_drive_files(files, query) if query.strip() else files
        _append_drive_items(self.query_one("#drive-list"), visible, path)

    def _drive_load(self, folder_id: str = "root", path: str = "/") -> None:
        try:
            files = self._fetch_drive_files(folder_id)
        except Exception as ex:
            self.notify(f"Drive error: {ex}", severity="error")
            files = []
        self._apply_drive_files(files, folder_id, path)

    def _refresh_drive_list(self) -> None:
        # Debounced keystroke path for #drive-search — filters
        # self._drive_files, the CURRENT folder's listing only (never the
        # whole Drive tree, never a fresh Drive call per keystroke). Own
        # exclusive group + own generation counter, same reason as
        # _refresh_task_list/_refresh_event_list above: sharing "drive-apply"
        # would let a keystroke here cancel an in-flight folder navigation
        # (_apply_drive_files_async) mid-rebuild.
        self._drive_search_apply_gen += 1
        gen = self._drive_search_apply_gen
        self.run_worker(
            self._apply_drive_search_async(gen, self._drive_files, self._drive_path),
            exclusive=True, group="drive-search-apply")

    async def _apply_drive_search_async(self, gen: int, files: list[dict], path: str) -> None:
        await self.query_one("#drive-list").clear()
        if gen != self._drive_search_apply_gen:
            return  # superseded by a newer apply call
        try:
            query = self.query_one("#drive-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_drive_files(files, query) if query.strip() else files
        _append_drive_items(self.query_one("#drive-list"), visible, path)

    def _drive_open_selected(self) -> None:
        lst = self.query_one("#drive-list")
        if lst.highlighted_child is None:
            return
        cid = lst.highlighted_child.id or ""
        if cid == "d-up":
            self._drive_load("root", "/")
            return
        if not cid.startswith("d-"):
            return
        fid = cid[2:]
        f = next((x for x in self._drive_files if x["id"] == fid), None)
        if f and f["mimeType"] == "application/vnd.google-apps.folder":
            self._drive_load(f["id"], self._drive_path + f["name"] + "/")

    def _drive_on_highlight(self, item: ListItem | None) -> None:
        if item is None:
            return
        cid = item.id or ""
        if cid == "d-up":
            self._drive_cancel_pending_preview()
            self.query_one("#drive-preview-meta").update("(parent folder)")
            self.query_one("#drive-preview-text").clear()
            return
        if not cid.startswith("d-"):
            return
        fid = cid[2:]
        f = next((x for x in self._drive_files if x["id"] == fid), None)
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
            _DRIVE_PREVIEW_DEBOUNCE, lambda: self._drive_start_preview(f))

    def _drive_cancel_pending_preview(self) -> None:
        if self._drive_preview_timer is not None:
            self._drive_preview_timer.stop()
            self._drive_preview_timer = None

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
            self._apply_drive_preview(gen, hit[0], hit[1])
            return
        self.query_one("#drive-preview-text").clear()
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
        info, body = self._drive_preview_fetch(f)
        if gen == self._drive_preview_gen:
            self._drive_preview_cache[f["id"]] = (info, body)
        self.call_from_thread(self._apply_drive_preview, gen, info, body)

    def _drive_preview_fetch(self, f: dict) -> tuple[str, str]:
        """Blocking; returns the (meta_text, body_text) pair to render."""
        is_folder = f["mimeType"] == "application/vnd.google-apps.folder"
        fid = f["id"]
        # The folder listing already told us this file's modifiedTime, for free.
        # Drive stamps a new one on every edit, so it revalidates the cache the
        # same way a thread's historyId does: if what we cached was stamped with
        # the same modifiedTime, it IS the current file and there is nothing to
        # download. Previously the cache was consulted ONLY when offline, so the
        # normal (online) path re-downloaded every file on every look.
        listed_mtime = str(f.get("modifiedTime") or "")
        cached_meta = self._cache.get("drive_file_meta", fid) if self._cache else None
        fresh = bool(
            cached_meta and listed_mtime
            and str(cached_meta.get("modifiedTime") or "") == listed_mtime
        )

        if fresh:
            meta = cached_meta
        elif self._online:
            try:
                meta = gauth.get_file_metadata(self.svc, fid)
            except Exception as ex:
                return f"(metadata error: {ex})", ""
            if self._cache:
                self._cache.put("drive_file_meta", fid, meta)
        else:
            meta = cached_meta
            if meta is None:
                return (f"Name: {f.get('name','')}\n(offline — never viewed online, "
                        "no cached details)", "(not available offline)")

        owners = ", ".join(
            o.get("displayName", o.get("emailAddress", "?")) for o in meta.get("owners", []))
        created = _fmt_date(meta.get("createdTime", ""))
        modified = _fmt_date(meta.get("modifiedTime", ""))
        kind = "Folder" if is_folder else meta.get("mimeType", "")
        info = (f"Name:     {meta.get('name','')}\n"
                f"Type:     {kind}\n"
                f"Where:    {self._drive_path}\n"
                f"Owner:    {owners or '(unknown)'}\n"
                f"Created:  {created}\n"
                f"Modified: {modified}")
        if not self._online:
            info += "\n(offline — showing cached details)"
        if is_folder:
            return info, "(folder — press Enter to open)"
        if not _is_previewable(meta.get("mimeType", "")):
            return info, "(binary/image file — no text preview)"

        # The body is the expensive part (a full file download). Reuse the
        # cached text whenever the modifiedTime says the file hasn't changed —
        # `fresh` was decided against the listing's modifiedTime above.
        cached_text = self._cache.get("drive_file_text", fid) if self._cache else None
        if fresh and cached_text:
            return info, cached_text["text"][:8000]

        if self._online:
            try:
                _, _, text = gauth.read_drive_text(self.svc, fid)
            except Exception as ex:
                return info, f"(preview error: {ex})"
            if self._cache:
                self._cache.put("drive_file_text", fid, {"text": text})
            return info, text[:8000]

        if cached_text:
            return info, cached_text["text"][:8000]
        return info, "(not available offline — open this file once while online to cache it)"

    def _apply_drive_preview(self, gen: int, info: str, body: str) -> None:
        if gen != self._drive_preview_gen:
            return  # cursor moved on; a newer preview owns the pane now
        self.query_one("#drive-preview-meta").update(info)
        text_widget = self.query_one("#drive-preview-text")
        text_widget.clear()
        if body:
            text_widget.write(body)

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
        # Same ListView.clear()-is-async trap as _apply_mail_data_async /
        # _apply_drive_files_async (AGENTS.md §2): this can run more than
        # once per session (cache load, live refresh, and again whenever a
        # feed is added/removed in Settings), so clear+repopulate is a
        # properly-awaited worker with a generation counter, not a bare
        # clear() + call_after_refresh.
        self._news_apply_gen += 1
        gen = self._news_apply_gen
        self.run_worker(self._apply_news_data_async(gen, entries), exclusive=True, group="news-apply")

    async def _apply_news_data_async(self, gen: int, entries: list[dict]) -> None:
        await self.query_one("#news-list").clear()
        if gen != self._news_apply_gen:
            return  # superseded by a newer _apply_news_data call
        # Backs the News tab's search filter (Input#news-search) — same role
        # as self._threads_cache/self._tasks_cache for Email/Tasks search,
        # see the module-level NOTE by its declaration in __init__.
        self._news_entries_cache = entries
        try:
            query = self.query_one("#news-search", Input).value
        except Exception:
            query = ""
        visible = _fuzzy_filter_news_entries(entries, query) if query.strip() else entries
        self._populate_news_list(visible)

    def _populate_news_list(self, entries: list[dict]) -> None:
        lst = self.query_one("#news-list")
        self._news_by_cid = {}
        items = []
        for e in sorted(entries, key=lambda e: e.get("published") or "", reverse=True):
            cid = _mk_id("n", e["id"])
            self._news_by_cid[cid] = e
            date = _fmt_date(e.get("published", "")).split(" ")[0]
            feed_title = (e.get("feed_title") or "")[:20]
            title = (e.get("title") or "(untitled)")[:40]
            # feed_title/title come straight from someone else's RSS/Atom
            # feed, and the row literally wraps feed_title in "[...]" — see
            # the markup=False NOTE in _feed_list_item above for why this
            # needs markup disabled rather than escaped: Textual's
            # Content.from_markup() (what Label routes through) would
            # otherwise silently swallow "[Feed Title]" as a bogus style tag.
            line = f"{date}  [{feed_title}] {title}"
            items.append(ListItem(Label(line, markup=False), id=cid))
        lst.extend(items)

    def _refresh_news_list(self) -> None:
        # Debounced keystroke path for #news-search — filters the already-
        # fetched self._news_entries_cache, never re-fetches any feed per
        # keystroke. Own exclusive group + own generation counter, same
        # reason as _refresh_task_list/_refresh_event_list/_refresh_drive_list
        # above: sharing "news-apply" would let a keystroke here cancel an
        # in-flight full news apply (cache load / live refresh / feed
        # add-remove) mid-rebuild.
        self._news_search_apply_gen += 1
        gen = self._news_search_apply_gen
        self.run_worker(
            self._apply_news_search_async(gen, self._news_entries_cache),
            exclusive=True, group="news-search-apply")

    async def _apply_news_search_async(self, gen: int, entries: list[dict]) -> None:
        await self.query_one("#news-list").clear()
        if gen != self._news_search_apply_gen:
            return  # superseded by a newer apply call
        try:
            query = self.query_one("#news-search", Input).value
        except Exception:
            query = ""
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

    def _add_feed_url(self) -> None:
        inp = self.query_one("#settings-feed-url", Input)
        url = inp.value.strip()
        if not url:
            return
        if url in self.settings.feed_urls:
            self.notify("Already subscribed to that feed", severity="warning")
            return
        self.settings.feed_urls.append(url)
        save_settings(self.settings)
        inp.value = ""
        self.query_one("#settings-feed-list", ListView).append(_feed_list_item(url))
        self.notify(f"Added feed: {url}")
        self.run_worker(
            lambda: self._fetch_and_merge_one_feed(url),
            thread=True, exclusive=False, group="news-fetch-one",
        )

    def _remove_selected_feed(self) -> None:
        lst = self.query_one("#settings-feed-list", ListView)
        item = lst.highlighted_child
        if item is None:
            self.notify("Select a feed to remove first", severity="warning")
            return
        url = getattr(item, "feed_url", None)
        if url is None or url not in self.settings.feed_urls:
            return
        self.settings.feed_urls.remove(url)
        save_settings(self.settings)
        item.remove()
        self.notify(f"Removed feed: {url}")
        if self._cache:
            remaining = [e for e in self._cache.get_all("feed_entry").values() if e.get("feed_url") != url]
            self._apply_news_data(remaining)

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
        try:
            query = self.query_one("#contacts-search", Input).value
        except Exception:
            query = ""
        self._contacts_apply_gen += 1
        gen = self._contacts_apply_gen
        self.run_worker(self._apply_contacts_list_async(gen, query), exclusive=True, group="contacts-apply")

    async def _apply_contacts_list_async(self, gen: int, query: str) -> None:
        # Same ListView.clear()-is-async trap as _apply_mail_data_async /
        # _apply_news_data_async (AGENTS.md's ListView.clear() NOTE) — this
        # can run more than once in quick succession (cache load, live
        # fetch, every keystroke in the search box), so it's a properly
        # awaited worker with a generation counter, not bare clear() +
        # call_after_refresh.
        await self.query_one("#contacts-list").clear()
        if gen != self._contacts_apply_gen:
            return  # superseded by a newer call
        lst = self.query_one("#contacts-list")
        self._contacts_by_cid = {}
        if self._contacts_auth_broken:
            lst.append(ListItem(Label(
                "Not connected — Google token is missing or expired.\n"
                "Reconnect from Settings -> General to load contacts.",
                markup=False)))
            return
        items = []
        for c in _fuzzy_filter_contacts(self._contacts_cache, query):
            name = (c.get("name") or "").strip()
            addr = (c.get("email") or "").strip()
            if not name and not addr:
                continue  # no usable info at all — not worth a row
            cid = _mk_id("ct", c.get("resource_name", ""))
            self._contacts_by_cid[cid] = c
            label = f"{name[:30]:<30} {addr[:40]}" if name else addr[:40]
            items.append(ListItem(Label(label, markup=False), id=cid))
        lst.extend(items)

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
            self.notify("Encryption disabled. Local cache cleared; it will repopulate unencrypted.")
            self._update_settings_cache_info()
            return
        if self.settings.key_method == "passphrase":
            self.push_screen(UnlockModal(self.settings, mode="create"), self._on_settings_passphrase_result)
        else:
            read_or_create_keyfile()
            self.settings.encrypt_at_rest = True
            self.settings.key_method = "keyfile"
            save_settings(self.settings)
            if self._cache:
                self._cache.clear_all()
            self.notify("Encryption enabled (local key file). Cache cleared; restart google-tui to apply.")
            self._update_settings_cache_info()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "settings-ai-provider":
            pid = event.pressed.id.removeprefix("rb-provider-")
            if pid == self.settings.ai_provider:
                return
            self.settings.ai_provider = pid
            save_settings(self.settings)
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
            read_or_create_keyfile()
            self.settings.key_method = "keyfile"
            self.settings.kdf_salt = None
            self.settings.canary = None
            save_settings(self.settings)
            if self._cache:
                self._cache.clear_all()
            self.notify("Switched to local key file. Cache cleared; restart google-tui to apply.")
            self._update_settings_cache_info()

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
        self.notify("Encryption enabled (passphrase). Cache cleared; restart google-tui to apply.")
        self._update_settings_cache_info()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-reauth-google":
            self._start_google_reauth()
        elif event.button.id == "settings-clear-cache":
            if self._cache:
                self._cache.clear_all()
            self.notify("Local cache cleared.")
            self._update_settings_cache_info()
        elif event.button.id == "settings-prune-cache":
            self._prune_cache()
        elif event.button.id == "settings-save-nous-key":
            key = self.query_one("#settings-nous-key", Input).value.strip()
            self.settings.nous_api_key = key or None
            save_settings(self.settings)
            self.notify("Nous API key saved.")
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
        elif event.button.id == "settings-save-search":
            key = self.query_one("#settings-google-cse-key", Input).value.strip()
            cx = self.query_one("#settings-google-cse-id", Input).value.strip()
            searxng_url = self.query_one("#settings-searxng-url", Input).value.strip()
            self.settings.google_cse_api_key = key or None
            self.settings.google_cse_id = cx or None
            self.settings.searxng_url = searxng_url or None
            save_settings(self.settings)
            self.notify("Search settings saved.")
        elif event.button.id is not None and event.button.id.startswith("browser-bookmark-"):
            idx = int(event.button.id.removeprefix("browser-bookmark-"))
            _label, url = _BROWSER_BOOKMARKS[idx]
            self.query_one("#browser-url", Input).value = url
            self._browser_navigate(url, push_history=True)
        elif event.button.id == "nav-go":
            self._nav_go()
        elif event.button.id == "nav-export":
            self._nav_export()
        elif event.button.id == "settings-save-routes":
            key = self.query_one("#settings-routes-key", Input).value.strip()
            self.settings.routes_api_key = key or None
            save_settings(self.settings)
            self.notify("Routes API key saved.")
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
                yield Static(
                    "Copy URL puts it on your computer's clipboard even over "
                    "SSH (terminal must allow OSC 52 — in tmux: "
                    "set -g set-clipboard on). If nothing lands on your "
                    "clipboard, use Save to file, or press F2 to release the "
                    "mouse and select the URL with your terminal as usual.",
                    id="reauth-copy-help", classes="muted",
                )
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
        `set-clipboard on`), which is exactly why "Save to file" and the F2
        mouse-release toggle exist alongside it.
        """
        self.app.copy_to_clipboard(self._auth_url or "")
        self.query_one("#reauth-status", Static).update(
            "Copied to clipboard. If your clipboard is still empty, your "
            "terminal blocks OSC 52 — use Save to file or F2 instead.")

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


class LabelPickerModal(ModalScreen):
    """Multi-select label picker for ThreadModal's "L" action. Presents the
    account's user labels as a checklist; dismisses with the list of selected
    label ids to ADD to the thread (or None on cancel).

    Deliberately assign-only (add), not a full add/remove editor: the thread
    body fetch (gauth.get_thread) doesn't return per-thread labelIds, so we
    can't pre-check "already applied" labels to offer removal without an extra
    round-trip. "Assign labels" is what the ROADMAP asked for; a
    remove/toggle editor is a reasonable future extension (see CHANGELOG)."""

    def __init__(self, labels: list[dict]):
        super().__init__()
        self._labels = labels

    def compose(self) -> ComposeResult:
        with Container(id="labelpick-box", classes="pane"):
            yield Label("ASSIGN LABELS", classes="pane-title-text")
            yield SelectionList(
                *[(_label_display_name(l), l["id"]) for l in self._labels],
                id="labelpick-list")
            with Horizontal(classes="btnrow"):
                yield Button("Apply", id="labelpick-apply")
                yield Button("Cancel", id="labelpick-cancel")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "labelpick-apply":
            self.dismiss(list(self.query_one("#labelpick-list", SelectionList).selected))
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

    def compose(self) -> ComposeResult:
        with Container(id="thread-box", classes="pane"):
            yield Label("THREAD", classes="pane-title-text", id="thread-title")
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
        if not self.app._require_online():
            return
        self._run_mutation(lambda: gauth.trash_thread(self.svc, self.thread_id),
                           "Moved to Trash")

    def action_archive(self) -> None:
        if not self.app._require_online():
            return
        self._run_mutation(lambda: gauth.archive_thread(self.svc, self.thread_id),
                           "Archived (removed from Inbox)")

    def action_labels(self) -> None:
        if not self.app._require_online():
            return
        labels = getattr(self.app, "_labels_cache", [])
        pickable = [l for l in labels
                    if l.get("type") != "system" and l.get("id") and l.get("name")]
        if not pickable:
            self.app.notify("No labels available to assign", severity="warning")
            return
        self.app.push_screen(LabelPickerModal(pickable), self._on_labels_result)

    def _on_labels_result(self, add_ids) -> None:
        if not add_ids:
            return
        self._run_mutation(
            lambda: gauth.modify_labels(self.svc, self.thread_id, add=list(add_ids)),
            f"Applied {len(add_ids)} label(s)", close=False)

    def _run_mutation(self, fn, success_msg: str, close: bool = True) -> None:
        """Run a mutating gauth call on a worker thread (fetch/apply split),
        then notify + (optionally) dismiss with "refresh" so the Email pane
        drops/updates the thread. `close=False` keeps the modal open (used for
        label changes, which don't remove the thread from view)."""
        def work() -> None:
            try:
                fn()
            except Exception as e:
                self.app.call_from_thread(self.app.notify, f"Action failed: {e}",
                                          severity="error")
                return
            self.app.call_from_thread(self._after_mutation, success_msg, close)
        self.run_worker(work, thread=True, exclusive=True)

    def _after_mutation(self, msg: str, close: bool) -> None:
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

    def compose(self) -> ComposeResult:
        with Container(id="compose-box", classes="pane"):
            yield Label("COMPOSE", classes="pane-title-text")
            yield Input(placeholder="To", id="c-to")
            yield ListView(id="c-to-suggestions", classes="hidden")
            yield Input(placeholder="Subject", id="c-subject")
            yield TextArea(id="c-body", language="markdown")
        with Horizontal(classes="btnrow"):
            yield Button("Send", id="send")
            yield Button("Cancel", id="cancel")
        yield Static("", id="send-countdown")

    def on_mount(self) -> None:
        if self.mode == "new":
            to, subject = self._prefill_to, ""
        else:
            g = self.svc["gmail"]
            th = g.users().threads().get(userId="me", id=self.thread_id, format="metadata",
                                         metadataHeaders=["From", "To", "Cc", "Subject"]).execute()
            last = th["messages"][-1]
            hdrs = {h["name"].lower(): h["value"] for h in last.get("payload", {}).get("headers", [])}
            subj = hdrs.get("subject", "")
            if self.mode == "reply":
                to = hdrs.get("from", "")
                subject = subj if subj.lower().startswith("re:") else "Re: " + subj
            elif self.mode == "reply_all":
                to = ", ".join(filter(None, [hdrs.get("from", ""), hdrs.get("to", ""), hdrs.get("cc", "")]))
                subject = subj if subj.lower().startswith("re:") else "Re: " + subj
            else:  # forward
                to = ""
                subject = subj if subj.lower().startswith("fwd:") else "Fwd: " + subj
        self.query_one("#c-to").value = to
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

    def _try_send(self) -> None:
        if self._countdown_timer is not None:
            return  # already counting down
        if not self.query_one("#c-to").value.strip():
            return
        self._start_send_countdown()

    def _start_send_countdown(self) -> None:
        self._countdown_remaining = self.SEND_COUNTDOWN_SECONDS
        self.query_one("#c-to").disabled = True
        self.query_one("#c-subject").disabled = True
        self.query_one("#c-body").disabled = True
        self.query_one("#send", Button).disabled = True
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

    def _cancel_countdown(self) -> None:
        self._countdown_timer.stop()
        self._countdown_timer = None
        self.query_one("#c-to").disabled = False
        self.query_one("#c-subject").disabled = False
        self.query_one("#c-body").disabled = False
        self.query_one("#send", Button).disabled = False
        self.query_one("#send-countdown", Static).update("")

    def _send_now(self) -> None:
        to = self.query_one("#c-to").value.strip()
        subject = self.query_one("#c-subject").value.strip()
        body = self.query_one("#c-body").text
        if self.mode == "forward":
            gauth.forward(self.svc, self.thread_id, to, body_prefix=body + "\n")
        elif self.mode == "new":
            gauth.send_message(self.svc, to=to, subject=subject, body=body)
        else:
            gauth.reply_to(self.svc, self.thread_id, body, reply_all=(self.mode == "reply_all"))
        self.dismiss("sent")

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
    def __init__(self, event: dict):
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        with Container(id="ev-box", classes="pane"):
            yield Label("APPOINTMENT DETAIL", classes="pane-title-text")
            yield Static(id="ev-detail")
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        e = self.event
        start = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
        end = _fmt_date(e.get("end", {}).get("dateTime") or e.get("end", {}).get("date", ""))
        det = (f"Summary: {e.get('summary','')}\n"
               f"Start:   {start}\nEnd:     {end}\n"
               f"Location:{e.get('location','')}\n"
               f"Link:    {e.get('htmlLink','')}\n\n{e.get('description','')}")
        self.query_one("#ev-detail").update(det)

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

    def __init__(self, svc, default_date: dt.date):
        super().__init__()
        self.svc = svc
        self.default_date = default_date
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
            # Attach the system's local timezone -- the user typed a
            # wall-clock time meaning "local time here", and Calendar's API
            # needs a UTC-offset-bearing dateTime (or an explicit timeZone
            # field, which create_event() deliberately omits) to place the
            # event correctly rather than silently treating it as UTC.
            local_tz = dt.datetime.now().astimezone().tzinfo
            start = dt.datetime.combine(day, start_time, tzinfo=local_tz)
            end = (dt.datetime.combine(day, end_time, tzinfo=local_tz)
                   if end_time is not None else start + dt.timedelta(hours=1))
            if end <= start:
                self.notify("End time must be after start time", severity="warning")
                return
        self._submitting = True
        self.run_worker(lambda: self._create_thread(title, start, end, all_day),
                         thread=True, exclusive=True, group="event-create")

    def _create_thread(self, title: str, start, end, all_day: bool) -> None:
        try:
            gauth.create_event(self.svc, title, start, end, all_day=all_day)
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


class TaskModal(ModalScreen):
    """Task detail + subtasks (P2, 2026-07-15 — was read-only-nothing before:
    the old version didn't show subtasks at all, despite ROADMAP.md's stale
    claim that it did; see CHANGELOG).

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
               f"Due:   {t.get('due','')}\n\nNotes:\n{t.get('notes','')}")
        self.query_one("#tk-detail").update(det)
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
                Label(f"{'[x]' if s.get('status') == 'completed' else '[ ]'} "
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
        if not self.app._require_online():
            return
        inp = self.query_one("#tk-subtask-input", Input)
        title = inp.value.strip()
        if not title:
            return
        inp.value = ""
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
        if not self.app._require_online():
            return
        s = self._highlighted_subtask()
        if not s:
            return
        done = s.get("status") != "completed"
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
        if not self.app._require_online():
            return
        s = self._highlighted_subtask()
        if not s:
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
        if not self.app._require_online():
            return
        # Unlike a subtask, deleting the TOP-LEVEL task also cascades to
        # every subtask under it server-side and closes this whole modal —
        # a lightweight confirm here is worth the one extra keypress even
        # though nothing else in this app confirms before a mutation.
        n = len(self.subtasks)
        msg = "Delete this task"
        msg += f" and its {n} subtask{'s' if n != 1 else ''}?" if n else "?"
        self.app.push_screen(ConfirmModal(msg), self._on_delete_task_confirm)

    def _on_delete_task_confirm(self, confirmed: bool | None) -> None:
        if not confirmed:
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
    if _update_check_enabled():
        try:
            if updater.check_for_update():
                updater.restart()  # does not return
        except Exception as e:
            # A broken update check must never be the reason you can't read your
            # mail. Report it and carry on into the app regardless.
            print(f"Can't reach update server, skipping update check. ({e})", flush=True)
    GoogleTUI().run()


def _update_check_enabled() -> bool:
    if "--no-update" in sys.argv or os.environ.get("GOOGLE_TUI_NO_UPDATE") == "1":
        return False
    try:
        return load_settings().check_for_updates
    except Exception:
        return True


if __name__ == "__main__":
    main()
