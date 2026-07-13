"""google-tui — multi-pane TUI for Gmail / Calendar / Tasks / Drive / Search / Hermes.

Top-level layout is four full-width TABS in the blue bar: Mail, Calendar,
Drive, Search (Ctrl+1..4). The Mail tab holds four PANES: Email, Events,
Tasks, Hermes (Alt+1..4, or Alt+arrows to move relatively). See AGENTS.md
for the full keybinding reference and the PANE_ADJACENCY rationale.
"""
from __future__ import annotations

import base64
import datetime as dt
from functools import cached_property

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Header, Input, Label, ListItem, ListView,
    RadioButton, RadioSet, RichLog, Select, Static, Switch, TabbedContent, TabPane, TextArea,
)
from textual.worker import get_current_worker  # noqa: F401 (kept for future threaded workers)

from . import gauth
from .ask import ask_llm, ask_hermes_agent, needs_agent, google_search
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

TAB_ORDER = ["tab-mail", "tab-calendar", "tab-drive", "tab-search", "tab-settings"]

_SUPERSCRIPT = {1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵"}

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
  Ctrl+1..5        Switch tab (Mail / Calendar / Drive / Search / Settings)
  Ctrl+Left/Right  Cycle tabs (use this if Ctrl+1..5 doesn't reach the app —
                   some terminals/browsers don't transmit Ctrl+digit)
  Alt+1..4         Jump to Mail pane (Email / Events / Tasks / Hermes)
  Alt+arrows       Move to the adjacent Mail pane
  Tab / Shift+Tab  Cycle Mail panes
  Ctrl+R           Reconnect / refresh live data
  Ctrl+P           Command palette
  Ctrl+H           This help
  Ctrl+Q           Quit

MAIL TAB
  Email pane:   Enter/Space open thread, r Reply, a Reply All, f Forward
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

SEARCH TAB
  Enter         Run the search

SETTINGS TAB
  Switch        Toggle encrypt-at-rest for the local cache
  RadioSet      Choose passphrase-at-launch vs. local key file
  Button        Clear the local cache immediately

Reply/Forward/Toggle-complete are disabled while offline (shown in the
title bar as "Offline (cached HH:MM)"); browsing cached data still works.
"""


def _fmt_date(s: str) -> str:
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.strftime("%m/%d %I:%M%p")
    except Exception:
        return s


def _mk_id(prefix: str, raw: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)
    return f"{prefix}-{safe}"


def _tab_label(text: str, num: int) -> str:
    return f"{text} [dim]{_SUPERSCRIPT[num]}[/dim]"


def _append_email_items(email_list, threads) -> None:
    for th in threads:
        mark = "•" if th["unread"] else " "
        subj = th["subject"] or "(no subject)"
        line = f"{mark} {th['from'][:36]:<36} {subj[:60]}  ({th['count']})"
        email_list.append(ListItem(Label(line), id=_mk_id("t", th["threadId"])))


def _event_day(e: dict) -> int | None:
    s = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).day
    except Exception:
        return None


def _is_previewable(mime: str) -> bool:
    return mime.startswith(_PREVIEWABLE_PREFIXES) or mime in _PREVIEWABLE_EXTRA


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
    #search-results { height: 1fr; border: round $panel-darken-1; }
    #help-bar { height: auto; background: $panel; padding: 0 1; }
    #help-context { color: $text; }
    #help-global { color: $text-muted; }
    .settings-row { height: 3; align: left middle; }
    .settings-row Label { width: auto; margin-right: 2; }
    .hidden { display: none; }
    #settings-key-method { height: auto; margin: 1 0; }
    #settings-cache-info { margin-top: 1; }
    #unlock-box { height: auto; }
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
        ("ctrl+4", "goto_tab_search", "Search"),
        ("ctrl+5", "goto_tab_settings", "Settings"),
        ("ctrl+left", "cycle_tab_back", "Prev Tab"),
        ("ctrl+right", "cycle_tab", "Next Tab"),
        ("alt+1", "goto_pane_email", "Email"),
        ("alt+2", "goto_pane_events", "Events"),
        ("alt+3", "goto_pane_tasks", "Tasks"),
        ("alt+4", "goto_pane_hermes", "Hermes"),
        ("r", "reply", "Reply"),
        ("a", "reply_all", "Reply All"),
        ("f", "forward", "Forward"),
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
                return "Enter Open   r Reply   a Reply All   f Forward   Space Expand"
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
        if tab == "tab-search":
            return "Enter Run Search"
        if tab == "tab-settings":
            return "Toggle encryption   Choose key method   Clear local cache"
        return ""

    def _update_help_bar(self) -> None:
        try:
            self.query_one("#help-context").update(self._context_help_text())
        except Exception:
            pass

    # ---- compose ----
    def compose(self) -> ComposeResult:
        yield Header()
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
            with TabPane(_tab_label("Search", 4), id="tab-search"):
                with Container(id="search-section", classes="section"):
                    yield Label("GOOGLE SEARCH", classes="pane-title-text")
                    yield Input(placeholder="Search query, Enter to run", id="s-query")
                    yield RichLog(id="search-results", markup=False, wrap=True)
            with TabPane(_tab_label("Settings", 5), id="tab-settings"):
                with Container(id="settings-section", classes="section"):
                    yield Label("SETTINGS", classes="pane-title-text")
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
        with Vertical(id="help-bar"):
            yield Static("", id="help-context")
            yield Static(HELP_GLOBAL, id="help-global")

    # ---- startup: resolve encryption key, then cache-first load + background sync ----
    def on_mount(self) -> None:
        self._focus_pane(0)
        self._update_help_bar()
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

        return had_mail or bool(drive_files)

    def _write_mail_cache(self, label_id, threads, events, tasks, tasklists) -> None:
        if not self._cache:
            return
        self._cache.put_many(f"thread_summary:{label_id}", {t["threadId"]: t for t in threads})
        self._cache.put_many("event", {e["id"]: e for e in events})
        self._cache.put_many("task", {f"{t['_list']}-{t['id']}": t for t in tasks})
        self._cache.put_many("tasklist", {tl["id"]: tl for tl in tasklists})

    def _live_refresh_thread(self) -> None:
        mail = cal_month = cal_week = drive_files = labels = None
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
        self.call_from_thread(self._apply_live_refresh, ok, mail, cal_month, cal_week, drive_files, labels)

    def _apply_live_refresh(self, ok: bool, mail, cal_month, cal_week, drive_files, labels) -> None:
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
    def action_goto_tab_search(self):   self._goto_tab("tab-search")
    def action_goto_tab_settings(self): self._goto_tab("tab-settings")

    def _cycle_tab(self, step: int) -> None:
        current = self._main_tabs().active
        idx = TAB_ORDER.index(current) if current in TAB_ORDER else 0
        self._goto_tab(TAB_ORDER[(idx + step) % len(TAB_ORDER)])

    def action_cycle_tab(self):      self._cycle_tab(1)
    def action_cycle_tab_back(self): self._cycle_tab(-1)

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
        elif tab_id == "tab-search":
            self.query_one("#s-query").focus()
        elif tab_id == "tab-settings":
            self._update_settings_cache_info()
            self.query_one("#settings-encrypt-switch").focus()
        self._update_help_bar()

    # ---- pane switching (Mail tab) ----
    def _goto_pane(self, idx: int) -> None:
        self._goto_tab("tab-mail")
        self._focus_pane(idx)

    def action_goto_pane_email(self):  self._goto_pane(0)
    def action_goto_pane_events(self): self._goto_pane(1)
    def action_goto_pane_tasks(self):  self._goto_pane(2)
    def action_goto_pane_hermes(self): self._goto_pane(3)

    def action_switch_left(self):  self._adjacent("left")
    def action_switch_right(self): self._adjacent("right")
    def action_switch_up(self):    self._adjacent("up")
    def action_switch_down(self):  self._adjacent("down")

    def action_cycle(self):
        if self._main_tabs().active == "tab-mail":
            self._focus_pane((self.active + 1) % len(PANE_IDS))

    def action_cycle_back(self):
        if self._main_tabs().active == "tab-mail":
            self._focus_pane((self.active - 1) % len(PANE_IDS))

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

    def _require_online(self) -> bool:
        if not self._online:
            self.notify("Can't do that while offline", severity="warning")
            return False
        return True

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
        if self._main_tabs().active != "tab-mail":
            return
        pane = PANE_IDS[self.active]
        if pane == "tasks":
            self.action_toggle_task()
        elif pane == "email":
            tid = self._selected_thread()
            if tid:
                self.push_screen(ThreadModal(self.svc, tid), self._on_thread_modal_result)
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

    # ---- hermes ask / search (shared Input.Submitted) ----
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "hermes-input":
            self._hermes_submit(event)
        elif event.input.id == "s-query":
            self._search_submit(event)

    def _hermes_submit(self, event: Input.Submitted) -> None:
        q = event.value.strip()
        if not q:
            return
        event.input.value = ""
        log = self.query_one("#hermes-log")
        log.write(f"You: {q}")
        self.run_worker(self._hermes_worker(q, log), exclusive=False)

    async def _hermes_worker(self, q: str, log: RichLog) -> None:
        try:
            if needs_agent(q):
                log.write("[running full Hermes agent…]")
                ans = ask_hermes_agent(q)
            else:
                ctx = self._build_context()
                sys_prompt = (
                    "You are Hermes, answering questions using the user's live Google "
                    "Workspace data provided below. Be concise (couple of sentences). "
                    "If you need to take an action, say so plainly.\n\nCONTEXT:\n" + ctx)
                ans = ask_llm(sys_prompt, q)
            log.write(f"Hermes: {ans}")
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

    def _search_submit(self, event: Input.Submitted) -> None:
        q = event.value.strip()
        if not q:
            return
        event.input.value = ""
        log = self.query_one("#search-results")
        log.write(f"Searching: {q} …")
        self.run_worker(self._search_worker(q, log), exclusive=False)

    async def _search_worker(self, q: str, log: RichLog) -> None:
        try:
            res = google_search(q)
        except Exception as ex:
            res = f"(error: {ex})"
        log.write(res)

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

    # ---- settings tab ----
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
        if event.button.id == "settings-clear-cache":
            if self._cache:
                self._cache.clear_all()
            self.notify("Local cache cleared.")
            self._update_settings_cache_info()


# ============================================================================
# Modals
# ============================================================================

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
    def __init__(self, svc, thread_id: str):
        super().__init__()
        self.svc = svc
        self.thread_id = thread_id

    def compose(self) -> ComposeResult:
        with Container(id="thread-box", classes="pane"):
            yield Label("THREAD", classes="pane-title-text")
            yield RichLog(id="thread-body", markup=False, wrap=True)
        with Horizontal(classes="btnrow"):
            yield Button("Reply", id="r")
            yield Button("Reply All", id="ra")
            yield Button("Forward", id="fwd")
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        msgs = gauth.get_thread(self.svc, self.thread_id)
        txt = []
        for m in msgs:
            txt.append(f"From: {m['from']}\nDate: {m['date']}\nSubject: {m['subject']}\n\n{m['body'][:4000]}\n{'='*60}")
        body = self.query_one("#thread-body")
        body.clear()
        body.write("\n".join(txt))
        gauth.mark_read(self.svc, self.thread_id)

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
    SEND_COUNTDOWN_SECONDS = 5

    def __init__(self, svc, thread_id: str, mode: str):
        super().__init__()
        self.svc = svc
        self.thread_id = thread_id
        self.mode = mode
        self._countdown_remaining = 0
        self._countdown_timer = None  # Textual Timer handle while a send is pending

    def compose(self) -> ComposeResult:
        with Container(id="compose-box", classes="pane"):
            yield Label("COMPOSE", classes="pane-title-text")
            yield Input(placeholder="To", id="c-to")
            yield Input(placeholder="Subject", id="c-subject")
            yield TextArea(id="c-body", language="markdown")
        with Horizontal(classes="btnrow"):
            yield Button("Send", id="send")
            yield Button("Cancel", id="cancel")
        yield Static("", id="send-countdown")

    def on_mount(self) -> None:
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
        body = self.query_one("#c-body").text
        if self.mode == "forward":
            gauth.forward(self.svc, self.thread_id, to, body_prefix=body + "\n")
        else:
            gauth.reply_to(self.svc, self.thread_id, body, reply_all=(self.mode == "reply_all"))
        self.dismiss("sent")

    def on_key(self, e) -> None:
        if e.key == "escape":
            if self._countdown_timer is not None:
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


def main():
    GoogleTUI().run()


if __name__ == "__main__":
    main()
