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
- [ ] **Offline cache + error toasts.** Cache last good fetch; if refresh
  fails (network/token), show a non-fatal toast instead of a blank pane.
- [ ] **Token refresh handling.** If `~/.hermes/google_token.json` expires
  without a `refresh_token`, show a guided re-auth message.
- [ ] **Connection pool / rate limiting.** Rapid `Ctrl+R` could trip Google
  quota; debounce refreshes.

## P3 — Nice-to-have

- [ ] **Week view time-grid** (Gantt-style columns per day with timed events)
  instead of the current simple day-column list.
- [ ] **Config file** (`config.toml`) for: default LLM model, timezone, pane
  order, searxng URL, refresh interval.
- [ ] **Rich text in email** — render minimal HTML (bold/links) instead of
  stripping to plain text only.
- [ ] **Multiple accounts** switch (if a second token appears).
- [ ] **Unit tests in-repo** (`tests/`) using the `run_test` pilot pattern from
  AGENTS.md §6, runnable via `pytest`.
- [ ] **Keyboard-first everywhere** — ensure every mouse action has a key
  equivalent (Drive folder nav, calendar month nav).

## Done

- [x] 4-pane layout: Email / Calendar / Tasks / Hermes Ask (resize-reactive).
- [x] Threaded email list + thread view + reply/reply-all/forward compose.
- [x] Calendar upcoming list + full month/week modal + event detail.
- [x] Tasks list with Space-toggle complete + task detail/subtasks view.
- [x] Hermes Ask pane (LLM for general Qs, agent for action Qs) with live ctx.
- [x] Drive browser (folders, plaintext read, binary download).
- [x] Search button (searxng via `hermes web search`).
- [x] Pane switching: Alt+arrows, Tab/Shift+Tab, 1-4, Ctrl+R refresh.
- [x] `google-tui` launcher on PATH (venv baked in).
- [x] Verified against live Google data via Textual `run_test` pilot.
