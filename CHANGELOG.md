# CHANGELOG.md — google-tui

Format: keep newest at top. One entry per meaningful change. Reference files
touched and any breaking notes.

## [2026-07-13]

### Added
- **Browser tab — Web/Gopher/Gemini/Search (P1 M2).** Replaces the
  standalone Search tab in place at `Ctrl+4` (`TAB_ORDER` and
  `BINDINGS`/`action_goto_tab_browser` updated accordingly; Settings stays
  at `Ctrl+5`), consuming M1's `render.py`/`DocumentView` directly — an
  address bar (`Input#browser-url`), a mode badge
  (`Static#browser-mode`: WEB/GOPHER/GEMINI/SEARCH — the only visual cue
  for Search mode, since bare query text has no scheme prefix to show it),
  and `DocumentView#browser-doc` for the rendered page. New
  `google_tui/fetchers.py` holds all the actual network I/O (`render.py`
  itself stays fetch-agnostic, per its M1 design): `fetch_http` (routes
  `text/html` through `render.parse_html`, other `text/*` through plain
  paragraph blocks, anything else raises `BrowserFetchError`); `fetch_gopher`
  (no existing client to port — raw `socket` I/O, `render.parse_gopher_url`
  to re-derive host/port/item-type/selector, `render.parse_gopher_menu` for
  type `1`, plain paragraphs for type `0`, a clear error for the `URL:`
  web-link selector convention and for any other item type); `fetch_gemini`
  (implemented from spec — TLS via `ssl.SSLContext(PROTOCOL_TLS_CLIENT)`
  with `CERT_NONE`/`check_hostname=False`, deliberately NOT
  `create_default_context()`, since self-signed certs are the Gemini norm;
  SHA-256 cert-fingerprint TOFU pinning via a new `GeminiTofuStore`
  wrapping `Cache`'s new `"gemini_cert"` category, keyed
  `f"{host}:{port}"`, checked *before* reading the response body; full
  1x/2x/3x/4x/5x/6x status dispatch — 1x raises `GeminiInputRequired`, 3x
  auto-follows same-host/scheme redirects up to 5 hops or raises
  `GeminiRedirectConfirm` otherwise, 6x is a "not supported yet" stub).
  Address-bar submission is classified by a new `_classify_address()`
  helper in `main.py` (omnibox heuristic: explicit
  `http(s)/gopher/gemini://` wins, a single dotted-word-with-no-space gets
  `https://` prepended, everything else — including any text containing a
  space — is a web search via the existing `ask.google_search`, with
  `search:` as an explicit escape hatch). Search results are rendered as a
  real linkified `Document` (`_search_result_document()`, regex-extracts
  every `https?://…` token from `hermes web search`'s opaque stdout into a
  numbered `Link`) rather than dumped into a `RichLog`, so numbered-link
  nav (matching bpq-apps' UX) works uniformly across all four Browser
  modes — this was the one real design call in M2, since the CLI's output
  format isn't structured/guaranteed. History is a session-lifetime-only
  in-memory `list[BrowserHistoryEntry]` (already-fetched `Document`s plus
  scroll position, not just URLs — Back/Forward never re-fetches, works
  fully offline); `Alt+Left/Right` are Back/Forward when the Browser tab is
  active (reusing `action_switch_left/right`, which already no-op outside
  Mail-tab-adjacency elsewhere), `Tab`/`Shift+Tab` toggle focus between the
  address bar and the page. Two new modals — `GeminiInputModal` (status
  10/11 prompts, masked input for "sensitive") and a reusable `ConfirmModal`
  (Gemini cross-host/cross-scheme redirect confirmation) — both funnel
  their result back through `_browser_navigate()`, deferred via
  `call_after_refresh` per the documented push_screen-callback-timing
  gotcha (AGENTS.md §2). The Browser tab is never gated by
  `self._require_online()` (that flag tracks Google reachability
  specifically, not arbitrary web/gopher/gemini fetches), and there's no
  SQLite cache category for page content itself — only the `gemini_cert`
  TOFU store persists; that's a deliberate v1 non-goal, tracked in
  ROADMAP. `HELP_TEXT`'s `SEARCH TAB` section and `_context_help_text()`'s
  `tab-search` branch became `BROWSER TAB`/`tab-browser`. Verified with
  headless Textual `run_test` pilots (mocked `fetchers.fetch_http`/
  `fetch_gemini` and `ask.google_search`, one scenario per process per
  AGENTS.md §6): Browser tab shows at `Ctrl+4`; address-bar submit renders
  a mocked `Document`; activating a numbered link navigates and grows
  history; `Alt+Left/Right` restore prior pages with zero additional
  fetches; bare-text input reaches `ask.google_search` and renders a
  linkified result Document; the Gemini status-10 input-required modal
  round-trip (push, submit, resume navigation) works end to end. Also unit-
  tested `fetchers.py`'s HTTP/Gopher/Gemini parsing/dispatch logic directly
  against synthetic fixture data (mocked `requests.get`/`socket.
  create_connection`/`ssl.SSLContext.wrap_socket`, zero real sockets) —
  covers content-type routing, gopher item-type dispatch and the `URL:`
  selector error, and the full Gemini status-code matrix including TOFU
  pin-then-mismatch and same-host-autofollow-vs-cross-host-confirm
  redirect branching. Found along the way: the `hermes web search`
  subcommand this feature (and the old Search tab) depends on no longer
  exists in the installed `hermes` CLI in this environment — tracked as a
  new ROADMAP P3 item rather than fixed here (out of scope for `ask.py`,
  which M2 deliberately left untouched); the Browser tab's Search mode
  degrades gracefully (an empty-link Document) rather than crashing when
  this happens. (`google_tui/fetchers.py` new; `google_tui/main.py`,
  `AGENTS.md`, `ROADMAP.md`)
- **`google_tui/render.py` — shared HTML/Gopher/Gemtext rendering module
  (P1 M1).** Protocol-agnostic `Document`/`Block`/`Link` model plus a
  Textual `DocumentView` widget, meant to be consumed by the future
  Browser (M2), News (M3), and rich-HTML-email (M4) epics instead of each
  rolling its own parser. Ports `bpq-apps/apps/htmlview.py`'s nav-vs-content
  link separation heuristic and `apps/gopher.py`'s tab-delimited menu
  parser (both packet-BBS apps whose `print()`/`input()` I/O and
  `__EXIT__`/`__MAIN__` sentinel-string control flow were left behind in
  favor of a real `LinkActivated` Textual message), and adds a from-spec
  Gemtext parser (no existing client to port). Fixes made during the port:
  entity decoding now keeps real Unicode instead of stripping to ASCII, a
  hardcoded-domain nav heuristic became a same-site `urlparse` check, and
  `<pre>`/`<code>` preformatted-block handling was added (didn't exist in
  the source). Not wired into `main.py`/any tab yet — that's M2/M3/M4's
  job. Design docs and audit only exist in the session that built this;
  the code and its docstrings are the reference going forward.
- **`SETUP.md` — Google Cloud Console walkthrough (P1 feature epic).**
  Step-by-step guide: create a project, enable Gmail/Calendar/Drive/Tasks/
  People/Routes APIs, configure the OAuth branding (Google rebranded the
  old "OAuth consent screen" into **Google Auth Platform** — Branding/
  Audience/Clients tabs, confirmed live via search since this UI moves),
  add yourself as a test user (flags the real caveat: Testing-mode tokens
  expire every 7 days unless the app is published/verified), create a
  Desktop-app OAuth client, and run the local auth flow. Recommends
  **People API** for the future Contacts tab and **Routes API** for the
  future Navigation tab — noting Routes is the maintained replacement for
  the now-deprecated Directions API, and that Maps Platform is the first
  API in this project requiring **Cloud Billing** (Workspace APIs are
  free). README updated to reflect labels/folders, multi-provider AI, the
  send countdown, and the onboarding wizard, and now links to `SETUP.md`.
- **Multi-provider AI + onboarding wizard (P1 feature epic).** The Ask pane
  is no longer locked into Hermes. `google_tui/ask.py` gets an `AIProvider`
  abstraction — `HermesProvider` (existing Nous LLM + `hermes` CLI agent),
  `ClaudeCodeProvider` (`claude -p --output-format text`), `OpenCodeProvider`
  (`opencode run`), `GeminiCLIProvider` (`gemini -p`) — all picked from a
  new "AI provider" radio group in the Settings tab and persisted to
  `settings.ai_provider`. Every provider gets the same Google context
  (recent email/events, built locally via `gauth`) handed to it as part of
  the prompt — that's how the Google token is "shared" with each provider,
  without needing separate Google integrations per CLI. Settings also
  gained a Nous API key field (`settings-nous-key`), so Hermes no longer
  requires hand-editing `~/.hermes/config.yaml`; the Settings tab container
  changed from `Container` to `VerticalScroll` since it no longer fits one
  screen. New `google_tui/setup_instructions.py` holds the shared
  Google-account and AI-provider setup text, reused by both the wizard and
  (later) `SETUP.md`. On launch, `GoogleTUI._diagnose_setup()` checks for a
  valid Google token and at least one reachable AI provider; if either is
  missing, an `OnboardingWizardModal` shows the relevant instructions
  before the normal tabs, with "Retry" (re-diagnose) and "Continue anyway"
  (proceed in the existing degraded/offline mode) options. Verified with
  mocked `run_test` pilots: wizard shows/hides correctly based on
  diagnosis, Continue anyway proceeds to normal startup, provider radio
  switch and Nous key save both persist to settings. (`google_tui/ask.py`,
  `google_tui/main.py`, `google_tui/settings.py`,
  `google_tui/setup_instructions.py`)
- **Labels as folders (P1 feature epic).** A `Select` dropdown
  above the Email pane (`#email-label-select`) lets you switch between
  Gmail labels/folders — "All Mail" plus every system and user label
  (nested user labels like `Family/Kids` shown indented by depth).
  `gauth.list_labels` (`users.labels.list`) and `gauth.list_threads(...,
  label_ids=...)` (`threads.list(labelIds=...)`) back it. Picking a label
  persists to `settings.default_label_id`, shows the cached threads for
  that label instantly (new label-scoped cache category
  `thread_summary:<label_id>`, replacing the old flat `thread_summary`
  category), and kicks a background refetch if online. Defaults to
  `INBOX` — previously the Email pane had no label filter at all (closer
  to All Mail than an inbox). Verified with a mocked `run_test` pilot:
  initial load shows Inbox-only threads, switching to a nested user label
  re-fetches and re-caches correctly. (`google_tui/gauth.py`,
  `google_tui/main.py`, `google_tui/settings.py`)
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
