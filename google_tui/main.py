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
import re
import urllib.parse
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import platformdirs
from rapidfuzz import fuzz
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Header, Input, Label, ListItem, ListView,
    RadioButton, RadioSet, RichLog, Select, Static, Switch, TabbedContent, TabPane, TextArea,
)
from textual.worker import get_current_worker  # noqa: F401 (kept for future threaded workers)

from . import gauth
from . import ask
from .ask import needs_agent
from . import setup_instructions
from . import fetchers
from . import render
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

TAB_ORDER = ["tab-mail", "tab-calendar", "tab-drive", "tab-browser", "tab-news", "tab-navigation", "tab-settings",
             "tab-contacts"]
SETTINGS_TAB_ORDER = ["settings-tab-general", "settings-tab-ai", "settings-tab-feeds", "settings-tab-search",
                       "settings-tab-navigation"]

_SUPERSCRIPT = {1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸"}

NAV_EXPORT_DIR = Path(platformdirs.user_documents_dir()) / "google-tui"

_PREVIEWABLE_PREFIXES = (
    "text/",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
)
_PREVIEWABLE_EXTRA = {"application/json", "application/xml"}

HELP_GLOBAL = (
    "Ctrl+# / Ctrl+←→ Tab   Alt+# Pane   Alt+←→↑↓ Move Pane   "
    "Ctrl+P Commands   Ctrl+H Help   Ctrl+Q Quit"
)

_KEY_METHOD_LABELS = {
    "passphrase": "Passphrase (prompt at launch)",
    "keyfile": "Local key file (no prompt)",
}

HELP_TEXT = """\
GLOBAL
  Ctrl+1..8        Switch tab (Mail / Calendar / Drive / Browser / News / Navigation / Settings / Contacts)
  Ctrl+Left/Right  Cycle tabs (use this if Ctrl+1..7 doesn't reach the app —
                   some terminals/browsers don't transmit Ctrl+digit)
  Alt+1..4         Jump to Mail pane (Email / Events / Tasks / Hermes)
  Alt+arrows       Move to the adjacent Mail pane
  Tab / Shift+Tab  Cycle Mail panes
  Ctrl+R           Reconnect / refresh live data
  Ctrl+P           Command palette
  Ctrl+H           This help
  Ctrl+Q           Quit

MAIL TAB
  Email pane:   Enter open thread, Space expand/collapse (shows snippet),
                l open folder picker, r Reply, a Reply All, f Forward
  Events pane:  Enter/Space open event detail
  Tasks pane:   Space toggle complete, Enter open detail
  Hermes pane:  type a question, Enter to ask

CALENDAR TAB
  [ / ]         Previous / next month (or week, in Week view)
  Enter/click   Open a day's full event list (Month view)
                Open an event, or a chooser if several share an hour (Week view)

DRIVE TAB
  Up/Down       Move selection — preview pane updates live
  Enter/click   Open a folder, or re-load a file's preview

BROWSER TAB
  Enter (address bar)    Load URL, or run a search (bare text w/ no scheme searches)
  Bookmark buttons       Starter destinations (Google/Wikipedia/Gopherpedia/
                         Gemini Protocol) shown until you navigate anywhere,
                         then hidden for the rest of the session
  Alt+Left / Alt+Right   Back / forward through this session's history
  Tab                    Toggle focus: address bar <-> page content
  0-9 then Enter (page)  Jump to numbered link
  Esc (page)             Cancel a pending number entry

NEWS TAB
  Enter/Space   Open the selected entry (rendered via the shared Document view)
  Entries from every subscribed feed are combined, newest first. Manage
  subscriptions (add/remove feed URLs) from the Settings tab.

NAVIGATION TAB
  Origin/Destination inputs, then Enter or the Go button, compute a driving
  route via the Google Routes API (free-text addresses — no need for exact
  coordinates). Shows total distance/duration plus a turn-by-turn step list.
  Export     Save the current itinerary to a text file (Documents/google-tui)
  Needs a Routes API key, set in Settings -> Navigation.

SETTINGS TAB
  Sub-tabs      General / AI Provider / News Feeds / Search / Navigation —
                Alt+Left/Right cycles between them while the Settings tab
                is active
  Switch        Toggle encrypt-at-rest for the local cache (General)
  RadioSet      Choose passphrase-at-launch vs. local key file (General)
  Button        Clear the local cache immediately (General)
  RadioSet      Choose AI provider for the Hermes Ask pane (AI Provider)
  Input+Button  Set/save the Nous API key (AI Provider)
  Input+Button  Add a News-tab feed subscription (URL) (News Feeds)
  Button        Remove the selected feed subscription (News Feeds)
  RadioSet      Choose the Browser tab's search provider: Google /
                DuckDuckGo / SearXNG (Search)
  Input+Button  Set Google Custom Search API key + Search Engine ID, or a
                SearXNG instance URL, then save (Search)
  Input+Button  Set/save the Routes API key used by the Navigation tab
                (Navigation)

CONTACTS TAB
  Type to search    Live fuzzy filter (name or email) over your fetched
                    Google Contacts — no re-query as you type
  Enter/Space       Open the highlighted contact's detail (name/email/phone),
                    with a "Compose Email" button to start a new message to them
  Compose New       Open a blank Compose (To/Subject/Body all empty)
  Refresh           Re-fetch contacts from Google now
  Contacts are fetched lazily (once, the first time you open this tab, not
  on every startup/Ctrl+R) since they change far less often than mail/
  calendar/drive. Needs the contacts.readonly scope on your Google token —
  if that's missing, this notifies an error instead of crashing (SETUP.md §7).
  ComposeModal's To field also fuzzy-suggests from these same contacts as
  you type a name.

Reply/Forward/Toggle-complete are disabled while offline (shown in the
title bar as "Offline (cached HH:MM)"); browsing cached data still works.
"""


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


def _tab_label(text: str, num: int) -> str:
    return f"{text} [dim]{_SUPERSCRIPT[num]}[/dim]"


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


def _email_collapsed_line(th: dict) -> str:
    mark = "•" if th["unread"] else " "
    subj = th["subject"] or "(no subject)"
    return f"{mark} {th['from'][:36]:<36} {subj[:60]}  ({th['count']})"


def _append_email_items(email_list, threads) -> None:
    for th in threads:
        email_list.append(ListItem(Label(_email_collapsed_line(th)), id=_mk_id("t", th["threadId"])))


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
    #email-list { height: 1fr; }
    #event-list, #task-list { height: 1fr; }
    #hermes-log { height: 1fr; border: round $panel-darken-1; }
    #hermes-input { dock: bottom; }
    .muted { color: $text-muted; }
    .btnrow { height: 3; align: left middle; }
    #send-countdown { height: 1; color: $accent; text-style: bold; }
    .section { height: 1fr; border: round $panel-darken-2; padding: 0 1; }
    #cal-grid, #cal-week-grid { height: 1fr; }
    #drive-body { height: 1fr; }
    #drive-list-col { width: 40%; border: round $panel-darken-1; }
    #drive-preview-col { width: 1fr; border: round $panel-darken-1; padding: 0 1; }
    #drive-preview-meta { height: auto; border-bottom: solid $panel-darken-2; padding-bottom: 1; }
    #drive-preview-text { height: 1fr; }
    #browser-bar { height: 3; align: left middle; }
    #browser-mode { width: 10; color: $accent; text-style: bold; content-align: center middle; }
    #browser-url { width: 1fr; }
    #browser-status { width: auto; color: $text-muted; margin-left: 1; }
    #browser-bookmarks { height: 3; align: left middle; }
    #browser-bookmarks Button { min-width: 3; width: auto; height: 3; margin-right: 1; }
    #browser-doc { height: 1fr; border: round $panel-darken-1; padding: 0 1; }
    #news-list { height: 1fr; }
    #nav-origin, #nav-destination { width: 1fr; margin-right: 1; }
    #nav-summary { color: $accent; text-style: bold; height: 1; margin: 1 0; }
    #nav-log { height: 1fr; border: round $panel-darken-1; }
    #thread-messages { height: 1fr; }
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
    #onboarding-scroll { height: 1fr; }
    #unlock-error { height: 1; }
    """

    BINDINGS = [
        ("alt+left", "switch_left", "Pane Left"),
        ("alt+right", "switch_right", "Pane Right"),
        ("alt+up", "switch_up", "Pane Up"),
        ("alt+down", "switch_down", "Pane Down"),
        ("tab", "cycle", "Cycle"),
        ("shift+tab", "cycle_back", "Cycle"),
        ("ctrl+1", "goto_tab_mail", "Mail"),
        ("ctrl+2", "goto_tab_calendar", "Calendar"),
        ("ctrl+3", "goto_tab_drive", "Drive"),
        ("ctrl+4", "goto_tab_browser", "Browser"),
        ("ctrl+5", "goto_tab_news", "News"),
        ("ctrl+6", "goto_tab_navigation", "Navigation"),
        ("ctrl+7", "goto_tab_settings", "Settings"),
        ("ctrl+8", "goto_tab_contacts", "Contacts"),
        ("ctrl+left", "cycle_tab_back", "Prev Tab"),
        ("ctrl+right", "cycle_tab", "Next Tab"),
        ("alt+1", "goto_pane_email", "Email"),
        ("alt+2", "goto_pane_events", "Events"),
        ("alt+3", "goto_pane_tasks", "Tasks"),
        ("alt+4", "goto_pane_hermes", "Hermes"),
        ("r", "reply", "Reply"),
        ("a", "reply_all", "Reply All"),
        ("f", "forward", "Forward"),
        ("l", "focus_label_select", "Folder"),
        ("space", "context_space", "Context"),
        ("[", "cal_prev", "Prev"),
        ("]", "cal_next", "Next"),
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+h", "help", "Help"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.active = 0
        self._tasklists = []
        now = dt.datetime.now()
        self._cal_year, self._cal_month = now.year, now.month
        self._cal_by_day: dict[int, list[dict]] = {}
        self._cal_week_cells: dict[tuple[int, int], list[dict]] = {}
        today = dt.date.today()
        self._cal_week_start = today - dt.timedelta(days=today.weekday())
        self._drive_folder_id = "root"
        self._drive_path = "/"
        self._drive_files: list[dict] = []
        self.settings: Settings = load_settings()
        self._current_label_id = self.settings.default_label_id
        self._cache: Cache | None = None
        self._online = False
        self._loading_modal: LoadingModal | None = None
        self._mail_apply_gen = 0
        self._drive_apply_gen = 0
        self._news_apply_gen = 0
        self._news_by_cid: dict[str, dict] = {}
        self._browser_history: list[BrowserHistoryEntry] = []
        self._browser_hist_pos: int = -1
        self._browser_tofu: fetchers.GeminiTofuStore | None = None
        self._browser_started: bool = False
        # Email pane's Space-to-expand (inline snippet preview, not the full
        # ThreadModal — see AGENTS.md's Email-pane NOTE). Naturally resets on
        # every list repopulate (refresh/label change); no persistence needed.
        self._expanded_thread_ids: set[str] = set()
        self._threads_cache: dict[str, dict] = {}
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
        # In-app Google re-authorization — guards against a double-click
        # spawning two concurrent OAuth local-server flows.
        self._google_reauth_in_progress = False
        self._google_reauth_status_id: str | None = None

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
        self._update_help_bar()

    def _adjacent(self, direction: str) -> None:
        if self._main_tabs().active != "tab-mail":
            return
        current_id = PANE_IDS[self.active]
        target_id = PANE_ADJACENCY.get(current_id, {}).get(direction)
        if target_id:
            self._focus_pane(PANE_IDS.index(target_id))

    # ---- help bar ----
    def _context_help_text(self) -> str:
        tab = self._main_tabs().active
        if tab == "tab-mail":
            pane = PANE_IDS[self.active]
            if pane == "email":
                return "Enter Open   r Reply   a Reply All   f Forward   Space Expand   l Folder"
            if pane == "events":
                return "Enter/Space Detail"
            if pane == "tasks":
                return "Space Toggle Complete   Enter Detail"
            if pane == "hermes":
                return "Enter Ask"
        if tab == "tab-calendar":
            return "[ / ] Prev/Next Month or Week   Enter Day Detail"
        if tab == "tab-drive":
            return "Enter Open Folder / Reload Preview"
        if tab == "tab-browser":
            return "Enter Load/Search   Alt+←/→ Back/Forward   Tab Toggle Focus   0-9+Enter Link"
        if tab == "tab-news":
            return "Enter/Space Open Entry"
        if tab == "tab-navigation":
            return "Enter/Go Compute Route   Export Save Itinerary To File"
        if tab == "tab-settings":
            return "Alt+←/→ Switch Section   Toggle encryption   Choose key method   Clear local cache   Manage feeds   Search provider   Routes API key"
        if tab == "tab-contacts":
            return "Type to search   Enter/Space Detail   Compose New   Refresh"
        return ""

    def _update_help_bar(self) -> None:
        try:
            self.query_one("#help-context").update(self._context_help_text())
        except Exception:
            pass

    # ---- compose ----
    def compose(self) -> ComposeResult:
        yield GtHeader()
        with TabbedContent(id="main-tabs", initial="tab-mail"):
            with TabPane(_tab_label("Mail", 1), id="tab-mail"):
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
                            yield ListView(id="email-list")
                    with Vertical(id="right"):
                        with Container(id="events", classes="pane"):
                            yield self._pane_title_row("EVENTS  (upcoming)", 2)
                            yield ListView(id="event-list")
                        with Container(id="tasks", classes="pane"):
                            yield self._pane_title_row("TASKS  (space=done, enter=detail)", 3)
                            yield ListView(id="task-list")
                        with Container(id="hermes", classes="pane"):
                            yield self._pane_title_row("HERMES ASK  (type a question, Enter)", 4)
                            yield RichLog(id="hermes-log", markup=False, wrap=True)
                            yield Input(placeholder="Ask Hermes about your Google stuff…", id="hermes-input")
            with TabPane(_tab_label("Calendar", 2), id="tab-calendar"):
                with Container(id="calendar-section", classes="section"):
                    yield Label("CALENDAR", classes="pane-title-text")
                    with TabbedContent(id="cal-tabs"):
                        with TabPane("Month", id="cal-tab-month"):
                            yield DataTable(id="cal-grid")
                        with TabPane("Week", id="cal-tab-week"):
                            yield DataTable(id="cal-week-grid")
            with TabPane(_tab_label("Drive", 3), id="tab-drive"):
                with Container(id="drive-section", classes="section"):
                    yield Label("/", id="drive-path", classes="muted")
                    with Horizontal(id="drive-body"):
                        with Vertical(id="drive-list-col"):
                            yield ListView(id="drive-list")
                        with VerticalScroll(id="drive-preview-col"):
                            yield Static(id="drive-preview-meta")
                            yield RichLog(id="drive-preview-text", markup=False, wrap=True)
            with TabPane(_tab_label("Browser", 4), id="tab-browser"):
                with Container(id="browser-section", classes="section"):
                    with Horizontal(id="browser-bar"):
                        yield Static("WEB", id="browser-mode")
                        yield Input(placeholder="URL, or type to search…", id="browser-url")
                        yield Button("Go", id="browser-go")
                        yield Static("", id="browser-status")
                    with Horizontal(id="browser-bookmarks"):
                        for i, (label, _url) in enumerate(_BROWSER_BOOKMARKS):
                            yield Button(label, id=f"browser-bookmark-{i}")
                    yield DocumentView(id="browser-doc")
            with TabPane(_tab_label("News", 5), id="tab-news"):
                with Container(id="news-section", classes="section"):
                    yield Label("NEWS  (all subscribed feeds, newest first)", classes="pane-title-text")
                    yield ListView(id="news-list")
            with TabPane(_tab_label("Navigation", 6), id="tab-navigation"):
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
            with TabPane(_tab_label("Settings", 7), id="tab-settings"):
                with Container(id="settings-section", classes="section"):
                    yield Label("SETTINGS", classes="pane-title-text")
                    with TabbedContent(id="settings-tabs"):
                        with TabPane("General", id="settings-tab-general"):
                            with VerticalScroll(id="settings-general-scroll"):
                                yield Label("Google account", classes="pane-title-text")
                                yield Button("Re-authorize Google account", id="settings-reauth-google")
                                yield Static(
                                    "Opens your browser for Google sign-in — no console commands to "
                                    "copy. Use this if a tab shows an auth error, if you just added a "
                                    "new scope (e.g. Contacts), or proactively before your token expires "
                                    "(Google expires test-user tokens ~weekly — see SETUP.md §4).",
                                    id="settings-reauth-note", classes="muted",
                                )
                                yield Static("", id="settings-reauth-status", classes="muted")
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
            with TabPane(_tab_label("Contacts", 8), id="tab-contacts"):
                with Container(id="contacts-section", classes="section"):
                    yield Label("CONTACTS", classes="pane-title-text")
                    with Horizontal(id="contacts-bar", classes="btnrow"):
                        yield Input(placeholder="Search contacts (name or email)…", id="contacts-search")
                        yield Button("Compose New", id="contacts-compose-new")
                        yield Button("Refresh", id="contacts-refresh")
                    yield ListView(id="contacts-list")
        with Vertical(id="help-bar"):
            yield Static("", id="help-context")
            yield Static(HELP_GLOBAL, id="help-global")

    # ---- startup: resolve encryption key, then cache-first load + background sync ----
    def on_mount(self) -> None:
        self._focus_pane(0)
        self._update_help_bar()
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
        threads = gauth.list_threads(self.svc, max_results=80, label_ids=label_ids)
        events = gauth.list_events(self.svc, days=21)
        tasklists = gauth.list_tasklists(self.svc)
        tasks = []
        for tl in tasklists:
            for t in gauth.list_tasks(self.svc, tl["id"], show_completed=True):
                tasks.append({**t, "_list": tl["id"]})
        return label_id, threads, events, tasks, tasklists

    # ---- labels (folders) ----
    def _apply_labels(self, labels: list[dict]) -> None:
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
            threads = gauth.list_threads(self.svc, max_results=80, label_ids=label_ids)
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
        _append_email_items(self.query_one("#email-list"), threads)
        self._threads_cache = {t["threadId"]: t for t in threads}

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

        _append_email_items(self.query_one("#email-list"), threads)
        self._threads_cache = {t["threadId"]: t for t in threads}

        event_list = self.query_one("#event-list")
        for e in events:
            start = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
            event_list.append(
                ListItem(Label(f"{start}  {e.get('summary','')[:40]}"), id=_mk_id("e", e["id"])))

        task_list = self.query_one("#task-list")
        for t in tasks:
            box = "[x]" if t.get("status") == "completed" else "[ ]"
            task_list.append(
                ListItem(Label(f"{box} {t.get('title','')[:50]}"), id=_mk_id("k", f"{t['_list']}-{t['id']}")))

        self._tasks_cache = tasks
        self._events_cache = events

    async def refresh_all(self) -> None:
        try:
            mail = self._fetch_mail_data()
        except Exception as e:
            self.notify(f"Refresh error: {e}", severity="error")
            return
        _, threads, events, tasks, tasklists = mail
        self._write_mail_cache(*mail)
        self._apply_mail_data(threads, events, tasks, tasklists)
        try:
            labels = gauth.list_labels(self.svc)
            if self._cache:
                self._cache.put_many("label", {l["id"]: l for l in labels})
            self._apply_labels(labels)
        except Exception:
            pass
        self.notify(f"Refreshed: {len(threads)} threads, {len(events)} events, {len(tasks)} tasks")

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
                self._contacts_fetch_started = True
                self.run_worker(self._contacts_fetch_thread, thread=True, exclusive=True, group="contacts-fetch")
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

    # ---- email reply/forward from lightbar ----
    def _selected_thread(self) -> str | None:
        el = self.query_one("#email-list")
        if el.highlighted_child is None:
            return None
        cid = el.highlighted_child.id or ""
        return cid[2:] if cid.startswith("t-") else None

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

    def action_focus_label_select(self) -> None:
        if self._main_tabs().active != "tab-mail" or PANE_IDS[self.active] != "email":
            return
        try:
            sel = self.query_one("#email-label-select", Select)
            sel.focus()
            sel.expanded = True
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
    # for why that's a trap this sidesteps entirely by not going there). ----
    def _toggle_thread_expand(self, thread_id: str) -> None:
        th = self._threads_cache.get(thread_id)
        if not th:
            return
        if thread_id in self._expanded_thread_ids:
            self._expanded_thread_ids.discard(thread_id)
            text = _email_collapsed_line(th)
        else:
            self._expanded_thread_ids.add(thread_id)
            snippet = (th.get("snippet") or "").strip()
            if len(snippet) > 100:
                snippet = snippet[:100].rstrip() + "…"
            extra_parts = []
            if snippet:
                extra_parts.append(snippet)
            if th.get("count", 1) > 1:
                extra_parts.append(f"({th['count']} messages)")
            extra = ("\n    " + "  ".join(extra_parts)) if extra_parts else ""
            text = _email_collapsed_line(th) + extra
        try:
            self.query_one(f"#{_mk_id('t', thread_id)} Label", Label).update(text)
        except Exception:
            pass

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

    def action_toggle_task(self):
        if not self._require_online():
            return
        t = self._selected_task()
        if not t:
            return
        done = t.get("status") != "completed"
        gauth.set_task_status(self.svc, t["_list"], t["id"], done)
        self.run_worker(self.refresh_all, exclusive=True)

    # ---- events ----
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
            self.push_screen(ThreadModal(self.svc, cid[2:]), self._on_thread_modal_result)
        elif cid.startswith("e-"):
            self._open_event_by_id(cid[2:])
        elif cid.startswith("k-"):
            t = self._selected_task()
            if t:
                self.push_screen(TaskModal(t))
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

    def _open_compose_from_thread(self, tid: str, mode: str) -> None:
        self.push_screen(ComposeModal(self.svc, tid, mode), self._on_compose_result)

    def _on_compose_result(self, result) -> None:
        if result == "sent":
            self.run_worker(self.refresh_all, exclusive=True)

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

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "contacts-search":
            self._refresh_contacts_list()

    def _hermes_submit(self, event: Input.Submitted) -> None:
        q = event.value.strip()
        if not q:
            return
        event.input.value = ""
        log = self.query_one("#hermes-log")
        log.write(f"You: {q}")
        self.run_worker(self._hermes_worker(q, log), exclusive=False)

    async def _hermes_worker(self, q: str, log: RichLog) -> None:
        provider = ask.get_provider(self.settings.ai_provider, nous_api_key=self.settings.nous_api_key)
        try:
            if needs_agent(q):
                log.write(f"[running {provider.display_name} agent…]")
                ans = provider.run_action(q)
            else:
                ctx = self._build_context()
                sys_prompt = (
                    "You are an assistant answering questions using the user's live "
                    "Google Workspace data provided below. Be concise (couple of "
                    "sentences). If you need to take an action, say so plainly.\n\n"
                    "CONTEXT:\n" + ctx)
                ans = provider.ask(sys_prompt, q)
            log.write(f"{provider.display_name}: {ans}")
        except Exception as e:
            log.write(f"(error: {e})")

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
            return fetchers.fetch_http(target)
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
        if self._main_tabs().active != "tab-browser":
            return
        if event.link.url.startswith("mailto:"):
            self.notify("mailto: links aren't handled by the Browser tab yet", severity="warning")
            return
        self._browser_navigate(event.link.url, push_history=True)

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
        lst = self.query_one("#drive-list")
        if path != "/":
            lst.append(ListItem(Label("📂 .. (up)"), id="d-up"))
        for f in files:
            icon = "📁" if f["mimeType"] == "application/vnd.google-apps.folder" else "📄"
            lst.append(ListItem(Label(f"{icon} {f['name'][:50]}"), id=_mk_id("d", f["id"])))

    def _drive_load(self, folder_id: str = "root", path: str = "/") -> None:
        try:
            files = self._fetch_drive_files(folder_id)
        except Exception as ex:
            self.notify(f"Drive error: {ex}", severity="error")
            files = []
        self._apply_drive_files(files, folder_id, path)

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
            self.query_one("#drive-preview-meta").update("(parent folder)")
            self.query_one("#drive-preview-text").clear()
            return
        if not cid.startswith("d-"):
            return
        fid = cid[2:]
        f = next((x for x in self._drive_files if x["id"] == fid), None)
        if not f:
            return
        self.run_worker(self._drive_preview(f), exclusive=True, group="drive-preview")

    async def _drive_preview(self, f: dict) -> None:
        meta_widget = self.query_one("#drive-preview-meta")
        text_widget = self.query_one("#drive-preview-text")
        text_widget.clear()
        is_folder = f["mimeType"] == "application/vnd.google-apps.folder"
        fid = f["id"]

        if self._online:
            try:
                meta = gauth.get_file_metadata(self.svc, fid)
            except Exception as ex:
                meta_widget.update(f"(metadata error: {ex})")
                return
            if self._cache:
                self._cache.put("drive_file_meta", fid, meta)
        else:
            meta = self._cache.get("drive_file_meta", fid) if self._cache else None
            if meta is None:
                meta_widget.update(f"Name: {f.get('name','')}\n(offline — never viewed online, no cached details)")
                text_widget.write("(not available offline)")
                return

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
        meta_widget.update(info)
        if is_folder:
            text_widget.write("(folder — press Enter to open)")
            return
        if not _is_previewable(meta.get("mimeType", "")):
            text_widget.write("(binary/image file — no text preview)")
            return

        if self._online:
            try:
                _, _, text = gauth.read_drive_text(self.svc, fid)
            except Exception as ex:
                text_widget.write(f"(preview error: {ex})")
                return
            if self._cache:
                self._cache.put("drive_file_text", fid, {"text": text})
            text_widget.write(text[:8000])
        else:
            cached = self._cache.get("drive_file_text", fid) if self._cache else None
            if cached:
                text_widget.write(cached["text"][:8000])
            else:
                text_widget.write("(not available offline — open this file once while online to cache it)")

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
        lst = self.query_one("#news-list")
        self._news_by_cid = {}
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
            lst.append(ListItem(Label(line, markup=False), id=cid))

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
            self.call_from_thread(
                self.notify,
                f"Contacts unavailable: {e} — re-run the OAuth flow with the "
                f"contacts.readonly scope (see SETUP.md §7), then restart.",
                severity="error",
            )
            return
        self._write_contacts_cache(contacts)
        self.call_from_thread(self._apply_contacts_data, contacts)

    def _apply_contacts_data(self, contacts: list[dict]) -> None:
        """Main-thread widget mutation half of the fetch/apply split.
        Stashes the full list (backs ComposeModal's To-field autocomplete,
        which reads self.app._contacts_cache directly) and re-renders the
        list through the current search filter."""
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
        for c in _fuzzy_filter_contacts(self._contacts_cache, query):
            cid = _mk_id("ct", c.get("resource_name", ""))
            self._contacts_by_cid[cid] = c
            name = (c.get("name") or "(no name)")[:30]
            email = (c.get("email") or "")[:40]
            lst.append(ListItem(Label(f"{name:<30} {email}", markup=False), id=cid))

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
            result = fetchers.compute_route(origin, destination, self.settings.routes_api_key)
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
    # copy-a-script-and-run-it-yourself process) ----
    def _start_google_reauth(self, button_id: str, status_id: str | None = None) -> None:
        if self._google_reauth_in_progress:
            return  # already running — the local server binds its own port
                    # (port=0), so a second click wouldn't conflict, just confuse
        self._google_reauth_in_progress = True
        try:
            self.query_one(f"#{button_id}", Button).disabled = True
        except Exception:
            pass
        self._google_reauth_status_id = status_id
        self.notify(
            "Opening your browser for Google sign-in… complete the consent "
            "screen there. (If no browser opens, check the terminal/log for "
            "the URL to open manually.)"
        )
        if status_id:
            try:
                self.query_one(f"#{status_id}", Static).update("Waiting for browser sign-in…")
            except Exception:
                pass
        self.run_worker(self._google_reauth_thread, thread=True, exclusive=True, group="google-reauth")

    def _google_reauth_thread(self) -> None:
        # gauth.reauthorize BLOCKS until the browser consent flow completes
        # (or times out) — same "never on the main thread" rule as every
        # other gauth call in this app, doubly so here since this one can
        # block for minutes waiting on a human, not just a network round trip.
        try:
            gauth.reauthorize()
        except Exception as e:
            self.call_from_thread(self._on_google_reauth_error, e)
            return
        self.call_from_thread(self._on_google_reauth_success)

    def _on_google_reauth_success(self) -> None:
        self._google_reauth_in_progress = False
        for bid in ("settings-reauth-google", "onboarding-reauth-google"):
            try:
                self.query_one(f"#{bid}", Button).disabled = False
            except Exception:
                pass
        status_id = getattr(self, "_google_reauth_status_id", None)
        if status_id:
            try:
                self.query_one(f"#{status_id}", Static).update("Re-authorized.")
            except Exception:
                pass
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
        # If this fired from the forced first-run onboarding modal, close it
        # out through the same path Retry uses — _on_onboarding_result defers
        # to _continue_startup via call_after_refresh (push_screen callback
        # timing NOTE, §2).
        if isinstance(self.screen, OnboardingWizardModal):
            self.screen.dismiss("resolved")

    def _on_google_reauth_error(self, error: Exception) -> None:
        self._google_reauth_in_progress = False
        for bid in ("settings-reauth-google", "onboarding-reauth-google"):
            try:
                self.query_one(f"#{bid}", Button).disabled = False
            except Exception:
                pass
        status_id = getattr(self, "_google_reauth_status_id", None)
        if status_id:
            try:
                self.query_one(f"#{status_id}", Static).update(f"Re-authorization failed: {error}")
            except Exception:
                pass
        self.notify(f"Google re-authorization failed: {error}", severity="error")

    def _update_settings_cache_info(self) -> None:
        try:
            size = cache_mod.CACHE_DB_PATH.stat().st_size
            info = f"{cache_mod.CACHE_DB_PATH}  ({size:,} bytes)"
        except FileNotFoundError:
            info = f"{cache_mod.CACHE_DB_PATH}  (not created yet)"
        try:
            self.query_one("#settings-cache-info").update(info)
        except Exception:
            pass

    def on_switch_changed(self, event: Switch.Changed) -> None:
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
            self._start_google_reauth(button_id="settings-reauth-google", status_id="settings-reauth-status")
        elif event.button.id == "settings-clear-cache":
            if self._cache:
                self._cache.clear_all()
            self.notify("Local cache cleared.")
            self._update_settings_cache_info()
        elif event.button.id == "settings-save-nous-key":
            key = self.query_one("#settings-nous-key", Input).value.strip()
            self.settings.nous_api_key = key or None
            save_settings(self.settings)
            self.notify("Nous API key saved.")
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
            self._contacts_fetch_started = True
            self.run_worker(self._contacts_fetch_thread, thread=True, exclusive=True, group="contacts-fetch")
        elif event.button.id == "contacts-compose-new":
            self._open_compose_new()


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
                    # missing scope) — gauth.reauthorize() reuses the OAuth
                    # client embedded in it. A genuinely first-ever setup
                    # (no token file yet) still needs the manual walkthrough
                    # below; this button will just surface that as an error.
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
            # self._online/etc.), not this modal — on success it dismisses
            # this screen itself (checks isinstance(self.screen,
            # OnboardingWizardModal)) rather than round-tripping back here.
            self._app_ref._start_google_reauth(button_id="onboarding-reauth-google")
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
    message's `[N]` link markers to stay unique across the whole thread,
    which `on_document_view_link_activated` doesn't even act on today while
    a non-Browser tab (e.g. this modal, opened over Mail) is active — same
    no-op as `NewsEntryModal`'s links — so that complexity isn't earning
    its keep yet. A v1 simplification, not an oversight.
    """

    def __init__(self, svc, thread_id: str):
        super().__init__()
        self.svc = svc
        self.thread_id = thread_id

    def compose(self) -> ComposeResult:
        with Container(id="thread-box", classes="pane"):
            yield Label("THREAD", classes="pane-title-text")
            with VerticalScroll(id="thread-messages"):
                yield Static("Loading…", markup=False)
        with Horizontal(classes="btnrow"):
            yield Button("Reply", id="r")
            yield Button("Reply All", id="ra")
            yield Button("Forward", id="fwd")
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        # gauth.get_thread is a blocking synchronous network call (same as
        # every other gauth-touching method in this app) — must run on a
        # worker THREAD, not directly in on_mount, both so it doesn't freeze
        # the UI and so a transient network error (e.g. an SSL hiccup while
        # the app's own background reconnect is still in flight) is caught
        # and shown as a notify instead of crashing the whole app.
        self.run_worker(self._fetch_thread, thread=True, exclusive=True)

    def _fetch_thread(self) -> None:
        try:
            msgs = gauth.get_thread(self.svc, self.thread_id)
        except Exception as e:
            self.app.call_from_thread(self._apply_error, e)
            return
        self.app.call_from_thread(self._apply_thread, msgs)
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
        container = self.query_one("#thread-messages", VerticalScroll)
        await container.remove_children()
        if not msgs:
            await container.mount(Static("(no messages)", markup=False))
            return
        pending: list[tuple[DocumentView, "render.Document"]] = []
        new_widgets = []
        for m in msgs:
            header = f"From: {m.get('from', '')}    Date: {m.get('date', '')}"
            new_widgets.append(Static(header, classes="thread-msg-header", markup=False))
            html_body = (m.get("html_body") or "").strip()
            text_body = m.get("body") or ""
            source = html_body if html_body else text_body
            doc = render.parse_feed_entry(m.get("subject", ""), source, base_url="")
            dv = DocumentView(classes="thread-msg-doc")
            new_widgets.append(dv)
            pending.append((dv, doc))
        await container.mount(*new_widgets)
        for dv, doc in pending:
            # DocumentView's own DEFAULT_CSS sets height:1fr (correct for
            # its usual full-pane use in the Browser/News tabs); stacking
            # several inside one VerticalScroll needs auto height instead
            # so each message takes only the space its content needs.
            dv.styles.height = "auto"
            dv.document = doc

    def _apply_error(self, error: Exception) -> None:
        container = self.query_one("#thread-messages", VerticalScroll)
        container.remove_children()
        container.mount(Static(f"Couldn't load this thread:\n{error}", markup=False))
        self.app.notify(f"Thread load error: {error}", severity="error")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "close":
            self.dismiss(None)
        else:
            mode = {"r": "reply", "ra": "reply_all", "fwd": "forward"}[e.button.id]
            self.dismiss(("compose", self.thread_id, mode))

    def on_key(self, e) -> None:
        if e.key == "escape":
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
            label = f"{c.get('name') or '(no name)'}  <{c.get('email','')}>"
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


class TaskModal(ModalScreen):
    def __init__(self, task: dict):
        super().__init__()
        self.task = task

    def compose(self) -> ComposeResult:
        with Container(id="tk-box", classes="pane"):
            yield Label("TASK DETAIL", classes="pane-title-text")
            yield Static(id="tk-detail")
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        t = self.task
        det = (f"Title: {t.get('title','')}\nStatus: {t.get('status','')}\n"
               f"Due:   {t.get('due','')}\n\nNotes:\n{t.get('notes','')}")
        self.query_one("#tk-detail").update(det)

    def on_button_pressed(self, e):
        self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape":
            self.dismiss(None)


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
        doc = render.parse_feed_entry(title, body, base_url=e.get("link", ""))
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
            yield Static(HELP_TEXT, id="help-modal-text")
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
    GoogleTUI().run()


if __name__ == "__main__":
    main()
