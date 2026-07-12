# AGENTS.md — google-tui

Multi-pane terminal TUI for Brad's Google Workspace (Gmail, Calendar, Tasks,
Drive) plus a "Hermes Ask" pane. Built with [Textual](https://textual.textualize.io/).
Python 3.13, package layout under `/home/bradb/google-tui/google_tui/`.

This file is the single source of truth for a future session to continue work
WITHOUT prior chat context. Read it top-to-bottom before touching code.

---

## 1. What the app does

Layout (resize-reactive, Textual reflows automatically):

```
┌─ EMAIL (threads, full height) ──┐ ┌─ CALENDAR (upcoming) ───────┐
│ ▸ Frank Krizan                   │ │ ▸ 07/13 Tick/Flea Appt      │
│   Fwd: [DigiPi] …                │ │ ▸ 07/15 OHD Water Testing   │
├──────────────────────────────────┤ ├─ TASKS ─────────────────────┤
│                                   │ │ [ ] Buy cat food            │
│                                   │ │ [x] Pay electric bill       │
└──────────────────────────────────┘ ├─ HERMES ASK (compact) ──────┤
                                     │ > ask a question, Enter      │
                                     └──────────────────────────────┘
Buttons on the right column header: [=full view] [Drive] [Search]
```

Panes:
- **Email** (left, full height): threaded Gmail list, lightbar. `Enter` opens
  thread; `r`/`a`/`f` reply / reply-all / forward (compose modal). Unread
  threads prefixed with a bullet `•`.
- **Calendar** (right top): next ~3 weeks of events, lightbar → detail dialog.
  `c` opens full month + week calendar modal.
- **Tasks** (right middle): all Google Task lists combined, lightbar.
  `Space` toggles complete (live), `Enter` shows details/subtasks.
- **Hermes Ask** (right bottom, compact): type question, `Enter`. General
  questions answered by the Nous LLM (`tencent/hy3:free`) with live Google
  context injected; action-style questions delegate to the full Hermes agent.
- **Drive** button: browse folders, nested nav, read Google-native files as
  plaintext (Docs→txt, Sheets→csv), download binaries.
- **Search** button: text search via configured searxng backend.

## 2. Key bindings

| Key | Action |
|-----|--------|
| `Alt+Left/Right/Up/Down` | switch pane |
| `Tab` / `Shift+Tab` | cycle panes |
| `1` `2` `3` `4` | jump to Email / Calendar / Tasks / Hermes |
| `r` `a` `f` | reply / reply-all / forward (email pane) |
| `Space` | toggle task complete |
| `Enter` | open selected item's detail |
| `c` | full calendar view |
| `d` | Drive browser |
| `s` | Google search |
| `Ctrl+R` | refresh all panes |
| `q` / `Esc` | quit / close modal |

NOTE on Textual selection model: `ListView.Highlighted` (capital H) is the
cursor index setter; `ListView.highlighted_child` (read-only) is the selected
ListItem. A `ListView` only has a `highlighted_child` after the cursor has
moved via key/message (e.g. `pilot.press("down")`), not by setting the
attribute directly. This matters for tests — see §6.

## 3. File map

```
/home/bradb/google-tui/
├── pyproject.toml              # package metadata + console_scripts entry
├── README.md                  # user-facing keys/layout/setup
├── AGENTS.md                  # THIS file
├── ROADMAP.md
├── CHANGELOG.md
├── google_tui/
│   ├── __init__.py            # exports main, gauth, ask
│   ├── __main__.py            # `python -m google_tui` → GoogleTUI().run()
│   ├── gauth.py               # Google auth + Gmail/Cal/Tasks/Drive helpers
│   ├── ask.py                 # Hermes Ask (LLM) + search backends
│   └── main.py                # Textual app: panes, modals, CSS, bindings
└── .venv/                     # Python 3.13 venv (system-site-packages)
```

`gauth.py`:
- `services()` — returns cached `{gmail, calendar, tasks, drive}` via
  `Credentials.from_authorized_user_file(~/.hermes/google_token.json)` + builds
  the four `googleapiclient` resources. Refreshes a worker copy so the API
  client isn't shared across worker threads.
- `list_threads(svc, n)` — Gmail threads (Q=`-in:chat`, max `n`, formats
  `metadata` then `full` per thread for snippet/body/headers). Threads deduped
  by `threadId`; unread detected via `UNREAD` label.
- `list_events(svc, days)` — Calendar `events.list` over next `days` days.
- `list_tasks(svc)` — ALL task lists + items; returns `(lists, tasks)` where
  each task carries `_list` (list title) and `status` (`needsAction`/`completed`).
- `list_drive(svc, folder)` — Drive `files.list` in `folder` (or root `root`).
- `read_file(svc, fid)` — returns `(name, mime, text)`; Google-native files
  exported via `files.export` (Docs→text/plain, Sheets→text/csv, Slides→
  text/plain), others fetched as bytes (returns `bytes` for `text`).
- `reply_to(svc, thread_id, to, subject, body, original)`, `forward(...)`,
  `set_task_status(svc, task_id, list_id, status)` — MUTATING helpers.

`ask.py`:
- `ask_llm(question, ctx)` — POSTs to Nous inference endpoint
  (`https://inference-api.nousresearch.com/v1/chat/completions`,
  model `tencent/hy3:free`) using `NOUS_API_KEY` from `~/.hermes/config.yaml`.
  `ctx` is a prebuilt "Google snapshot" string.
- `needs_agent(q)` — keyword heuristic; if True, question is delegated to the
  full Hermes agent via `subprocess` shelling `hermes "<question>" --print`.
- `ask_hermes_agent(q)` — runs `hermes` and returns stdout.
- `google_search(q)` — shells `hermes web search "<q>" --print`, returns text.
- `build_ctx()` — pulls live threads/events/tasks into a compact text block
  for LLM context.

`main.py`:
- `GoogleTUI(App)` — main screen. Holds `self.svc`, `self._tasks_cache`,
  `self._events_cache` (populated by `refresh_all()` so modal detail views work
  offline of the network).
- `refresh_all()` — `async` worker; populates the four list widgets. Runs on
  mount (`on_mount` → `run_worker`) and on `Ctrl+R`.
- Modals (all subclass `ModalScreen`): `ThreadModal`, `CalendarModal`
  (month DataTable + `TabbedContent` week view), `EventDetailModal`,
  `TaskDetailModal`, `DriveModal`, `SearchModal`, `ComposeModal`.
- `_mk_id(prefix, raw)` — MODULE-LEVEL helper (NOT a method) that sanitizes a
  Google id into a valid Textual widget CSS id (`t-…`, `e-…`, `k-…`, `d-…`).
  MUST stay module-level: do not re-indent it into the class body, and do not
  name any method `_id` (collides with Textual's internal `DOMNode._id`).
- Module-level helpers: `_fmt_date(s)`, `_strip_html(s)`, `_plain_text(html)`,
  `_preferred_timezone()` (defaults to `America/New_York`).

## 4. Auth & secrets

- Token: `~/.hermes/google_token.json` (OAuth, has `refresh_token` + Gmail/
  Calendar/Drive/Tasks scopes). `google_client_secret.json` was NOT found —
  token is long-lived.
- Why a custom wrapper: the bundled `google-workspace` skill's
  `scripts/google_api.py` does NOT implement the `tasks` service and Drive has
  no `list` subcommand. So this project talks to the Google APIs directly via
  `google-api-python-client` using the already-valid token.
- Nous key: read from `~/.hermes/config.yaml` (`keys.nous_api_key`) by
  `ask.py`. If missing, `ask_llm` raises a clear error.
- The skill's `gws` CLI and `scripts/google_api.py` are NOT used by this app.

## 5. How to run

```bash
cd /home/bradb/google-tui
. .venv/bin/activate            # optional — launcher does this for you
google-tui                     # works from ANY shell (see §7)
```

`google-tui` launcher: `/home/bradb/.local/bin/google-tui` (on PATH),
shell script that `exec`s `/home/bradb/google-tui/.venv/bin/python -m google_tui`.
If the project folder moves, update the `VENV=` path in that launcher.

## 6. Testing without a TTY

Textual needs a real terminal, so headless tests use Textual's `run_test`
driver with a `pilot`. Pattern that works:

```python
async with app.run_test(size=(140, 44)) as pilot:
    await asyncio.sleep(2)                          # let workers populate
    app.action_goto_email()
    await pilot.pause()
    await pilot.press("down")                       # move cursor → highlighted_child set
    await pilot.pause()
    await pilot.press("r")                          # reply → ComposeModal opens
    await pilot.pause()
    assert isinstance(app.screen, ModalScreen)
```

Gotchas that cost time before:
- Mock `ask.ask_llm`, `ask.ask_hermes_agent`, `ask.google_search`, and
  `gauth.reply_to`/`forward`/`set_task_status` in tests to avoid network + real
  email sends.
- Do NOT assert on `ListView.highlighted`/`highlighted_child` after setting the
  attribute directly (read-only setters differ between versions). Drive selection
  through key presses instead.
- The Hermes Ask answer takes ~1s to stream into the RichLog; sleep 2s before
  asserting log line count.

## 7. Known caveats / open items

- NO send confirmation: `ComposeModal` send fires `gauth.reply_to`/`forward`
  immediately. Not tested against the live API (would actually send mail).
  Recommended next step: add a confirmation step before send (see ROADMAP).
- Threads: only first 80 shown (medata+full per thread). No threading UI
  beyond one level; no pagination beyond that.
- Calendar modal month grid: row keys auto-generated; `on_data_table_cell_
  highlighted` uses `e.coordinate.row/column` — do not call `update_cell` with
  integer indices (throws `CellDoesNotExist`).
- Week view is a simple TabbedContent of day columns (no time-grid Gantt).
- Drive: binary files are downloaded to a temp file and opened with `less`
  (shell-out) — fine on Linux; `less` must exist.
- Search uses `hermes web search` (shell-out) — requires `hermes` CLI on PATH.
- LLM model is hardcoded `tencent/hy3:free`; change in `ask.py` only.

## 8. Common tasks a future session might do

- Add a new pane: add id to `PANE_IDS`/`PANE_TITLES`, a widget in `compose()`,
  an `action_goto_<x>`, and bind a key in `BINDINGS`.
- Change the LLM: edit `MODEL` in `ask.py`.
- Add a Google action (e.g. create event): add a helper in `gauth.py`, a modal
  in `main.py`, and a button/handler.
- Fix a modal crash: check the traceback's `main.py` line; modals subclass
  `ModalScreen` and call `self.dismiss(...)`.
- Bump something: update CHANGELOG.md and ROADMAP.md when done.
