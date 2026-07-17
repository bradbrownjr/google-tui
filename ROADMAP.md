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

## P2 — Drive

- [ ] **Better binary-vs-image detection in the file preview**, instead of
  the current single "(binary/image file — no text preview)" message for
  everything `_is_previewable()` (`main.py:925-926`) says no to. Split the
  message (and eventually the handling) by mimetype: an actual image
  (`image/*`) vs. a real binary (executables, archives, etc.) are different
  situations for a user, even though neither gets a text preview today. Full
  in-terminal image rendering is the bigger P4 item below (needs
  `textual-image`); this is just the smaller "say what kind of file it is"
  fix using the mimetype already fetched via `gauth.get_file_metadata`.
- [ ] **Download a Drive file to the local filesystem** — no such action
  exists today; `gauth.read_drive_text`/`get_file_metadata` only read for
  in-app preview. Would need a new `gauth` download helper (`files().get_media`
  for binary, `files().export_media` for native Google Docs/Sheets/Slides —
  same `_MIME_EXPORT` table `gauth.py:633+` already uses for text preview)
  and a destination-path prompt, likely reusing whatever prompt/modal pattern
  Navigation's `_export_itinerary` (`main.py:~350`) already established for
  "write a file to `user_documents_dir()`."

## P2 — General UI

- [ ] **Drop the underline on clickable/link text** — makes it harder to
  read, not easier. Best candidate found: `render._LINK_STYLE = "underline
  bright_cyan"` (`render.py:1023`), used for every numbered `[N]` link in
  `DocumentView` (Browser pages, Gopher/Gemini menus, search results, News
  entries, HTML email bodies) — try `bright_cyan` alone, no underline, and
  confirm that's the element being reported (vs. e.g. a `Button` label)
  before changing it.

## P3 — Browser

- [ ] **SFTP/SCP** — the FTP/SFTP/SCP browser item shipped `[2026-07-18]`
  scoped down to plain FTP only (see Done below); SFTP/SCP still need a new
  dependency (`paramiko` — nothing like it is in `pyproject.toml` today) and
  are unbuilt. Also still open: **download a Drive/FTP file to the local
  filesystem** — no such action exists for either protocol yet (shares the
  destination-path-prompt need with the `## P2 — Drive` download item
  above); `fetch_ftp` (`fetchers.py`) only reads for in-app preview, same
  gap `gauth.read_drive_text` has for Drive.

## P4 — Nice-to-have

- [ ] **Week view sub-hour granularity** (30/15-minute rows) — the current
  week grid (`#cal-week-grid`) is hour-granularity; an event's summary fills
  every hour row it spans, so start/end times aren't visually precise within
  the hour.
- [ ] **Drive image preview** — currently images show metadata only ("no text
  preview"); in-terminal rendering would need the `textual-image` package
  (not a current dependency).
- [ ] **Config file** (`config.toml`) for: default LLM model, timezone, pane
  order, searxng URL, refresh interval.
- [ ] **Markdown detection + rendering** in Drive file preview, calendar/task
  descriptions, and (cautiously) email bodies. `render.py` today only
  understands HTML (`parse_html`), Gemtext (`parse_gemtext`), and Gopher
  (`parse_gopher_menu`) — no Markdown parser exists. The natural entry
  point is `parse_feed_entry` (`render.py:953`): it already sniffs
  `_HTML_TAG_RE` and routes to `parse_html`, else wraps each line as an
  unstyled paragraph `Block` — add a Markdown sniffer + `parse_markdown()`
  as a second branch there, producing the same `Block`/`Link` vocabulary
  `DocumentView` already renders. Doing just that lights up Markdown in
  email bodies for free, since `ThreadModal` already routes every message
  through `parse_feed_entry` (`main.py:3420-3423`, added for HTML email
  rendering — P1 M4, `bec0aae`). Two more surfaces don't go through the
  shared Document/`DocumentView` pipeline at all yet and need that wiring
  first:
  - **Drive file preview** (`#drive-preview-text`, `main.py:826`) is a
    plain `RichLog(markup=False)` — a `.md` file's raw source is dumped
    verbatim today. Detect by extension/mimetype and switch to
    `DocumentView`.
  - **`EventModal`'s description** (`main.py:3663`) interpolates
    `e.get('description','')` raw into an f-string on a plain `Static` —
    no rendering of any kind yet, not even HTML. Moving it onto
    `DocumentView`/`parse_feed_entry` gets HTML-sniffing (Google Calendar's
    rich-text editor often produces HTML descriptions) for free, with
    Markdown as the second win, not the only one. Same gap likely applies
    to Task notes — worth checking `TaskDetailModal` while in there.
  Real design question, not just plumbing: detection needs to be
  conservative. A false-positive Markdown parse on an ordinary plain-text
  email (a stray `_word_` or `*note*`) would look worse than leaving it
  unrendered — the sniffer should require several Markdown-syntax hits
  (headers, fenced code, list markers), not just one asterisk or
  underscore. *(Suggested model: Sonnet for the parser/sniffer + Email
  wiring; Drive/Calendar/Task integration is separate, mostly mechanical,
  surface work once `parse_markdown()` exists.)*
- [ ] **Multiple accounts** switch (if a second token appears).
- [ ] **Unit tests in-repo** (`tests/`) using the `run_test` pilot pattern from
  AGENTS.md §6, runnable via `pytest`.
- [ ] **Keyboard-first everywhere** — audit remaining tabs/modals for
  mouse-only actions. Drive folder nav (arrow keys + Enter) and Calendar
  month/week nav (`[`/`]`) — the two examples this item used to name — are
  already fully keyboard-accessible; confirmed `[2026-07-16]` via a
  keyboard-only pilot, no code change needed. What's left, if anything, is
  unaudited — re-scope once something concrete turns up.
- [ ] **Cache email bodies for offline reading.** Only thread summaries
  (subject/from/date) are cached today, not full bodies — opening a thread
  while offline isn't possible yet. Would follow the same lazy,
  cache-on-view pattern as `drive_file_text`.
- [ ] **Dashboard tab: the external cards.** The Google-native half shipped
  `[2026-07-17]` (see CHANGELOG / the Done list below): a 2×2 card grid —
  TODAY (today's events), TASKS (grouped overdue/today/upcoming/unscheduled),
  MAIL (unread count + top unread), NEWS (top rotating headlines from the
  subscribed feeds) — plus the Hermes Ask card full-width below. Card enable/
  disable (Settings → Dashboard) shipped `[2026-07-18]`, making `DASH_
  PANE_IDS` a real "card library" rather than a fixed 5 — see AGENTS.md's
  `DASH_ADJACENCY` NOTE. Still to build, the remaining half: **weather**,
  **stocks** (symbols configurable in Settings), **dictionary word of the
  day**, **Wikipedia picture of the day**. Each needs a new fetcher
  (Open-Meteo / a stocks API / a dictionary API / Wikipedia REST `featured`
  endpoint) plus its own Settings rows (weather location, stock symbols) and
  a new card slot (the layout + enable/disable mechanism — `#dashboard-body`
  Grid, `DASH_PANE_IDS`, `DASH_ADJACENCY`, `_apply_narrow_layout`,
  `_apply_dashboard_panes_enabled` — is already in place; adding a card is:
  a `Container` in `compose()`, an id in `DASH_PANE_IDS` + `PANE_TITLES`
  [auto-appears in the Settings checklist], an adjacency entry, a `_fetch_*`/
  `_apply_*` split per AGENTS.md §8, and the fetcher — no separate on/off
  toggle to wire per-card, the checklist already covers any id in `DASH_
  PANE_IDS`). The news-headline card currently pulls from ALL subscribed
  feeds newest-first, not "top-5 by RSS category" — per-category selection
  is a possible refinement if the flat list proves too noisy. *(Suggested
  model: Opus for the fetchers + Settings; the grid/enable-disable wiring is
  now mechanical.)*
- [ ] **RSS subscription list.** Categorized checklist of popular feeds to
  toggle on/off, plus add-your-own custom feed URL (Settings already has a
  feed list at `#settings-feed-list`, `main.py:569` — extend it rather than
  replace it). *(Suggested model: Sonnet.)*
- [ ] **Usenet support.** Needs a curated list of popular public Usenet
  servers plus support for an arbitrary/unlisted server URL, with credentials
  and API support. Should ship with a small curated server list rather than
  an empty form. *(Suggested model: Opus — new protocol client (NNTP),
  credential storage, and Settings UI.)*

## Done

- [x] **Plain FTP browsing** (`[2026-07-18]`) — new `fetchers.fetch_ftp`
  (`ftplib`, stdlib) browses `ftp://` directories (MLSD, falling back to
  parsing classic Unix `LIST` output) and previews files, wired into the
  Browser tab's address bar/link-follow like HTTP/Gopher/Gemini. Anonymous
  by default; an auth failure pops a login prompt (`FtpLoginModal`), with
  credentials optionally saved (Fernet-encrypted with the same key the local
  cache's encrypt-at-rest uses, in their own file — not cache rows — so
  "Clear Cache" can't wipe them; new `ftp_creds.py`). Settings → General
  gained a "Saved FTP hosts" view/remove list. SFTP/SCP deliberately
  deferred — still open above. See CHANGELOG.
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
  from cache when offline.
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
