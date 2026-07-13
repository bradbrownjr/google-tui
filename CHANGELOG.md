# CHANGELOG.md — google-tui

Format: keep newest at top. One entry per meaningful change. Reference files
touched and any breaking notes.

## [2026-07-13]

### Added
- **Send confirmation via a 5-second cancelable countdown.**
  `ComposeModal` no longer fires `gauth.reply_to`/`forward` the instant
  Send is clicked. Clicking Send disables the To/Subject/body fields and
  the Send button and shows a "Sending in N…" countdown
  (`ComposeModal.SEND_COUNTDOWN_SECONDS = 5`); the actual send only
  happens once it reaches zero. Cancel or `Esc` at any point during the
  countdown aborts it and re-enables the form instead of sending.
  (`google_tui/main.py`)

### Fixed
- **Dead `on_dismiss` handler.** `ModalScreen.Dismissed` doesn't exist in
  the installed Textual version, so `GoogleTUI.on_dismiss` was never
  invoked — `ThreadModal`'s Reply/Reply All/Forward buttons, and the
  direct `r`/`a`/`f` keybindings, silently did nothing (no `ComposeModal`
  opened, no refresh after send). Replaced with explicit
  `push_screen(..., callback)` pairs (`_on_thread_modal_result` →
  `_open_compose_from_thread` → `_on_compose_result`), deferred one step
  via `call_after_refresh` per the push_screen-callback-timing note in
  AGENTS.md §2. Verified with a scripted `run_test` pilot (mocked
  `gauth`): Reply from `ThreadModal` now reliably opens `ComposeModal`,
  and a completed send now triggers `refresh_all`.

## [Unreleased]

### Added
- **Local cache + offline mode.** `google_tui/cache.py` (SQLite,
  `cache_items(category, key, payload, updated_at)`) persists thread
  summaries, events, tasks, calendar month/week data, and Drive listings/
  metadata/text. Startup is now cache-first: cached data populates the UI
  instantly, then a background thread reconnects to Google
  (`Header.sub_title`: `Connecting…` → `Synced HH:MM` or `Offline (cached
  HH:MM)`). `LoadingModal` only appears on a genuine first run with an
  empty cache. Reply/Reply All/Forward/toggle-task are disabled with a
  warning notify while offline; Drive preview falls back to cached
  metadata/text for files viewed at least once online. No offline mutation
  queue — this is read-only browsing of cached data, not a sync engine
  (tracked as a P2 follow-up in ROADMAP).
- **Settings tab** (`Ctrl+5`, `google_tui/settings.py` +
  `google_tui/cache.py`): encrypt-at-rest toggle for the local cache, off
  by default. Two key methods, both offered as a choice: a random local
  key file (`~/.config/google-tui/cache.key`, chmod 600, no prompt) or a
  passphrase typed at launch (scrypt-derived key, verified against a
  stored canary, never written to disk itself). Small "browse" cache rows
  (thread/event/task summaries) are bulk-decrypted on every list
  population; large "content" rows (Drive file text) are decrypted one at
  a time, only when actually opened — encryption overhead scales with what
  you look at, not with total cache size. Turning encryption on/off, or
  switching key method, clears the cache immediately and asks for a
  restart (no re-encryption/migration).
- `LoadingModal`, shown the instant the app mounts, before any Google API
  call — the initial fetch (mail + calendar + drive) reliably takes
  10-20+ seconds. Runs on a background worker THREAD (not just an asyncio
  worker) so the event loop stays free to actually paint the modal instead
  of freezing the terminal with nothing on screen.
- `Ctrl+Left/Right` to cycle tabs — a reliable fallback for `Ctrl+1..4`,
  which most terminals (and every major browser, for its own tab-switching)
  don't transmit at all.
- `AGENTS.md`, `ROADMAP.md`, `CHANGELOG.md` for cold-start continuation.
- `google-tui` launcher at `/home/bradb/.local/bin/google-tui` (on PATH)
  that bakes in venv activation — runs from any shell without sourcing `.venv`.
- **Tab/pane redesign.** Four full-width tabs (Mail / Calendar / Drive /
  Search) now live in the blue bar (`Ctrl+1..4`), styled as the outer
  `TabbedContent`'s own tab strip. The Mail tab holds the four panes (Email /
  Events / Tasks / Hermes, `Alt+1..4` or adjacency-based `Alt+arrows`) that
  used to be the whole app. Tab and pane numbers are shown dimmed at all
  times (not hidden-until-modifier-held — Textual has no key-release event
  to detect that; see AGENTS.md §2).
- **Calendar tab.** Full month grid with events listed inside each day's
  square (`+N more` opens `DayEventsModal` with the full list) and a new
  hour-grid week view (24 hour rows x 7 day columns), modeling Google
  Calendar's web UI. `[`/`]` page the month or week.
- **Drive tab.** Folder browser with a live preview pane: metadata (owner,
  type, path, created/modified — `gauth.get_file_metadata`) always shown,
  plus a text preview for non-binary/non-image files, updating as the cursor
  moves rather than requiring Enter.
- **Search tab**, inline instead of a modal.
- Two-row help bar: a contextual row (current tab/pane's shortcuts) above a
  static global-shortcuts row, both wrapping instead of truncating on a
  narrow terminal. `HelpModal` (`Ctrl+H`) has the full reference.
- `Ctrl+Q` quit (was bare `q`); `Ctrl+P` is Textual's own command palette.

### Fixed
- Tab bar collapsed from 2 rows to 1 (the second row was Textual's own
  `Tabs` underline indicator, made redundant once tabs got a real
  active-state background) and inactive tab labels are now fully legible
  (explicit `color: $text` instead of Textual's default 50%-dim, which read
  poorly against the blue bar).
- Widened the Email pane (`#left` 45% → 65%; right column panes had a lot of
  dead whitespace) and removed a doubled border on `#left` that was nesting
  Email's own pane border one level deeper than Events/Tasks/Hermes,
  visually offsetting it by a line.
- `gauth.read_drive_text` called `files().get(file_id=...)` — the Google
  Drive API v3 parameter is `fileId` (camelCase); the wrong name crashed
  every Drive text preview with "unexpected keyword argument". Only
  affected files (folders always worked, since they never called this path).
- `ListView.clear()` returns an unsynchronized `AwaitRemove` — now that mail
  and Drive data can be applied twice per session (once from cache, once
  from the live refresh), a fire-and-forget clear + deferred repopulate
  intermittently raised `DuplicateIds`. Fixed by properly `await`ing the
  clear inside an exclusive worker, plus a generation counter to drop any
  populate that got superseded mid-flight.

### Changed
- `_mk_id` helper moved to MODULE level (was a class method) so `DriveModal`
  can use it; fixed a latent bug where naming a method `_id` collided with
  Textual's internal `DOMNode._id` (caused `'NoneType' not callable` at
  `refresh_all`). (main.py)
- Mail-tab "Calendar" pane renamed to "Events" to stop clashing with the new
  Calendar tab; `PANE_ADJACENCY` (an explicit map) replaces the old
  `active ± 1/2` arithmetic, which assumed a 2x2 grid that no longer matches
  the layout (Email spans the full left column against a 3-row right column).
- `refresh_all` now clears each list before repopulating — `Ctrl+R` used to
  duplicate every row instead of replacing them.
- `gauth.month_events` refactored to share a new `events_between(svc, start,
  end)` helper instead of duplicating the `events.list` call shape.
- Every gauth-touching method on `GoogleTUI` split into a `_fetch_*` (pure
  data) / `_apply_*` (widget mutation) pair, so the initial load can run the
  fetches on a background thread while still safely applying results back on
  the main thread via `call_from_thread`.

### Removed
- `CalendarModal`, `DriveModal`, `DriveFileModal`, `SearchModal` — their
  content is now inline in the Calendar/Drive/Search tabs instead of behind
  a keypress-triggered modal.
- Bare `1`-`4`, `q`, `c`, `d`, `s` bindings — superseded by `Ctrl+1..4` /
  `Alt+1..4` (tabs vs. panes no longer share the same number keys) and
  `Ctrl+Q`; `c`/`d`/`s` opened modals that no longer exist.

## [2026-07-12] — Initial build

### Added
- Multi-pane Textual TUI at `/home/bradb/google-tui` (Python package
  `google_tui`).
- Google auth wrapper (`gauth.py`) using `~/.hermes/google_token.json`
  directly (the bundled `google-workspace` skill's CLI lacks Tasks + Drive
  list, so we hit the APIs directly).
- Email pane: threaded Gmail list (80 threads), lightbar, `Enter` thread
  view, `r`/`a`/`f` reply/reply-all/forward compose modal.
- Calendar pane: upcoming events (3 weeks), lightbar + detail dialog, full
  month + week view modal (`CalendarModal`).
- Tasks pane: all task lists combined, lightbar, `Space` toggle complete
  (live), `Enter` detail/subtasks view.
- Hermes Ask pane: general questions → Nous LLM (`tencent/hy3:free`) with live
  Google context; action questions → full Hermes agent (shells `hermes`).
- Drive button: folder browse, plaintext read (Docs→txt, Sheets→csv),
  binary download.
- Search button: text search via searxng (`hermes web search`).
- Pane switching: Alt+Left/Right/Up/Down, Tab/Shift+Tab, 1-4, Ctrl+R refresh.
- Verified against live Google data via Textual `run_test` pilot (email/
  calendar/tasks/drive/search/thread modals all open; task toggle + compose
  open confirmed via real key presses).

### Known limitations (see ROADMAP)
- No send confirmation (compose sends immediately).
- Email/events/drive capped (80 threads / 3 weeks / one folder page).
- Calendar week view is a simple day-column list, not a time-grid.
- Live email send not exercised end-to-end (would actually send mail).
