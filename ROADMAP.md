# ROADMAP.md — google-tui

Prioritized future work. Each item notes status and the file(s) it touches.
Update this file as items are completed (and bump CHANGELOG.md).

## P0 — Safety (do before relying on it daily)

- [ ] **Send confirmation dialog.** `ComposeModal` currently fires
  `gauth.reply_to` / `forward` immediately on send. Add a confirm step
  (e.g. `y` to send / `Esc` to cancel) so a misfire can't email someone.
  Touches `main.py` `ComposeModal.on_button_pressed`.
- [ ] **Live send smoke test (supervised).** Once confirmation exists, do ONE
  real reply to a test thread and verify To/Subject/body/threading. Never
  auto-send in tests — mock `gauth.reply_to`/`forward`.
- [ ] **`on_dismiss` is likely dead code.** Discovered while wiring up
  `UnlockModal`: `ModalScreen.Dismissed` doesn't exist in the installed
  Textual version (`hasattr(ModalScreen, "Dismissed")` is `False`), and
  `GoogleTUI.on_dismiss(self, event: ModalScreen.Dismissed)` only imports
  cleanly because `from __future__ import annotations` never evaluates the
  annotation. This method is very likely never invoked, meaning
  `ThreadModal`'s Reply/Reply All/Forward buttons (which dismiss with
  `("compose", tid, mode")` expecting `on_dismiss` to catch it and push
  `ComposeModal`) silently do nothing. Not fixed this round (out of scope
  for the caching/settings work). Fix: route that result through
  `push_screen(ThreadModal(...), callback)` instead — see the
  `push_screen`-callback-timing NOTE in AGENTS.md §2 (the callback fires
  BEFORE the screen pops, so defer any widget-touching work one step via
  `call_after_refresh`).

## P1 — UX polish

- [ ] **Task subtasks + add/delete.** `TaskDetailModal` shows subtasks
  read-only. Add create/check/delete for tasks and subtasks (need
  `gauth.create_task` / `delete_task` / `patch_subtask`).
- [ ] **Email compose from scratch** (new message, not reply) — `ComposeModal`
  is reply/forward-only today. Add a "New" button.
- [ ] **Calendar create event** from a modal (date/time/title) →
  `gauth.create_event`. Currently read-only.
- [ ] **Threading depth.** Show full thread tree (multiple messages) with
  expand/collapse; today each thread shows the latest message only.
- [ ] **Search within panes** (filter emails/tasks live as you type).

## P2 — Robustness

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

## P3 — Nice-to-have

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
