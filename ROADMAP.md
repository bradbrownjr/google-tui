# ROADMAP.md — google-tui

Prioritized future work. Each item notes status and the file(s) it touches.
Update this file as items are completed — move the completed item's entry
into CHANGELOG.md under a new dated section (`## [YYYY-MM-DD]`) instead of
just checking it off here, so ROADMAP.md only ever shows what's still open.

## P3 — Robustness

- [ ] **Pagination / "load more".** Email caps at 80 threads; events at 3
  weeks; Drive at one folder page. Add lazy load on scroll / a "More" button.
- [ ] **Token refresh handling.** If `~/.hermes/google_token.json` expires
  without a `refresh_token`, show a guided re-auth message.
- [ ] **Connection pool / rate limiting.** Rapid `Ctrl+R` could trip Google
  quota; debounce refreshes.
- [ ] **Offline mutation queue.** Reply/Forward/toggle-task are currently
  just disabled while offline (`self._require_online()`). Queuing them for
  automatic replay on reconnect would be a real feature, not a small one —
  needs conflict handling (e.g. a task toggled offline AND changed
  elsewhere) and persistence for the queue itself. Deliberately not
  attempted in the initial caching pass.
- [ ] **Live encryption-setting hot-swap.** Toggling encrypt-at-rest or key
  method currently clears the cache and asks for a restart rather than
  rebuilding `self._cache` with the new key in-session. Fine for now; worth
  revisiting if restart-to-apply proves annoying in practice.
- [ ] **Ctrl+# tab bindings don't work over SSH — consider Function keys.**
  `Ctrl+1..8` (`main.py:597-604`) are swallowed by many SSH clients/terminal
  multiplexers before they reach the app. Midnight Commander's F-key
  convention (F1..F8) is generally more SSH-safe; downside is some
  terminals/window managers reserve individual F-keys too (e.g. fullscreen
  toggles). Recommend switching the primary bindings to F1..F8 and keeping
  Ctrl+1..8 as secondary aliases, rather than a straight swap.
  *(Suggested model: Sonnet.)*

## P4 — Nice-to-have

- [ ] **Week view sub-hour granularity** (30/15-minute rows) — the current
  week grid (`#cal-week-grid`) is hour-granularity; an event's summary fills
  every hour row it spans, so start/end times aren't visually precise within
  the hour.
- [ ] **Drive image preview** — currently images show metadata only ("no text
  preview"); in-terminal rendering would need the `textual-image` package
  (not a current dependency).
- [ ] **Drive true parent-folder tracking** — "up" reloads root rather than
  the actual parent; would need a folder-id stack instead of the current
  `path` string.
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
- [ ] **Keyboard-first everywhere** — ensure every mouse action has a key
  equivalent (Drive folder nav, calendar month nav).
- [ ] **Cache email bodies for offline reading.** Only thread summaries
  (subject/from/date) are cached today, not full bodies — opening a thread
  while offline isn't possible yet. Would follow the same lazy,
  cache-on-view pattern as `drive_file_text`.
- [ ] **Dashboard tab** (new first tab): weather, stocks (configurable in
  Settings), dictionary word of the day, Wikipedia picture of the day, top-5
  rotating news headlines (by RSS category, per the RSS settings), unread
  email count, tasks due/overdue/unscheduled, and today's events (scheduled +
  all-day, selectable calendar sources in Settings). Should ship with
  reasonable moderate defaults rather than an empty dashboard. Largest
  net-new feature on this list — new fetchers for weather/stocks/
  dictionary/Wikipedia, a new tab, and several new Settings rows.
  *(Suggested model: Opus.)*
- [ ] **Email tab becomes single-purpose; add preview pane.** Make the email
  viewer the only content on its tab (today it shares the Mail tab with
  Events/Tasks/Hermes panes via Alt+1..4), and add an inline preview pane so
  a highlighted message can be read without opening `ThreadModal`.
  *(Suggested model: Opus — layout rework of the whole Mail tab.)*
- [ ] **Toggle preview/info column in Email and Drive**, default to visible
  when the terminal is wide enough to fit it. *(Suggested model: Sonnet.)*
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

- [x] Tab/pane redesign: Mail / Calendar / Drive / Search full-width tabs in
  the blue bar (`Ctrl+1..4`); Mail tab holds Email / Events / Tasks / Hermes
  panes (`Alt+1..4`, adjacency-based `Alt+arrows`).
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
- [x] **Settings tab** (`Ctrl+5`): encrypt-at-rest toggle (off by default),
  passphrase-at-launch vs. local-keyfile key method, clear-cache button.
  Small "browse" cache rows bulk-decrypt cheaply; large "content" rows
  (Drive file text) decrypt lazily, one at a time, only when opened.
