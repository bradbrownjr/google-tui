# AGENTS.md — google-tui

Multi-pane terminal TUI for Brad's Google Workspace (Gmail, Calendar, Tasks,
Drive) plus a "Hermes Ask" pane. Built with [Textual](https://textual.textualize.io/).
Python 3.13, package layout under `/home/bradb/google-tui/google_tui/`.

This file is the single source of truth for a future session to continue work
WITHOUT prior chat context. Read it top-to-bottom before touching code.

---

## 1. What the app does

Five full-width **tabs** live in the blue bar (this IS the styled `Tabs`
bar of the outer `TabbedContent#main-tabs`, not a separate status widget):
**Mail**, **Calendar**, **Drive**, **Browser**, **Settings**. The Mail tab
holds four **panes**: Email, Events, Tasks, Hermes. Tabs and panes are
deliberately different concepts with different key prefixes (`Ctrl+#` for
tabs, `Alt+#` for panes) — see §2.

```
┌[Mail¹]  Calendar²  Drive³  Browser⁴  Settings⁵──────────────────┐  ← blue bar,
├─ EMAIL (widened) ────────────┐ ┌─ EVENTS ─────────────────────┤    active tab
│ ▸ Frank Krizan                │ │ ▸ 07/13 Tick/Flea Appt       │    has an
│   Fwd: [DigiPi] …             │ │ ▸ 07/15 OHD Water Testing    │    accent-
│                                │ ├─ TASKS ──────────────────────┤    colored
│                                │ │ [ ] Buy cat food             │    background
│                                │ │ [x] Pay electric bill        │
│                                │ ├─ HERMES ASK ─────────────────┤
│                                │ │ > ask a question, Enter      │
└────────────────────────────────┘ └───────────────────────────────┘
  [help bar: contextual row above a static global-shortcuts row]
```

App startup is **cache-first**: whatever was cached from the last run (see
§1a) is applied to the UI instantly, then a background thread reconnects to
Google and refreshes it. `Header.sub_title` shows `Connecting…` →
`Synced HH:MM` or `Offline (cached HH:MM)`. `LoadingModal` only appears on a
genuine first run with an empty cache — the initial live fetch (mail +
calendar + drive) commonly takes ~20s (see the NOTE on `list_threads` below),
so on every run after the first, the app is interactive immediately instead
of blocking on that.

## 1a. Local cache, offline mode, encryption-at-rest

- **`google_tui/cache.py`** — `Cache`, a SQLite (`cache_items(category, key,
  payload, updated_at)`) key/value store, one row per cached object,
  optionally Fernet-encrypted per row. Categories: `thread_summary`,
  `thread_body` (unused so far — bodies aren't cached, only summaries),
  `event`, `task`, `tasklist`, `cal_month` (key `YYYY-MM`), `cal_week` (key
  = the Monday's ISO date), `drive_listing` (key = folder id, only `root`
  is ever fetched today), `drive_file_meta`, `drive_file_text` (both keyed
  by file id, populated lazily — only after a live Drive preview actually
  succeeds for that file, never pre-fetched for a whole folder).
  **Design intent**: small "browse" rows (summaries/listings) are cheap to
  bulk-decrypt on every list population; large "content" rows (Drive text)
  are decrypted one at a time, only when opened. This is what makes
  encryption not cost a "potato laptop" anything proportional to total
  cache size — see the module docstring.
- **`google_tui/settings.py`** — `Settings` dataclass persisted as
  **plaintext** JSON at `platformdirs.user_config_dir("google-tui")/
  settings.json` (`encrypt_at_rest`, `key_method`, `kdf_salt`, `canary`).
  Must stay plaintext: the app needs to know the key method before it can
  derive or verify any key.
- **Key methods** (`Settings.key_method`): `"keyfile"` — a random Fernet key
  at `.../cache.key`, chmod 0600, no prompt ever. `"passphrase"` — a key
  derived via scrypt from a passphrase typed at launch (`UnlockModal`,
  mode="unlock"); verified against a stored `canary` (a Fernet-encrypted
  known string) so a wrong passphrase is caught before it's trusted, not
  after decrypting garbage. The passphrase itself is NEVER written to disk;
  only `kdf_salt` and `canary` are.
- **Turning encryption on/off, or switching key method, always
  `Cache.clear_all()`s immediately** (no re-encryption/migration code) and
  tells the user to restart. This is a deliberate simplification — see
  ROADMAP.
- **Offline behavior is intentionally narrow**: `self._online: bool` is set
  by `_apply_live_refresh` after each connect attempt. Reply/Reply All/
  Forward/toggle-task check `self._require_online()` first and just
  `notify(..., severity="warning")` instead of attempting the call — there
  is NO queue-for-later/sync-when-reconnected mechanism. Drive preview reads
  from cache instead of `gauth` when offline. This is "browse cached data
  read-only while offline," not a sync engine.

Mail-tab panes:
- **Email** (left, full height): threaded Gmail list, lightbar. `Enter`/
  `Space` opens thread; `r`/`a`/`f` reply / reply-all / forward (compose
  modal). Unread threads prefixed with a bullet `•`.
- **Events** (right top, renamed from "Calendar" to avoid clashing with the
  Calendar tab): next ~3 weeks of events, lightbar, `Enter`/`Space` → detail.
- **Tasks** (right middle): all Google Task lists combined, lightbar.
  `Space` toggles complete (live), `Enter` shows details/subtasks.
- **Hermes Ask** (right bottom): type question, `Enter`. General questions
  answered by the Nous LLM (`tencent/hy3:free`) with live Google context
  injected; action-style questions delegate to the full Hermes agent.

Other tabs:
- **Calendar tab**: nested `TabbedContent#cal-tabs` (Month/Week), unrelated
  to the outer tab bar. Month = `DataTable#cal-grid` with multi-line rows
  (day number + up to 2 events + `+N more`); `Enter`/click on a day opens
  `DayEventsModal`. Week = `DataTable#cal-week-grid`, 24 hour rows x 7 day
  columns, an event's summary is written into every hour row it spans (a
  text-cell approximation of a Gantt block — DataTable is a grid of cells,
  not a pixel canvas). `[`/`]` page the month, or the week when the Week
  sub-tab is active.
- **Drive tab**: `ListView#drive-list` (left) + live preview pane (right).
  Preview updates on `ListView.Highlighted` (cursor movement), not just
  `Selected` — metadata (who/what/where/when) always shown; text preview
  only for `_is_previewable()` mime types. "Up" always reloads root, not the
  true parent folder (pre-existing simplification, not fixed by the tab
  redesign — see §7). Offline: reads `drive_file_meta`/`drive_file_text`
  from cache instead of `gauth`; shows "not available offline" for a file
  that was never viewed while online.
  - `gauth.get_file_metadata(svc, file_id)` — added for the preview's
    who/what/where/when: `fields="id,name,mimeType,size,owners,
    modifiedTime,createdTime,parents,webViewLink"`.
- **Browser tab** (`Ctrl+4`, P1 M2): address bar (`Input#browser-url`) + a
  mode badge (`Static#browser-mode`: WEB/GOPHER/GEMINI/SEARCH) + a
  `render.DocumentView` (`#browser-doc`) rendering whatever came back.
  Address-bar submission is classified by `_classify_address()` (omnibox
  heuristic: explicit `http(s)://`/`gopher://`/`gemini://` wins; a single
  dotted-word-with-no-space gets `https://` prepended; everything else,
  including any text containing a space, is a web search via the existing
  `ask.google_search` — same backend the old standalone Search tab used,
  now reached as a Browser mode instead of a separate tab). Fetching lives
  in `google_tui/fetchers.py` (`fetch_http`/`fetch_gopher`/`fetch_gemini`),
  never in `render.py` (which stays I/O-free) or `main.py` directly — every
  `fetch_*` is blocking and run via `self.run_worker(fn, thread=True,
  exclusive=True, group="browser-fetch")`, same fetch/apply split as the
  rest of the app. History is an in-memory `list[BrowserHistoryEntry]`
  (already-fetched `Document`s, not just URLs — Back/Forward never
  re-fetches) — session-lifetime only, no SQLite cache category for page
  content. `Alt+Left/Right` are back/forward (not `[`/`]`) when the Browser
  tab is active; `Tab`/`Shift+Tab` toggle focus between the address bar and
  the page. Gemini's TOFU cert pinning uses a new `Cache` category
  (`"gemini_cert"`, key `f"{host}:{port}"`) via `fetchers.GeminiTofuStore`;
  Gemini status 1x (input) and cross-host 3x (redirect) responses raise
  `fetchers.GeminiInputRequired`/`GeminiRedirectConfirm`, each handled by a
  small modal (`GeminiInputModal`/`ConfirmModal`) that resumes navigation
  through `_browser_navigate` on confirm. Never gated by
  `self._require_online()` — that flag tracks Google reachability
  specifically, unrelated to arbitrary web/gopher/gemini fetches.
- **Settings tab**: `Switch#settings-encrypt-switch` (encrypt-at-rest on/off)
  + `RadioSet#settings-key-method` (passphrase vs. keyfile, hidden via
  `.hidden` CSS class when encryption is off) + a "Clear local cache now"
  button + a `Static` showing the cache file's path/size. See §1a for the
  encryption model this drives.

## 2. Key bindings

| Key | Action |
|-----|--------|
| `Ctrl+1..5` | switch **tab** (Mail / Calendar / Drive / Browser / Settings) |
| `Ctrl+Left/Right` | cycle tabs — the reliable fallback for `Ctrl+1..5` (see caveat below) |
| `Alt+1..4` | jump to a Mail **pane** (Email / Events / Tasks / Hermes); switches to the Mail tab first if needed |
| `Alt+Left/Right/Up/Down` | move to the adjacent Mail pane (see `PANE_ADJACENCY` below) |
| `Tab` / `Shift+Tab` | cycle Mail panes (no-op outside the Mail tab) |
| `r` `a` `f` | reply / reply-all / forward (Email pane) — blocked with a warning notify while offline |
| `Space` | contextual (`action_context_space`): expand thread (Email), toggle complete (Tasks — blocked while offline), event detail (Events); no-op elsewhere |
| `Enter` | open selected item's detail (`ListView.Selected` / `DataTable.CellSelected`) |
| `[` `]` | previous / next month, or week if the Week sub-tab is active (Calendar tab only — no-op on other tabs) |
| `Ctrl+R` | reconnect / refresh all data (same code path as the background sync on startup) |
| `Ctrl+P` | command palette (Textual's own default binding, not declared in `BINDINGS`) |
| `Ctrl+H` | `HelpModal` — full reference, grouped by tab |
| `Ctrl+Q` / `Esc` | quit / close modal |

**Tab number display:** the confirmed design is "always show, dimmed" —
`_tab_label()` appends a `[dim]` superscript digit to each tab title, and
`_pane_title_row()` renders a two-`Label` row (title `width: 1fr`, number
`width: auto`, both styled) for Mail panes. This is NOT hide-until-modifier-
held: Textual 8.2.8's `events.py` has only one keyboard event class (`Key`,
press-only) — there is no key-release event and no exposed Kitty-protocol
modifier tracking, so "numbers appear only while Ctrl/Alt is held" cannot be
implemented in this Textual version. Don't attempt to "fix" this later
without re-checking whether Textual has since added key-release support.

**`Ctrl+1..4` terminal caveat:** most terminals (and browser-based terminals
especially — Chrome/Firefox/Edge reserve `Ctrl+1..8` for switching *browser*
tabs, intercepting the keystroke before it ever reaches the terminal) don't
reliably transmit `Ctrl+<digit>` at all; only terminals with `modifyOtherKeys`
or the Kitty keyboard protocol enabled do (confirmed via
`ANSI_SEQUENCES_KEYS` in this Textual version — the sequences exist and are
mapped, but most terminals never send them). `Ctrl+Left/Right` (`Ctrl+Arrow`)
is universally well-supported and is the reliable path — `Ctrl+1..4` is kept
for terminals that do support it, but don't assume it works everywhere, and
don't "fix" it by touching the bindings — there's nothing to fix in this
app's code; it's what the terminal transmits.

**`PANE_ADJACENCY`** (replaces an older `active ± 1` / `active ± 2`
arithmetic scheme that assumed a symmetric 2x2 grid): Email spans the full
left column; Events/Tasks/Hermes stack in the right column. This is an
explicit `{pane: {direction: pane}}` map, not arithmetic — see `main.py`
near `PANE_ADJACENCY`. If you add a 5th Mail pane, update this map, not a
formula.

NOTE on Textual selection model: `ListView.Highlighted` (capital H) is the
cursor index setter; `ListView.highlighted_child` (read-only) is the selected
ListItem. A `ListView` only has a `highlighted_child` after the cursor has
moved via key/message (e.g. `pilot.press("down")`), not by setting the
attribute directly. This matters for tests — see §6.

NOTE on `TabbedContent`: there are TWO `TabbedContent` widgets in the DOM
(`#main-tabs` outer, `#cal-tabs` nested inside the Calendar tab). A bare
`self.query_one(TabbedContent)` raises `TooManyMatches` — always query by ID
(`self._main_tabs()` helper, or `self.query_one("#cal-tabs", TabbedContent)`).
`on_tabbed_content_tab_activated` must check `event.tabbed_content.id` before
acting, since both post the same `TabbedContent.TabActivated` message.

NOTE on `TabPane`/`Tab` titles: pass a **markup string** (e.g.
`"Mail [dim]¹[/dim]"`), not a `rich.text.Text` object. Textual 8.2.8's
`Widget.render_str()` always routes through `Content.from_markup()` unless
the input is already a Textual `Content` instance — a Rich `Text` object hits
`Content.from_markup()` too and blows up (`AttributeError: 'Text' object has
no attribute 'translate'`) instead of being passed through.

NOTE on `App.query_one`/`App.query` and screens: they resolve against
`self.screen`, i.e. the CURRENTLY ACTIVE (top-of-stack) screen — not the base
app screen. Cost real debugging time once already: a worker callback tried to
`self.query_one("#email-list")` while `LoadingModal` was still on top of the
stack and got `NoMatches("... on Screen(id='_default')")` even though
`#email-list` obviously exists — it exists on the base screen, which wasn't
current. Fix: dismiss any modal FIRST, then query/populate widgets. Any
future modal shown during startup (or any worker that might run while a
modal is up) needs this same ordering.

NOTE on the startup/refresh worker (`_start_after_unlock` → `_load_from_cache`
→ `_live_refresh_thread` → `_apply_live_refresh`): Gmail/Calendar/Drive calls
are blocking synchronous httplib2 calls, not asyncio-native — an `async def`
worker with no real `await` inside doesn't yield control back to the loop,
so it can't paint anything (like `LoadingModal`) before it finishes. That's
why the live refresh specifically runs via `run_worker(fn, thread=True)` (a
real OS thread) rather than the plain `async def` pattern `refresh_all`
still uses for the post-task-toggle refresh. Textual widgets are NOT
thread-safe (`App.call_from_thread`'s own docstring says so) — every
gauth-touching method is split into a `_fetch_*` half (pure data, safe to
call from the worker thread — also safe to call `Cache` methods from there,
they're lock-guarded) and an `_apply_*` half (widget mutation, must run via
`self.call_from_thread(...)` back on the main thread). If you add a 6th data
source, follow this same fetch/apply split; don't call `gauth.*` and mutate
a widget in the same method if that method might ever run off the main
thread.

Also: `gauth.list_threads(svc, max_results=80)` does up to 160 sequential
Gmail API calls (metadata then full, per thread) and has been measured
taking **~20 seconds** in this environment — this is normal, not a hang.
Cache-first startup (§1a) means this only blocks the UI on a genuine first
run; every run after that shows cached data immediately while this happens
in the background. Don't "optimize" the call count itself without being
asked; it's tracked in ROADMAP's P2 pagination item.

NOTE on `push_screen(screen, callback)` timing: the callback fires **before**
the screen is actually popped (confirmed by reading `Screen.dismiss` in this
Textual version: it calls the result callback, THEN `self.app.pop_screen()`)
— NOT after, like you'd assume. A callback that does `self.query_one(...)`
immediately hits the same "wrong screen" `NoMatches` described above.
`_on_startup_unlock_result` and `_on_settings_passphrase_result` both defer
their actual work one step via `self.call_after_refresh(...)` for exactly
this reason. Do the same for any new modal-with-callback flow.

NOTE on `ListView.clear()`: it returns an `AwaitRemove` — removal is NOT
synchronous, and for a bulk removal (dozens of items) it can take LONGER
than a single `call_after_refresh` cycle to actually finish. This only bit
us once mail/drive data started being applied TWICE per session (cache load,
then live refresh, both with the same item IDs): a fire-and-forget
`clear()` + `call_after_refresh(populate)` pattern raised `DuplicateIds`
intermittently, because the second populate's items were inserted before
the first populate's identically-IDed items had actually been removed.
Fixed in `_apply_mail_data_async`/`_apply_drive_files_async` by `await`ing
`clear()` properly inside a `run_worker(..., exclusive=True, group=...)`
coroutine, plus a generation counter (`_mail_apply_gen`/`_drive_apply_gen`)
as a second safety net so a stale, superseded populate is a no-op instead of
racing. If you add another category that gets applied more than once per
session, use this same pattern — don't go back to bare `.clear()` +
`call_after_refresh`.

NOTE: `ModalScreen.Dismissed` does **not exist** in this Textual version
(`hasattr(ModalScreen, "Dismissed")` is `False`) — `on_dismiss(self, event:
ModalScreen.Dismissed)` in `GoogleTUI` type-checks fine only because
`from __future__ import annotations` makes it a string, never evaluated.
This means `on_dismiss` is very likely **dead code that Textual never
calls** in this version (there's no message class for it to dispatch on).
It was NOT touched this round (out of scope), but if `ThreadModal`'s
Reply/Reply All/Forward buttons ever seem to silently do nothing, this is
almost certainly why — the fix would be routing that result through
`push_screen(..., callback)` instead (mind the callback-timing NOTE above).

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
│   ├── render.py              # protocol-agnostic Document/Block/Link model + DocumentView (P1 M1)
│   ├── fetchers.py            # HTTP/Gopher/Gemini fetch for the Browser tab (P1 M2)
│   ├── setup_instructions.py  # shared Google-account/AI-provider onboarding text
│   ├── cache.py               # SQLite local cache, optional per-row Fernet encryption
│   ├── settings.py            # plaintext Settings dataclass (settings.json)
│   └── main.py                # Textual app: tabs, panes, modals, CSS, bindings
└── .venv/                     # Python 3.13 venv (system-site-packages)
```

`cache.py` / `settings.py`: see §1a for the full design (categories, key
methods, canary verification). `CACHE_DB_PATH` = `platformdirs.
user_cache_dir("google-tui")/cache.db`; `KEY_FILE_PATH` and `SETTINGS_PATH`
= `platformdirs.user_config_dir("google-tui")/{cache.key,settings.json}`.

`gauth.py`:
- `services()` — returns cached `{gmail, calendar, tasks, drive}` via
  `Credentials.from_authorized_user_file(~/.hermes/google_token.json)` + builds
  the four `googleapiclient` resources. Refreshes a worker copy so the API
  client isn't shared across worker threads.
- `list_threads(svc, max_results, q)` — Gmail threads, formats `metadata`
  then `full` per thread for snippet/body/headers. Unread via `UNREAD` label.
- `list_events(svc, days)` — Calendar `events.list` over next `days` days.
- `events_between(svc, start, end)` — generic date-range `events.list`;
  `month_events(svc, year, month)` and the Calendar tab's week grid both call
  this rather than duplicating the API-call shape.
- `list_tasklists(svc)` / `list_tasks(svc, list_id, show_completed)` — task
  lists and one list's items (caller tags each item with `_list`).
- `list_drive(svc, folder_id)` — Drive `files.list` in `folder_id` (or root).
- `get_file_metadata(svc, file_id)` — `files.get` with an expanded `fields`
  string (`owners`, `createdTime`, `modifiedTime`, `parents`, ...); backs the
  Drive tab's who/what/where/when preview panel.
- `read_drive_text(svc, file_id)` — returns `(name, mime, text)`; Google-native
  files exported via `files.export` (Docs→text/plain, Sheets→text/csv,
  Slides→text/plain), others fetched as bytes then decoded best-effort. Its
  `files().get(...)` call used the wrong keyword (`file_id=`) — the Google API
  discovery-generated method needs `fileId=` (camelCase). This is a real API
  parameter name, not a Python convention; grep for `file_id=` vs `fileId=`
  if a Drive call ever throws "unexpected keyword argument".
- `reply_to(...)`, `forward(...)`, `set_task_status(...)` — MUTATING helpers.

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
- `GoogleTUI(App)` — main screen. Holds `self.svc`, `self.settings`,
  `self._cache` (`Cache | None`, built once the encryption key is resolved),
  `self._online`, `self._tasks_cache`, `self._events_cache` (Mail-tab
  upcoming events), `self._cal_by_day` / `self._cal_week_cells`
  (Calendar-tab month/week grids), `self._drive_files` (Drive tab) — all
  populated so modal/preview reads don't need a fresh network round trip.
- `LoadingModal` — pushed only on a genuine first run (empty cache), by
  `_start_after_unlock`; dismissed by `_apply_live_refresh`.
- `UnlockModal` — passphrase entry, "unlock" (startup) and "create"
  (Settings tab) modes; see §1a.
- Every gauth-touching operation is split `_fetch_*` (pure data, thread-safe,
  also writes to `self._cache` when called from the live-refresh path) /
  `_apply_*` (widget mutation, main-thread only) — see the NOTEs on the
  startup/refresh worker above. `refresh_all()` (used after a task toggle)
  and `_live_refresh_thread` (startup + `Ctrl+R`) both call `_fetch_mail_data()`
  then `_write_mail_cache(...)` then `_apply_mail_data(...)`; same
  fetch/apply pattern for `_build_cal_month`/`_build_cal_week`/`_drive_load`.
  `_apply_mail_data`/`_apply_drive_files` can each now run TWICE per session
  (cache load, then live refresh) — see the `ListView.clear()` NOTE above for
  why they're `run_worker(..., exclusive=True)`-wrapped async methods with a
  generation counter, not plain synchronous clear+append.
- `_load_from_cache()` — reads every category via `Cache.get_all`/`get` and
  feeds the SAME `_apply_*` methods the live path uses; returns whether
  anything was found (decides whether `LoadingModal` is needed).
- Modals (all subclass `ModalScreen`): `LoadingModal`, `UnlockModal`,
  `ThreadModal`, `ComposeModal`, `EventModal`, `TaskModal`, `DayEventsModal`
  (Calendar day/hour-slot overflow), `HelpModal` (`Ctrl+H`). `CalendarModal`/
  `DriveModal`/`DriveFileModal`/`SearchModal` from the pre-tab-redesign
  version are GONE — their content is inline in the Calendar/Drive/Search
  `TabPane`s now; do not recreate them as modals.
- `_mk_id(prefix, raw)` — MODULE-LEVEL helper (NOT a method) that sanitizes a
  Google id into a valid Textual widget CSS id (`t-…`, `e-…`, `k-…`, `d-…`).
  MUST stay module-level: do not re-indent it into the class body, and do not
  name any method `_id` (collides with Textual's internal `DOMNode._id`).
- Module-level helpers: `_fmt_date(s)`, `_mk_id`, `_tab_label(text, num)`,
  `_event_day(e)`, `_is_previewable(mime)`.

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
    app.action_goto_pane_email()
    await pilot.pause()
    await pilot.press("down")                       # move cursor → highlighted_child set
    await pilot.pause()
    await pilot.press("r")                          # reply → ComposeModal opens
    await pilot.pause()
    assert isinstance(app.screen, ModalScreen)
```

Use `app.save_screenshot(path)` at any point inside `run_test` to export an
SVG snapshot of the current render — the closest substitute for eyeballing a
live TTY app when you can't attach one. `pip install cairosvg` (into the
project `.venv`) to convert those to PNG for visual review.

Gotchas that cost time before:
- Mock `ask.ask_llm`, `ask.ask_hermes_agent`, `ask.google_search`, and
  `gauth.reply_to`/`forward`/`set_task_status` in tests to avoid network + real
  email sends.
- Do NOT assert on `ListView.highlighted`/`highlighted_child` after setting the
  attribute directly (read-only setters differ between versions). Drive selection
  through key presses instead.
- There are two `TabbedContent`s in the DOM (`#main-tabs`, `#cal-tabs`) — use
  `app.query_one("#main-tabs")`, never a bare type query (see §2).
- The Hermes Ask answer takes ~1s to stream into the RichLog; sleep 2s before
  asserting log line count.
- Run each `GoogleTUI()` test scenario in its OWN process (`python
  scenario_x.py`, not multiple `async with app.run_test()` blocks chained in
  one `asyncio.run(...)`). Chaining full app instances in one process left a
  background `thread=True` worker from a prior instance still in flight when
  the next instance mounted, and it reproducibly caused a `DuplicateIds`
  crash unrelated to the actual scenario being tested. Wipe
  `platformdirs.user_cache_dir("google-tui")` / `user_config_dir("google-tui")`
  between scenarios that need a clean cache (`shutil.rmtree(..., ignore_errors=True)`).
- To prime a cache for a "warm start" test, actually run a cold-start
  scenario first (real live data) rather than hand-crafting cache rows —
  the payload shapes (Gmail thread dict, Calendar event dict, etc.) are
  exactly what `gauth.*` returns, easy to get subtly wrong by hand.

## 7. Known caveats / open items

- NO send confirmation: `ComposeModal` send fires `gauth.reply_to`/`forward`
  immediately. Not tested against the live API (would actually send mail).
  Recommended next step: add a confirmation step before send (see ROADMAP).
- Threads: only first 80 shown (metadata+full per thread). No threading UI
  beyond one level; no pagination beyond that.
- Calendar month grid (`#cal-grid`): `on_data_table_cell_selected` reads the
  day number off `event.value` (the first line of the multi-line cell text),
  not `event.coordinate` + a separate `get_cell_at` lookup — simpler and
  avoids the `update_cell`-with-integer-indices `CellDoesNotExist` trap the
  old modal-era code had to work around.
- Week view (`#cal-week-grid`) is **hour granularity**, not 30/15-minute — an
  event's summary is written into every hour row it spans, so sub-hour timing
  isn't visually precise. Documented follow-up in ROADMAP, not fixed here.
- Drive "up" always reloads root, not the true parent folder — a
  simplification carried over unchanged from the pre-tab-redesign DriveModal
  (fixing real parent-stack tracking is a separate, unrequested task).
- Drive preview is text-only for `_is_previewable()` mime types; images and
  other binaries show metadata + "no text preview" (no download-to-`less` or
  in-terminal image rendering — that would need `textual-image`, not
  currently a dependency).
- The Browser tab's Search mode uses `hermes web search` (shell-out) —
  requires `hermes` CLI on PATH. **Verified during M2 that this subcommand
  does not exist in the `hermes` CLI installed in this environment**
  (`hermes web search "..."` prints argparse's top-level usage/"invalid
  choice: 'web'" instead of running a search — the CLI's command set has
  moved on since `ask.google_search` was written). `ask.google_search`
  itself was left unchanged (pre-existing, out of scope for M2 — the
  Browser tab's `_search_result_document()` just linkifies whatever comes
  back, gracefully degrading to a document with no links if the shell-out
  fails). Worth fixing in a follow-up: find the current equivalent
  `hermes` subcommand (or API) for a web search.
- LLM model is hardcoded `tencent/hy3:free`; change in `ask.py` only.
- Tab numbers are always-shown-dimmed, not hide-until-modifier-held — see the
  NOTE in §2 for why (no key-release event in Textual 8.2.8).
- Offline mode is READ-ONLY browsing of cached data, not a sync engine: no
  queued mutations, no automatic retry beyond `Ctrl+R`/next launch. See §1a.
- Changing the encrypt-at-rest switch or key method takes effect on the
  NEXT launch, not live — the running session keeps using whatever `Cache`
  object it already built. The cache is cleared immediately so stale rows
  under the old scheme can't linger, but a "restart to apply" notify is the
  only feedback; there's no in-session cache-object hot-swap. A genuine
  live-swap (rebuild `self._cache` with the new key without restarting)
  is a reasonable follow-up if the restart step proves annoying in practice.
- `on_dismiss(self, event: ModalScreen.Dismissed)` is almost certainly dead
  code in this Textual version — see the NOTE in §2. Not fixed this round
  (out of scope), but worth knowing before assuming Reply/Forward-from-
  ThreadModal works.

## 8. Common tasks a future session might do

- Add a new Mail pane: add id to `PANE_IDS`/`PANE_TITLES`, a neighbor entry
  in `PANE_ADJACENCY`, a widget in `compose()`, an `action_goto_pane_<x>`,
  and bind `alt+N` in `BINDINGS`.
- Add a new top-level tab: add a `TabPane` in `compose()` under
  `TabbedContent#main-tabs`, an `action_goto_tab_<x>`, bind `ctrl+N`, and add
  a branch to `on_tabbed_content_tab_activated` and `_context_help_text`.
- Change the LLM: edit `MODEL` in `ask.py`.
- Add a Google action (e.g. create event): add a helper in `gauth.py` and a
  modal/handler in `main.py`.
- Fix a modal crash: check the traceback's `main.py` line; modals subclass
  `ModalScreen` and call `self.dismiss(...)`.
- Cache a new data source: add a category name (see §1a), a `_fetch_*` /
  `_apply_*` pair, write-through in `_live_refresh_thread` (`self._cache.put`
  or `.put_many`), and a read in `_load_from_cache`. If the category is
  "content-sized" (could be large, like Drive file text) rather than a
  small summary, cache it lazily on first successful view, not eagerly for
  a whole listing — see the `drive_file_text` pattern in `_drive_preview`.
- Bump something: update CHANGELOG.md and ROADMAP.md when done.
