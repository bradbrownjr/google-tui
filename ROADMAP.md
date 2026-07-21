# ROADMAP.md — google-tui

Prioritized future work. Each item notes status and the file(s) it touches.
Update this file as items are completed — move the completed item's entry
into CHANGELOG.md under a new dated section (`## [YYYY-MM-DD]`) instead of
just checking it off here, so ROADMAP.md only ever shows what's still open.

## P1 — Bugs (from 2026-07-17 live-usage testing)

- [ ] **BLOCKED — needs a live repro before touching code again.**
  `Ctrl+R` reportedly crashed the app with no visible error. Every
  unhandled exception is supposed to be caught by `GoogleTUI._handle_exception`
  and logged to `platformdirs.user_log_dir("google-tui")/google-tui.log`
  (`main.py:1441-1454`) before the app exits — checked that file, and there's
  no crash logged anywhere near when this was reported (2026-07-17, after
  04:22). Either it wasn't actually `Ctrl+R`, or the failure happened in a
  path that bypasses `_handle_exception` entirely (e.g. a raw crash before/
  after Textual's event loop is pumping — see the comment at `main.py:7074`
  about exactly that gap). Already investigated once (`[2026-07-18]`) with
  nothing more to find without a live repro — don't re-research this from
  scratch; wait for `tail -f ~/.local/state/google-tui/log/google-tui.log`
  running live when it happens again, then come back with whatever that
  catches (or confirmation the log stayed empty through the crash, which
  would point at the pre-event-loop gap instead).
- [ ] **BLOCKED — needs a targeted live repro before touching code again.**
  Reply → archive → reply sequence made a thread disappear from Inbox even
  though Roger's follow-up reply should have put it back (Gmail
  auto-restores the INBOX label on any thread that gets a new message).
  Likely interacts with `_run_mutation`'s optimistic cache handling for
  trash/archive (`main.py:5967-6039`) and the transient network errors
  (see the label-refresh-retry fix, `[2026-07-18]`) hitting mid-refresh — a
  refresh that fails partway through could leave `self._threads_cache` in a
  stale post-archive state that a later successful refresh doesn't correct
  if the correction logic assumes the cache was left consistent. Already
  investigated once (`[2026-07-18]`) with nothing more to find without a
  repro; don't re-research this from scratch — reproduce it deliberately
  (archive a thread, have the other party reply, then `Ctrl+R`) with log
  capture running, then come back with what actually happened to
  `_threads_cache` across that sequence.
- [ ] **BLOCKED — needs a real raw-MIME sample before touching code again.**
  Forwarded email from Roger didn't show the inline quoted original.
  `gauth._extract_body`/`_extract_html_body` (`gauth.py:334-368`) recurse
  through ordinary `multipart/alternative`/`multipart/mixed` nesting fine,
  but neither one has any special case for a `message/rfc822` MIME part —
  the way some mail clients (not Gmail's own web "Forward," which inlines
  the original as quoted plain text/HTML in the normal body) attach a
  forwarded original as a genuine nested message. That part's content would
  fall through both extractors' loops and get silently dropped. Already
  investigated once (`[2026-07-18]`) with nothing more to find without a
  sample; don't re-research this from scratch — export the raw message via
  `messages().get(format="raw")` to see its actual MIME tree, then come
  back with that before writing a fix.

## P2 — Email client completeness

These are table-stakes mail features the client is currently missing; several
build directly on infrastructure that already exists.

- [ ] **Attachments — view/download received + attach on compose.** No
  attachment support exists anywhere (`grep attachment` → 0 hits). Received
  messages don't surface their attachment parts, and `ComposeModal` can't add
  a file. Needs `gauth.py` to walk MIME parts for `filename`/`attachmentId`
  and fetch bodies via `messages().attachments().get`, a way to list/open them
  from `ThreadModal` (download reusing the Drive tab's local-save path —
  `EXPORT_DIR`), and a file-picker + multipart-build path on send. Biggest
  value of this batch. *(Suggested model: Opus — MIME assembly on both the
  read and send sides, plus new UI.)*
- [ ] **Snooze a thread from the list.** The star + mark-unread pieces of the
  former "Star / mark-unread / snooze" bullet shipped (`[2026-07-21]` and
  earlier); snooze is what's left. It's the larger piece: Gmail's API has no
  native snooze, so it needs a small "remind at" modal plus either a
  `SNOOZED`-style user label the app hides-until-due or a scheduled re-surface
  hooked into `_periodic_refresh`. No new backend for the label-move part
  (`modify_labels`), but the "resurface at time T" scheduling is genuinely new.
- [ ] **Multi-select bulk actions.** No way to select N threads and
  archive/label/trash them at once — every action is single-thread. Needs a
  selection model on the Email `ListView` (Space-to-check, visual marker) and
  bulk variants of the mutation actions.
## P3 — Calendar, Contacts, and cross-cutting UX

- [ ] **RSVP to received invitations** (accept / decline / tentative).
  Distinct from *sending* invites (explicitly out of scope per the `gauth.py`
  note). No `responseStatus` handling exists — reading the self-attendee's
  status and PATCHing it via `events().patch` would let you respond to invites
  from `EventModal`. Mid effort.
- [ ] **Contacts create / edit / delete.** `ContactModal` (`main.py:8325`) is
  read-only (Name/Email/Phone + "Compose Email"). Add People API
  create/update/delete (`people().createContact`/`updateContact`/
  `deleteContact`) behind an editable modal + list actions so the tab can
  actually maintain the address book. *(Suggested model: Opus — People API
  write paths + edit UI + offline-queue parity with the mail/task mutations.)*
- [ ] **Desktop notifications** for new mail and upcoming events. Nothing
  emits OS notifications today (`grep notify-send/plyer` → 0). Hook the
  existing `_periodic_refresh` loop to fire `notify-send` (Linux) /
  `terminal-notifier` (macOS) on newly-arrived unread threads and imminent
  events, so the app is useful left running in the background. Gate behind a
  Settings toggle; degrade silently where no notifier binary exists.
- [ ] **Offline full-text mail search.** Thread bodies are already cached
  (`cache.py`, `thread_body` category), but search only filters the
  already-loaded list. A SQLite FTS5 index over the cached corpus would give
  real offline search across everything ever opened, not just the current
  page. Touches `cache.py` (FTS table + populate on cache write) and the Email
  search path.
- [ ] **Themes** (dark / light / custom palettes). Only `ascii_mode` exists —
  no color theming. Textual 8.x has native theme support
  (`App.register_theme`, command-palette theme picker); wire a curated set +
  a Settings → General selector persisted to `Settings`. Mostly wiring.

## P4 — Nice-to-have

- [ ] **Week view sub-hour granularity** (30/15-minute rows) — the current
  week grid (`#cal-week-grid`) is hour-granularity; an event's summary fills
  every hour row it spans, so start/end times aren't visually precise within
  the hour.
- [ ] **Drive image preview** — currently images show metadata only ("(image
  file — no text preview)" since `[2026-07-17]`, was a generic binary/image
  message before); in-terminal rendering would need the `textual-image`
  package (not a current dependency).
- [ ] **Multiple accounts** switch (if a second token appears).
- [ ] **Keyboard-first everywhere** — audit remaining tabs/modals for
  mouse-only actions. Drive folder nav (arrow keys + Enter) and Calendar
  month/week nav (`[`/`]`) — the two examples this item used to name — are
  already fully keyboard-accessible; confirmed `[2026-07-16]` via a
  keyboard-only pilot, no code change needed. What's left, if anything, is
  unaudited — re-scope once something concrete turns up.
- [ ] **News card per-category selection.** The Dashboard NEWS card pulls
  from ALL subscribed feeds newest-first, not "top-5 by RSS category" —
  a possible refinement if the flat list proves too noisy in practice.
- [ ] **Usenet support.** Needs a curated list of popular public Usenet
  servers plus support for an arbitrary/unlisted server URL, with credentials
  and API support. Should ship with a small curated server list rather than
  an empty form. *(Suggested model: Opus — new protocol client (NNTP),
  credential storage, and Settings UI.)*

## Done

- [x] **Config file (`config.toml`)** (`[2026-07-19]`) — new, optional,
  hand-edited `google_tui/app_config.py` (`AppConfig`/`load_config`), read
  once at startup with stdlib `tomllib` (no new dependency — the app never
  writes to this file, unlike `settings.json`). Covers the five ROADMAP
  fields: `llm_model` (overrides the Hermes/Nous provider's default model,
  threaded through `ask.py`'s `ask_llm`/`HermesProvider`/`get_provider`),
  `timezone` (an IANA name, resolved via `zoneinfo.ZoneInfo`, overriding
  OS-local for the Dashboard TODAY card's date filter and the New-Event
  modal's wall-clock-to-`dateTime` conversion), `pane_order`, and
  `refresh_interval_minutes` (genuinely new — no periodic auto-refresh loop
  existed before; `on_mount` schedules `_periodic_refresh`, which skips
  while offline rather than spamming per-section error toasts on a timer),
  and `searxng_url` (a fallback default only — `Settings.searxng_url` /
  Settings -> Search already fully covers this, config.toml just seeds it
  when the user hasn't set one there). `pane_order` deliberately only
  reorders the Dashboard's Tab/Shift+Tab cycle (and Alt-digit-jump/"first
  enabled pane" fallback) — the visual 2-column grid position and
  `DASH_ADJACENCY`'s Alt-arrow spatial navigation are hand-tuned to pair
  semantically related cards and stay exactly as authored regardless of
  config; see `config.toml.example` at the repo root for the documented
  format. A missing file, TOML syntax error, or any single bad field value
  (bad timezone name, non-positive interval, non-list pane_order) all fall
  back to defaults with a logged warning rather than blocking startup. New
  `tests/unit/test_app_config.py` and pilot scenario
  `tests/pilot/config_toml_overrides.py`.
- [x] **RSS subscription list** (`[2026-07-19]`) — Settings → News Feeds
  gained a "Browse popular feeds…" button opening `FeedPickerModal`, a
  filterable checklist (clone of the existing `LabelPickerModal` pattern)
  over a new hand-curated, hand-verified table (`popular_feeds.py`,
  `POPULAR_FEEDS`) spanning General News, World News, Local News (US —
  necessarily national outlets, since no single feed is "local" for every
  user), Tech News, Cybersecurity, Amateur Radio, Electronics, and Sports.
  Unlike `LabelPickerModal` (assign-only), this picker is a genuine two-way
  toggle: checking subscribes, unchecking unsubscribes, diffed against
  `Settings.feed_urls` on Apply (`_on_feed_pick_result`) — manually-added
  feeds outside the curated table are never touched by it. `_add_feed_url`/
  `_remove_selected_feed` refactored into shared `_subscribe_feed`/
  `_unsubscribe_feed` helpers so the picker and the existing manual-URL
  Input+Button both go through one code path (single place that saves
  Settings, refreshes the Settings-tab list, and kicks the background merge
  fetch / cache purge). New pilot scenario
  `tests/pilot/popular_feeds_picker.py`.
- [x] **Dashboard tab: the external cards** (`[2026-07-19]`) — the four cards
  left over from the Google-native Dashboard grid (`[2026-07-17]`): WEATHER
  (Open-Meteo geocoding + forecast), STOCKS (Stooq CSV quotes), WORD OF THE
  DAY (Merriam-Webster's public RSS feed), PICTURE OF THE DAY (Wikimedia's
  Feed API — caption/description as text with a link to the image, since
  in-terminal image rendering isn't built yet; see the still-open Drive image
  preview item above). All four are free/keyless. Grid grew from 2×2+Hermes
  to 2×4+Hermes (`DASH_PANE_IDS`/`PANE_TITLES`/`DASH_ADJACENCY`,
  `#dashboard-body`'s `grid-size`); WEATHER/STOCKS/WORD/POTD all start
  disabled in `Settings.dashboard_panes_enabled`'s default (opt in via
  Settings → Dashboard) so existing installs don't suddenly grow four cards
  they haven't configured. Settings → Dashboard gained a weather-location
  and stock-symbols row ("Save card settings" triggers an immediate
  refresh). `_live_refresh_thread` fetches all four independently of Google
  auth (none of them touch Google) and independently of each other (one
  failing doesn't blank the others) — a `_DASH_EXTRA_UNCHANGED` sentinel
  distinguishes "this refresh had nothing new" (leave the card as painted)
  from an explicit `None` ("nothing cached/configured, show the empty
  state"), the latter also used when a card is newly enabled from Settings
  so it repaints immediately instead of staying blank until the next
  refresh. WORD OF THE DAY/PICTURE OF THE DAY's Enter action opens the
  source link in the Browser tab (`_open_dashboard_link`, reusing the
  `_bookmark_open_selected` two-step). Found and fixed a latent bug in
  passing: the pre-existing dash-mail/dash-news Space-mirrors-Enter path
  built a `ListView.Selected` missing its required `index` arg, a `TypeError`
  waiting to happen (Textual 8.2.8 has no default for it). New pilot
  scenario `tests/pilot/dashboard_external_cards.py` (fetchers mocked via
  `tests/pilot/fakes.py`, extended); 79 tests total, all green. See
  CHANGELOG.
- [x] **Unit tests in-repo** (`[2026-07-19]`) — new `tests/` package,
  `pytest`-discoverable (`pip install -e ".[dev]"`, then `pytest`, 59 tests).
  `tests/unit/` covers pure functions (render's HTML/Gopher/Gemtext parsers,
  drive_sources' URL/listing parsing, gauth's Gmail MIME-payload extraction,
  remote_creds/cache round trips) with plain in-process tests. `tests/pilot/`
  formalizes the `run_test` pilot pattern from AGENTS.md §6 into five
  standalone scenario scripts (startup smoke test, email reply-modal flow,
  Google Drive regression, FTP source-switch + cache namespacing, Browser
  sftp:// redirect) — each still runs in its own subprocess (a real
  DuplicateIds crash previously came from chaining multiple `GoogleTUI()`
  instances in one process), with a thin `tests/test_pilot_scenarios.py`
  subprocess-wrapper making them all reachable from one `pytest` invocation.
  See CHANGELOG.
- [x] **Unified Drive-tab sources: Google Drive + FTP + SSH (SFTP/SCP)**
  (`[2026-07-18]`) — the Drive tab is source-agnostic now, via a new
  `Select#drive-source-select` picker ("Google Drive" + any saved FTP/SSH
  hosts + "+ Add remote host…"). New `drive_sources.py` `DriveBackend`
  abstraction (`GoogleDriveSource`/`FtpSource`/`SshSource`) normalizes every
  source to the shape the existing file list/preview/download UI already
  expected — closes the SFTP/SCP item below AND the still-open
  "download an FTP file" item, since download is source-agnostic too.
  `SshSource` tries the SFTP subsystem first (real mtime/size, true
  partial-read preview); only if a server refuses the subsystem outright
  does it fall back, for that connection's lifetime, to an exec-channel
  mode (`find -printf`, falling back further to `ls -la`, for listing;
  `head -c` for preview; the new `scp` package's `SCPClient` for download —
  best-effort against varied remote shells, not exhaustively tested). New
  deps: `paramiko`, `scp`. `ftp_creds.py` generalized into `remote_creds.py`
  (adds protocol/port, composite `source_key`, backward-compatible with
  existing saved FTP logins). Browser's `ftp://` handling is fully
  reassigned: an `ftp://`/`sftp://` address (typed or bookmarked) now
  switches to the Drive tab and connects there (`RemoteHostModal`,
  generalized from `FtpLoginModal`) instead of fetching inline — fixes a
  latent bug too, where `sftp://` previously fell through `_classify_address`
  into a literal web search for the whole URL string. Also fixed two
  correctness gaps the old Google-only code got away with: `_drive_items_by_
  cid` replaces a lossy `cid[2:]`-reversal id lookup (broke for FTP/SSH's
  path-shaped ids), and Drive's preview cache is now namespaced by source
  (two different hosts can share a path like `/readme.txt`). See CHANGELOG.
- [x] **P2 batch: link underline, Drive preview split, Drive download**
  (`[2026-07-17]`) — dropped the underline from `render._LINK_STYLE`
  (now just `bright_cyan`); split the Drive preview's binary/image message
  by mimetype ("(image file — no text preview)" vs. "(binary file — no text
  preview)"); added Drive → local-filesystem download (`gauth.
  download_drive_file`, new `d` keybinding, writes to `EXPORT_DIR` —
  `main.py`'s former `NAV_EXPORT_DIR`, renamed since it's now shared with
  Navigation's itinerary export — no destination prompt). See CHANGELOG.
- [x] **Plain FTP browsing** (`[2026-07-18]`) — new `fetchers.fetch_ftp`
  (`ftplib`, stdlib) browses `ftp://` directories (MLSD, falling back to
  parsing classic Unix `LIST` output) and previews files, wired into the
  Browser tab's address bar/link-follow like HTTP/Gopher/Gemini. Anonymous
  by default; an auth failure pops a login prompt (`FtpLoginModal`), with
  credentials optionally saved (Fernet-encrypted with the same key the local
  cache's encrypt-at-rest uses, in their own file — not cache rows — so
  "Clear Cache" can't wipe them; new `ftp_creds.py`). Settings → General
  gained a "Saved FTP hosts" view/remove list. SFTP/SCP deliberately
  deferred. **Superseded `[2026-07-18]`** by the unified Drive-tab sources
  entry above — FTP browsing moved from Browser into Drive, `fetch_ftp`/
  `FtpLoginModal`/`ftp_creds.py` all replaced by `drive_sources.py`/
  `RemoteHostModal`/`remote_creds.py`. See CHANGELOG.
- [x] **Browser bookmarks: real list view with folders, `H`/`B`/`Ctrl+B`,
  start-page setting** (`[2026-07-18]`) — the flat 4-`Button` starter row is
  now a `ListView` backed by persisted, user-editable `Settings.
  browser_bookmarks` (folders supported), color/icon-coded by protocol.
  `Alt+H` → plain `H`; new `B` (re-show bookmarks any time, not just before
  first navigation) and `Ctrl+B` (bookmark the current page) bindings; new
  `Settings.browser_start_page` (Settings → General) picks bookmarks-vs-home
  as the Browser tab's first-activation view each session. Also resolved,
  won't-do: pulling synced Chrome/Android bookmarks — no public API exists
  for a third-party standalone app to read them. See CHANGELOG.
- [x] **Size the Month grid's day squares to better fill the terminal**
  (`[2026-07-18]`) — `#cal-grid`'s 7 day columns now get explicit widths that
  split the widget's actual size evenly (instead of narrow auto-sized
  columns), and row height grows with available vertical space (min 4, max
  8 lines), with `_day_cell_text` showing more events per day (`max_events`)
  and wider text (`line_width`) when the extra room allows. Small terminals
  keep the old minimum sizing unchanged. See CHANGELOG.

- [x] **Background-color events by their source calendar** (`[2026-07-18]`) —
  new `gauth.list_calendars` (`calendarList().list()`) feeds a new `calendars`
  kwarg on `events_between`/`month_events`, which now fetch every SELECTED
  calendar (not just `"primary"`), tagging each event with `_calendarId` and
  `_color` (the event's own `colorId` override, else its calendar's
  `backgroundColor`). `_day_cell_text` (Month) and a new `_bg_cell` helper
  (Week, single-event hour/all-day cells only — a multi-event "N events" cell
  has no one color to use) background-color each event line/cell via
  `Text.stylize`. `_live_refresh_thread` fetches the calendar list once per
  refresh and shares it between the month/week fetches. See CHANGELOG.
- [x] **Better multi-day event display** (`[2026-07-18]`) — Week view's
  `#cal-week-grid` now has a dedicated "All day" row above the hour grid.
  `_apply_cal_week` routes all-day events (date-only start/end) and
  multi-day *timed* events (start/end `dateTime`s on different calendar
  dates, e.g. an overnight session) into that row instead of the hour cells
  — spanning every day column they cover within the displayed week. Ordinary
  same-day timed events are unaffected. `_cal_week_cell_selected` and
  `_cal_week_matches` (the "/" find-next) both account for the new row-0
  offset (hour *h* is now grid row *h+1*). See CHANGELOG.
- [x] **Highlight today's date** on the Month grid (`[2026-07-18]`) —
  `_day_cell_text`/`_apply_cal_month` now bold-reverses just the day-number
  line of today's cell (via `rich.text.Text.stylize`, not the constructor's
  whole-cell `style=`) whenever the grid is showing the current year/month.
  Reverse video adapts to any theme instead of hardcoding a color. See
  CHANGELOG.
- [x] **Quote the last message below new reply text** (`[2026-07-18]`) —
  `ComposeModal.on_mount`'s reply/reply-all path now fetches the thread with
  `format="full"` (was `"metadata"`) and, when the new
  `Settings.quote_on_reply` (default on, matching Gmail's web client) is set,
  pre-populates `#c-body` with a blank couple of lines followed by a new
  `gauth.quote_for_reply`-built `"On <date>, <sender> wrote:\n> ..."` block,
  cursor placed at the very top so typing the reply doesn't land inside the
  quote. Offline replies still degrade the same way they already did (cached
  thread summaries carry no body to quote from), so quoting is simply skipped
  there. See CHANGELOG.
- [x] **Filter-as-you-type in `LabelPickerModal`'s label checklist**
  (`[2026-07-18]`) — new `Input#labelpick-search` above the `SelectionList`,
  filtered via new `_fuzzy_filter_labels` (same `_fuzzy_score` idiom as
  Contacts/Email/Tasks' filters). Checked state is tracked separately
  (`self._checked_ids`) so a label checked while filtered stays checked once
  the filter's cleared. `email-label-select` (the folder dropdown) already
  had Textual's built-in `type_to_search` substring-jump — good enough as
  a "search" there since it's single-select and rarely has enough labels to
  need real narrowing; only the multi-select checklist got a dedicated
  filter box. See CHANGELOG.
- [x] **Show applied labels in the Email list, same row as the subject**
  (`[2026-07-18]`) — `list_threads`'s thread-summary dicts now carry
  `labelIds` (union across the thread's messages, `gauth._thread_summary`),
  and `_email_collapsed_line` renders them as a compact inline column
  (new `_thread_label_chips` + `_label_display_name`) — kept on the same
  row rather than a second line, to keep the list compact. User labels
  only, since system ones (INBOX/UNREAD/CATEGORY_*) aren't shown as chips
  in Gmail's own UI either. `ThreadModal`'s separate "Labels: …" line under
  the subject already existed before this change. See CHANGELOG.
- [x] **Date/time shown on Email list rows and the Dashboard MAIL card**
  (`[2026-07-18]`) — appended a formatted date/time (from each thread's raw
  `date` header) to `_email_collapsed_line` and `_populate_dash_mail`'s row
  text, the cheaper of the two options this item originally weighed; a
  `DataTable` rewrite remains open if real sortable columns are wanted
  later. Confirmed thread order from `list_threads` was already
  newest-first, so no sort change was needed. See CHANGELOG.
- [x] **A custom `default_label_id` now survives launch** (`[2026-07-18]`) —
  `email-label-select`'s initial value used to ignore anything other than
  `"ALL"`/`"INBOX"`, then a mount-time `Select.Changed` echo silently
  overwrote a saved custom-label default back to `"INBOX"` on every launch.
  See CHANGELOG.
- [x] **Dashboard MAIL card always means Inbox** (`[2026-07-18]`) — decoupled
  from `_current_label_id`; the Email tab can browse any label while the
  Dashboard's MAIL card keeps showing Inbox unread specifically. See
  CHANGELOG.
- [x] **P1 bug batch from the 2026-07-17 live-usage testing pass**
  (`[2026-07-18]`) — five of eight: the `(1)` suffix on single-message
  threads, retry-once-on-timeout for the label refresh error, Drive/Mail
  preview panes scrolling to top on new content, `Alt+Right`/`Alt+Left`
  focus movement into/out of the Mail/Drive preview columns, and
  `LabelPickerModal` pre-checking already-applied labels (`gauth.get_thread`
  now returns per-message `label_ids`) with a new "Labels: …" line in
  `ThreadModal` confirming a successful apply. The other three (`Ctrl+R`
  crash, reply→archive→reply thread disappearing, dropped forwarded-message
  body) stay open above — each needs a live repro or a real raw-MIME sample
  before a blind fix. See CHANGELOG.
- [x] **Ctrl+K Hermes quick-ask popup + Dashboard card enable/disable**
  (`[2026-07-18]`) — `HermesAskModal` pops up the configured AI provider's
  ask box from any tab; the Dashboard's own Hermes card now names the
  configured provider too (was hardcoded "Hermes"). Settings → Dashboard
  lets you enable/disable any card, at least one always on — the first step
  toward a real card library. Also fixed a shipped-but-broken `Alt+4`
  (silently jumped to the Mail card instead of Hermes since the `[2026-07-17]`
  grid grew past 3 cards). See CHANGELOG.
- [x] **Dashboard tab: Google-native cards** (`[2026-07-17]`) — the 2×2 card
  grid (TODAY / TASKS grouped / MAIL unread / NEWS rotating headlines) + the
  full-width Hermes Ask card, replacing the interim Events/Tasks/Hermes stack.
  Reuses existing Google/feed data — no new fetchers or API keys. The external
  cards (weather/stocks/dictionary/Wikipedia) remain open above. See CHANGELOG.
- [x] Tab/pane redesign: full-width tabs in the blue bar (`Ctrl+#`), with
  Mail originally holding Email / Events / Tasks / Hermes panes (`Alt+1..4`,
  adjacency-based `Alt+arrows`) — superseded `[2026-07-16]` by the Mail/
  Dashboard split above (Mail is Email-only now; Events/Tasks/Hermes moved
  to the Dashboard tab, same `Alt+1..4` keys).
- [x] Threaded email list + thread view + reply/reply-all/forward compose.
  Full thread tree (every message, oldest-first, own `DocumentView` each) —
  not just the latest message — shipped in the P1 M4 rewrite (`bec0aae`,
  see `ThreadModal`/`_apply_thread` in `main.py`); a since-stale ROADMAP P2
  entry claiming otherwise was removed `[2026-07-15]`.
- [x] Calendar tab: full month grid (events in each day square, `+N more`
  overflow modal) and hour-grid week view, modeled on Google Calendar's web UI.
- [x] Tasks list with Space-toggle complete + task detail/subtasks view.
- [x] Hermes Ask pane (LLM for general Qs, agent for action Qs) with live ctx.
- [x] Drive tab: folder browser + live preview pane (metadata always;
  text preview for non-binary/non-image files).
- [x] Search tab (searxng via `hermes web search`).
- [x] Two-row wrapping help bar (contextual above global) + `HelpModal`
  (`Ctrl+H`); `Ctrl+Q` quit, `Ctrl+P` command palette.
- [x] `google-tui` launcher on PATH (venv baked in).
- [x] Verified against live Google data via Textual `run_test` pilot +
  exported SVG screenshots.
- [x] **Local cache + offline mode.** Cache-first startup (`google_tui/
  cache.py`, SQLite), `Header.sub_title` Connecting/Synced/Offline
  indicator, mutating actions disabled while offline, Drive preview reads
  from cache when offline. Thread bodies are also cached (`thread_body`
  category, historyId-stamped, see `[2026-07-14]`) so a previously-opened
  email reopens instantly and reads offline.
- [x] **Offline mutation queue — full CREATE/DELETE.** New Event, Add
  subtask, Delete subtask, Delete task now queue offline (temp-id placeholders
  overlaid at render, replayed on reconnect; a delete whose target is itself a
  queued create just cancels the create). Completes the P3 queue work — see
  CHANGELOG `[2026-07-16]`.
- [x] **Drive true parent-folder tracking.** "Up" now navigates to the
  actual parent via a folder-id stack, not always back to root — see
  CHANGELOG `[2026-07-16]`.
- [x] **Drive preview/info column toggle** (`p`, `action_toggle_preview`)
  — hides `#drive-preview-col` so the file list can claim the full width.
  See CHANGELOG `[2026-07-16]`.
- [x] **Email tab single-purpose + preview pane.** Events/Tasks/Hermes
  relocated to the new Dashboard tab (interim content, see the open
  Dashboard item above); Mail tab is Email-only now, with a `p`-toggled
  preview pane (`action_toggle_preview`, shared with Drive's) showing the
  highlighted thread's latest message, hidden by default (flippable in
  Settings → General), live-updating on highlight while visible. See
  CHANGELOG `[2026-07-16]`.
- [x] **Settings tab** (`Ctrl+5`): encrypt-at-rest toggle (off by default),
  passphrase-at-launch vs. local-keyfile key method, clear-cache button.
  Small "browse" cache rows bulk-decrypt cheaply; large "content" rows
  (Drive file text) decrypt lazily, one at a time, only when opened.
- [x] **Fix dead `Tab`/`Shift+Tab` pane-cycling keys.** `Screen`'s own
  non-priority `tab`/`shift+tab` bindings were winning over the app's
  `cycle`/`cycle_back` actions on every keypress; made those two
  `ActionSpec`s priority bindings and had the actions `SkipAction()` through
  to `Screen`'s default focus-next/previous on tabs where they don't apply.
  See CHANGELOG `[2026-07-16]`.
