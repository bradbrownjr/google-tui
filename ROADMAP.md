# ROADMAP.md — google-tui

Prioritized future work. Each item notes status and the file(s) it touches.
Update this file as items are completed — move the completed item's entry
into CHANGELOG.md under a new dated section (`## [YYYY-MM-DD]`) instead of
just checking it off here, so ROADMAP.md only ever shows what's still open.

## P2 — UX polish

- [ ] **Task subtasks + add/delete.** `TaskDetailModal` shows subtasks
  read-only. Add create/check/delete for tasks and subtasks (need
  `gauth.create_task` / `delete_task` / `patch_subtask`).
- [ ] **Calendar create event** from a modal (date/time/title) →
  `gauth.create_event`. Currently read-only.
- [ ] **Threading depth.** Show full thread tree (multiple messages) with
  expand/collapse; today each thread shows the latest message only.
- [ ] **Search within panes** (filter emails/tasks live as you type).
- [ ] **Web browser: Alt+H home, fix Alt+Left history, slow Page Up/Down/End.**
  Add a home shortcut (Alt+H) to a configurable start URL. `action_switch_left`
  already calls `_browser_back()` when `tab-browser` is active
  (`main.py:1435-1437`), but Alt+Left is reported as not going back — likely
  swallowed by focus on `#browser-url`/`DocumentView` in some states; needs
  repro + fix. Separately, Page Up/Down/End inside `DocumentView` are reported
  as very slow on large pages — profile the scroll/render path.
  *(Suggested model: Sonnet.)*
- [ ] **Numbered inline links: wire up activation outside the Browser tab, and
  color link text.** `render.py`'s link numbering (ported from `bpq-apps/apps/
  htmlview.py` — nav links hidden until requested, inline `[N]` content links)
  already renders correctly in email bodies and elsewhere via `DocumentView`,
  but `on_document_view_link_activated` only acts on it in the Browser tab —
  `ThreadModal` and `NewsEntryModal` are explicit no-ops today (see
  `ThreadModal`'s docstring, `main.py:3236-3240`). Wire up number-key activation
  in both, and give link text its own color/style in `render.py` so links are
  visually distinct from body text. *(Suggested model: Sonnet.)*
- [ ] **Email viewer (`ThreadModal`): help bar, keyboard nav, and actions.**
  Currently a bare button row (Reply/Reply All/Forward/Close) with no help bar.
  Add:
  - A contextual help bar listing this modal's shortcuts (consistent with the
    rest of the app), with entries clickable the same way as the global help bar.
  - Left/Right to move to the next/previous message in the current folder
    without closing the modal.
  - `/` to search within the open message — context-aware continuation of the
    app's existing search-within-pane behavior.
  - `R`/`A`/`F` key shortcuts matching the existing Reply/Reply All/Forward
    buttons, plus `D` delete, `S` save-and-archive (remove from inbox), and `L`
    assign labels.
  - Border on the dialog and any missing buttons for the above actions.
  - Mark the thread read on open (already partially done — `gauth.mark_read`
    fires in `_fetch_thread`, `main.py:3300-3304`) and add a shortcut to mark a
    thread unread again from the list.
  *(Suggested model: Opus — touches `gauth` for delete/archive/labels, new
  modal-local bindings, and the shared help-bar/search patterns; biggest single
  item on this list.)*

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
- [ ] **Rich text in email** — render minimal HTML (bold/links) instead of
  stripping to plain text only.
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
