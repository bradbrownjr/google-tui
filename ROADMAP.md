# ROADMAP.md — google-tui

Prioritized future work. Each item notes status and the file(s) it touches.
Update this file as items are completed — move the completed item's entry
into CHANGELOG.md under a new dated section (`## [YYYY-MM-DD]`) instead of
just checking it off here, so ROADMAP.md only ever shows what's still open.

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
  subscribed feeds) — plus the Hermes Ask card full-width below. Still to
  build, the remaining half: **weather**, **stocks** (symbols configurable in
  Settings), **dictionary word of the day**, **Wikipedia picture of the day**.
  Each needs a new fetcher (Open-Meteo / a stocks API / a dictionary API /
  Wikipedia REST `featured` endpoint) plus its own Settings rows (weather
  location, stock symbols, on/off toggles) and a new card slot in the grid
  (the layout mechanism — `#dashboard-body` Grid, `DASH_PANE_IDS`,
  `DASH_ADJACENCY`, `_apply_narrow_layout` — is already in place; adding a
  card is: a `Container` in `compose()`, an id in `DASH_PANE_IDS`, an
  adjacency entry, a `_fetch_*`/`_apply_*` split per AGENTS.md §8, and the
  fetcher). The news-headline card currently pulls from ALL subscribed feeds
  newest-first, not "top-5 by RSS category" — per-category selection is a
  possible refinement if the flat list proves too noisy. *(Suggested model:
  Opus for the fetchers + Settings; the grid wiring is now mechanical.)*
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
