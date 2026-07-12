"""google-tui — multi-pane TUI for Gmail / Calendar / Tasks / Drive / Search / Hermes.

Layout:
  LEFT  : Email (threaded, full height)
  RIGHT : Calendar (upcoming) | Tasks | Hermes Ask (compact)
  Buttons: [Calendar full view] [Drive] [Search]

Switch panes with Alt+Left/Right/Up/Down, or Tab/Shift+Tab, or 1-4.
"""
from __future__ import annotations

import datetime as dt
from functools import cached_property

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, ListItem, ListView,
    RichLog, Static, TabbedContent, TabPane, TextArea,
)
from textual.worker import get_current_worker  # noqa: F401 (kept for future threaded workers)

from . import gauth
from .ask import ask_llm, ask_hermes_agent, needs_agent, google_search

PANE_IDS = ["email", "calendar", "tasks", "hermes"]
PANE_TITLES = {
    "email": "EMAIL",
    "calendar": "CALENDAR",
    "tasks": "TASKS",
    "hermes": "HERMES ASK",
}


def _fmt_date(s: str) -> str:
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.strftime("%m/%d %I:%M%p")
    except Exception:
        return s


def _mk_id(prefix: str, raw: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)
    return f"{prefix}-{safe}"


class GoogleTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #top { height: 1; background: $primary; color: $text; }
    #body { height: 1fr; }
    #left { width: 45%; border: round $panel-darken-1; }
    #right { width: 1fr; }
    .pane { height: 1fr; border: round $panel-darken-2; padding: 0 1; }
    .pane-active { border: round $accent; }
    .pane-title { text-style: bold; color: $accent; height: 1; }
    #email-list { height: 1fr; }
    #cal-list, #task-list { height: 1fr; }
    #hermes-log { height: 1fr; border: round $panel-darken-1; }
    #hermes-input { dock: bottom; }
    .btnrow { height: 3; align: left middle; }
    .muted { color: $text-muted; }
    #cal-modal-grid { height: 1fr; }
    """

    BINDINGS = [
        ("alt+left", "switch_left", "Pane Left"),
        ("alt+right", "switch_right", "Pane Right"),
        ("alt+up", "switch_up", "Pane Up"),
        ("alt+down", "switch_down", "Pane Down"),
        ("tab", "cycle", "Cycle"),
        ("shift+tab", "cycle_back", "Cycle"),
        ("1", "goto_email", "Email"),
        ("2", "goto_calendar", "Calendar"),
        ("3", "goto_tasks", "Tasks"),
        ("4", "goto_hermes", "Hermes"),
        ("r", "reply", "Reply"),
        ("a", "reply_all", "Reply All"),
        ("f", "forward", "Forward"),
        ("space", "toggle_task", "Toggle Task"),
        ("c", "open_calendar", "Calendar View"),
        ("d", "open_drive", "Drive"),
        ("s", "open_search", "Search"),
        ("ctrl+r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.active = 0
        self._svc = None
        self._tasklists = []

    # ---- data layer ----
    @cached_property
    def svc(self):
        return gauth.services()

    def _focus_pane(self, idx: int) -> None:
        self.active = idx % len(PANE_IDS)
        for pid in PANE_IDS:
            try:
                c = self.query_one(f"#{pid}")
                c.remove_class("pane-active")
            except Exception:
                pass
        pid = PANE_IDS[self.active]
        c = self.query_one(f"#{pid}")
        c.add_class("pane-active")
        # focus the inner interactive widget
        targets = {
            "email": "#email-list",
            "calendar": "#cal-list",
            "tasks": "#task-list",
            "hermes": "#hermes-input",
        }
        try:
            self.query_one(targets[pid]).focus()
        except Exception:
            pass

    # ---- compose ----
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading Google data…", id="top")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                with Container(id="email", classes="pane"):
                    yield Label("EMAIL  (threads)", classes="pane-title")
                    yield ListView(id="email-list")
            with Vertical(id="right"):
                with Container(id="calendar", classes="pane"):
                    yield Label("CALENDAR  (upcoming)  [c]=full view", classes="pane-title")
                    yield ListView(id="cal-list")
                with Container(id="tasks", classes="pane"):
                    yield Label("TASKS  (space=done, enter=detail)", classes="pane-title")
                    yield ListView(id="task-list")
                with Container(id="hermes", classes="pane"):
                    yield Label("HERMES ASK  (type a question, Enter)", classes="pane-title")
                    yield RichLog(id="hermes-log", markup=False, wrap=True)
                    yield Input(placeholder="Ask Hermes about your Google stuff…", id="hermes-input")
        with Horizontal(classes="btnrow"):
            yield Button("📅 Calendar", id="btn-cal")
            yield Button("📁 Drive", id="btn-drive")
            yield Button("🔍 Search", id="btn-search")
            yield Button("↻ Refresh", id="btn-refresh")
        yield Footer()

    def on_mount(self) -> None:
        self._focus_pane(0)
        self.run_worker(self.refresh_all, exclusive=True)

    # ---- refresh ----
    async def refresh_all(self) -> None:
        try:
            self.query_one("#top").update("Refreshing…")
            threads = gauth.list_threads(self.svc, max_results=80)
            events = gauth.list_events(self.svc, days=21)
            self._tasklists = gauth.list_tasklists(self.svc)
            tasks = []
            for tl in self._tasklists:
                for t in gauth.list_tasks(self.svc, tl["id"], show_completed=True):
                    tasks.append({**t, "_list": tl["id"]})
        except Exception as e:
            self.query_one("#top").update(f"Error: {e}")
            return

        for th in threads:
            mark = "•" if th["unread"] else " "
            subj = th["subject"] or "(no subject)"
            line = f"{mark} {th['from'][:28]:<28} {subj[:40]}  ({th['count']})"
            self.query_one("#email-list").append(ListItem(Label(line), id=_mk_id("t", th["threadId"])))

        for e in events:
            start = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
            self.query_one("#cal-list").append(
                ListItem(Label(f"{start}  {e.get('summary','')[:40]}"), id=_mk_id("e", e["id"])))

        for t in tasks:
            box = "[x]" if t.get("status") == "completed" else "[ ]"
            self.query_one("#task-list").append(
                ListItem(Label(f"{box} {t.get('title','')[:50]}"), id=_mk_id("k", f"{t['_list']}-{t['id']}")))

        self.query_one("#top").update(
            f"Email: {len(threads)} threads | Events: {len(events)} | Tasks: {len(tasks)}")
        self._tasks_cache = tasks
        self._events_cache = events


    def action_switch_left(self):  self._focus_pane(self.active - 1)
    def action_switch_right(self): self._focus_pane(self.active + 1)
    def action_switch_up(self):    self._focus_pane(self.active - 2)
    def action_switch_down(self):  self._focus_pane(self.active + 2)
    def action_cycle(self):        self._focus_pane(self.active + 1)
    def action_cycle_back(self):   self._focus_pane(self.active - 1)
    def action_goto_email(self):   self._focus_pane(0)
    def action_goto_calendar(self):self._focus_pane(1)
    def action_goto_tasks(self):   self._focus_pane(2)
    def action_goto_hermes(self):  self._focus_pane(3)
    def action_refresh(self):      self.run_worker(self.refresh_all, exclusive=True)

    def action_open_calendar(self): self.push_screen(CalendarModal(self.svc))
    def action_open_drive(self):    self.push_screen(DriveModal(self.svc))
    def action_open_search(self):   self.push_screen(SearchModal())

    # email reply/forward from lightbar
    def _selected_thread(self) -> str | None:
        el = self.query_one("#email-list")
        if el.highlighted_child is None:
            return None
        cid = el.highlighted_child.id or ""
        return cid[2:] if cid.startswith("t-") else None

    def action_reply(self):
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="reply"))
    def action_reply_all(self):
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="reply_all"))
    def action_forward(self):
        tid = self._selected_thread()
        if tid:
            self.push_screen(ComposeModal(self.svc, tid, mode="forward"))

    # tasks
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
        t = self._selected_task()
        if not t:
            return
        done = t.get("status") != "completed"
        gauth.set_task_status(self.svc, t["_list"], t["id"], done)
        self.run_worker(self.refresh_all, exclusive=True)

    # event select -> detail
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        cid = event.item.id or ""
        if cid.startswith("t-"):
            tid = cid[2:]
            self.push_screen(ThreadModal(self.svc, tid))
        elif cid.startswith("e-"):
            eid = cid[2:]
            for e in getattr(self, "_events_cache", []):
                if e.get("id") == eid:
                    self.push_screen(EventModal(e))
                    break
        elif cid.startswith("k-"):
            t = self._selected_task()
            if t:
                self.push_screen(TaskModal(t))

    # modal returns (ThreadModal/Compose/etc. may return action tuples)
    def on_dismiss(self, event: ModalScreen.Dismissed) -> None:
        result = event.result
        if isinstance(result, tuple):
            kind = result[0]
            if kind == "compose":
                _, tid, mode = result
                self.push_screen(ComposeModal(self.svc, tid, mode))
            elif kind == "drive":
                _, fid, path = result
                self.push_screen(DriveModal(self.svc, fid, path))
            elif kind == "up":
                # re-open drive at parent (simplify: reopen root)
                self.push_screen(DriveModal(self.svc))
            elif kind == "text":
                _, name, text = result
                self.push_screen(DriveFileModal(name, text))
            elif kind == "error":
                self.query_one("#top").update(f"Drive error: {result[1]}")
        elif result == "sent":
            self.run_worker(self.refresh_all, exclusive=True)

    # buttons
    def on_button_pressed(self, event: Button.Pressed) -> None:
        b = event.button.id
        if b == "btn-cal":    self.push_screen(CalendarModal(self.svc))
        elif b == "btn-drive":self.push_screen(DriveModal(self.svc))
        elif b == "btn-search":self.push_screen(SearchModal())
        elif b == "btn-refresh": self.action_refresh()

    # hermes ask
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "hermes-input":
            return
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


# ============================================================================
# Modals
# ============================================================================

class ThreadModal(ModalScreen):
    def __init__(self, svc, thread_id: str):
        super().__init__()
        self.svc = svc
        self.thread_id = thread_id

    def compose(self) -> ComposeResult:
        with Container(id="thread-box", classes="pane"):
            yield Label("THREAD", classes="pane-title")
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
    def __init__(self, svc, thread_id: str, mode: str):
        super().__init__()
        self.svc = svc
        self.thread_id = thread_id
        self.mode = mode

    def compose(self) -> ComposeResult:
        with Container(id="compose-box", classes="pane"):
            yield Label("COMPOSE", classes="pane-title")
            yield Input(placeholder="To", id="c-to")
            yield Input(placeholder="Subject", id="c-subject")
            yield TextArea(id="c-body", language="markdown")
        with Horizontal(classes="btnrow"):
            yield Button("Send", id="send")
            yield Button("Cancel", id="cancel")

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
            self.dismiss(None)
            return
        to = self.query_one("#c-to").value.strip()
        subject = self.query_one("#c-subject").value.strip()
        body = self.query_one("#c-body").text
        if not to:
            return
        if self.mode == "forward":
            gauth.forward(self.svc, self.thread_id, to, body_prefix=body + "\n")
        else:
            gauth.reply_to(self.svc, self.thread_id, body, reply_all=(self.mode == "reply_all"))
        self.dismiss("sent")

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class EventModal(ModalScreen):
    def __init__(self, event: dict):
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        with Container(id="ev-box", classes="pane"):
            yield Label("APPOINTMENT DETAIL", classes="pane-title")
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
            yield Label("TASK DETAIL", classes="pane-title")
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


class CalendarModal(ModalScreen):
    def __init__(self, svc):
        super().__init__()
        self.svc = svc
        now = dt.datetime.now()
        self.year, self.month = now.year, now.month

    def compose(self) -> ComposeResult:
        with Container(id="cal-modal", classes="pane"):
            yield Label("CALENDAR  ([ and ] = change month)", classes="pane-title")
            with TabbedContent(id="cal-tabs"):
                with TabPane("Month", id="tab-month"):
                    yield DataTable(id="cal-grid")
                    yield ListView(id="cal-day-events")
                with TabPane("Week", id="tab-week"):
                    yield ListView(id="cal-week")
        yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._build_month()

    def _build_month(self) -> None:
        grid = self.query_one("#cal-grid")
        grid.clear(columns=True)
        grid.add_columns("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        events = {e["id"]: e for e in gauth.month_events(self.svc, self.year, self.month)}
        # map day -> count
        daycount = {}
        for e in events.values():
            d = _fmt_date(e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))
            try:
                dd = int(d.split()[0].split("/")[1])
                daycount[dd] = daycount.get(dd, 0) + 1
            except Exception:
                pass
        first = dt.date(self.year, self.month, 1)
        offset = (first.weekday())  # Mon=0
        days = (dt.date(self.year + (1 if self.month == 12 else 0), 1 if self.month == 12 else self.month + 1, 1) - first).days
        cells = [""] * offset
        for d in range(1, days + 1):
            cells.append(f"{d}\n•{daycount.get(d,0)}" if daycount.get(d) else str(d))
        while len(cells) % 7:
            cells.append("")
        for i in range(0, len(cells), 7):
            grid.add_row(*cells[i:i+7])
        self._events = events

    def on_data_table_cell_highlighted(self, e) -> None:
        # show that day's events
        try:
            row = e.coordinate.row
            col = e.coordinate.column
        except Exception:
            return
        grid = self.query_one("#cal-grid")
        val = grid.get_cell_at((row, col))
        if not val or not any(ch.isdigit() for ch in str(val)):
            return
        day = int("".join(ch for ch in str(val) if ch.isdigit()))
        try:
            thedate = dt.date(self.year, self.month, day)
        except Exception:
            return
        lst = self.query_one("#cal-day-events")
        lst.clear()
        for e in self._events.values():
            ds = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
            try:
                ed = dt.datetime.fromisoformat(ds.replace("Z", "+00:00")).date()
            except Exception:
                continue
            if ed == thedate:
                lst.append(ListItem(Label(f"{e.get('summary','')[:40]}")))

    def on_key(self, e) -> None:
        if e.key == "[":
            self.month -= 1
            if self.month == 0:
                self.month = 12; self.year -= 1
            self._build_month()
        elif e.key == "]":
            self.month += 1
            if self.month == 13:
                self.month = 1; self.year += 1
            self._build_month()
        elif e.key == "escape":
            self.dismiss(None)

    def on_button_pressed(self, e):
        self.dismiss(None)


class DriveModal(ModalScreen):
    def __init__(self, svc, folder_id: str = "root", path: str = "/"):
        super().__init__()
        self.svc = svc
        self.folder_id = folder_id
        self.path = path

    def compose(self) -> ComposeResult:
        with Container(id="drive-modal", classes="pane"):
            yield Label("DRIVE  (enter=open/nav, [..]=up, r=read)", classes="pane-title")
            yield ListView(id="drive-list")
        with Horizontal(classes="btnrow"):
            yield Button("Read", id="read")
            yield Button("Up", id="up")
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        files = gauth.list_drive(self.svc, self.folder_id)
        self._files = files
        lst = self.query_one("#drive-list")
        lst.clear()
        lst.append(ListItem(Label("📂 .. (up)"), id="d-up"))
        for f in files:
            icon = "📁" if f["mimeType"] == "application/vnd.google-apps.folder" else "📄"
            lst.append(ListItem(Label(f"{icon} {f['name'][:50]}"), id=_mk_id("d", f["id"])))

    def on_list_view_selected(self, e: ListView.Selected) -> None:
        cid = e.item.id or ""
        if not cid.startswith("d-"):
            return
        fid = cid[2:]
        if fid == "up":
            return
        f = next((x for x in self._files if x["id"] == fid), None)
        if f and f["mimeType"] == "application/vnd.google-apps.folder":
            self.dismiss(("drive", fid, self.path + f["name"] + "/"))
        else:
            self._read(fid)

    def _read(self, fid: str) -> None:
        try:
            name, mime, text = gauth.read_drive_text(self.svc, fid)
        except Exception as ex:
            self.dismiss(("error", str(ex)))
            return
        # show first 8000 chars
        self.dismiss(("text", name, text[:8000]))

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "close":
            self.dismiss(None)
        elif e.button.id == "up":
            self.dismiss(("up",))
        elif e.button.id == "read":
            lst = self.query_one("#drive-list")
            if lst.highlighted_child:
                cid = lst.highlighted_child.id or ""
                if cid.startswith("d-"):
                    fid = cid[2:]
                    if fid and fid != "up":
                        f = next((x for x in self._files if x["id"] == fid), None)
                        if f and f["mimeType"].startswith("application/vnd.google-apps.folder"):
                            self.dismiss(("drive", fid, self.path + f["name"] + "/"))
                        else:
                            self._read(fid)

    def on_key(self, e) -> None:
        if e.key == "escape":
            self.dismiss(None)


class DriveFileModal(ModalScreen):
    def __init__(self, name: str, text: str):
        super().__init__()
        self.name, self.text = name, text

    def compose(self) -> ComposeResult:
        with Container(id="df-box", classes="pane"):
            yield Label(f"FILE: {self.name}", classes="pane-title")
            yield RichLog(id="df-text", markup=False, wrap=True)
        yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.query_one("#df-text").write(self.text)

    def on_button_pressed(self, e):
        self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape":
            self.dismiss(None)


class SearchModal(ModalScreen):
    def compose(self) -> ComposeResult:
        with Container(id="search-modal", classes="pane"):
            yield Label("GOOGLE SEARCH", classes="pane-title")
            yield Input(placeholder="Search query, Enter to run", id="s-query")
            yield RichLog(id="s-results", markup=False, wrap=True)
        yield Button("Close", id="close")

    def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id != "s-query":
            return
        q = e.value.strip()
        if not q:
            return
        log = self.query_one("#s-results")
        log.write(f"Searching: {q} …")
        self.run_worker(self._search(q, log), exclusive=False)

    async def _search(self, q: str, log: RichLog) -> None:
        res = google_search(q)
        log.write(res)

    def on_button_pressed(self, e):
        self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape":
            self.dismiss(None)


def main():
    GoogleTUI().run()


if __name__ == "__main__":
    main()
