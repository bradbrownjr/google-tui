# ROADMAP.md — google-tui

Prioritized future work. Each item notes status and the file(s) it touches.
Update this file as items are completed — move the completed item's entry
into CHANGELOG.md under a new dated section (`## [YYYY-MM-DD]`) instead of
just checking it off here, so ROADMAP.md only ever shows what's still open.

## P0 — Safety (do before relying on it daily)

- [ ] **Live send smoke test (supervised).** Now that Send has a 5-second
  cancelable countdown (see CHANGELOG `[2026-07-13]`) instead of firing
  immediately, do ONE real reply to a test thread with your own
  `~/.hermes/google_token.json` and verify To/Subject/body/threading.
  Needs a real Google token and an actual send, so it can't be done from
  this sandbox — run it yourself when ready. Never auto-send in
  unsupervised tests — mock `gauth.reply_to`/`forward` as usual.

## P1 — Major Feature Epics (ordered build sequence)

Epics remaining from the 2026-07-13 planning pass (Labels-as-folders,
multi-provider AI/onboarding, the Google Console setup guide, M1's shared
rendering module, M2's Browser tab, M3's News tab, M4's rich HTML email
rendering, M5's Contacts tab, and M6's Navigation tab all shipped — see
CHANGELOG). Only M7 (repo screenshot) remains, deliberately LAST — take it
now that the major UI work (M5 Contacts) has landed, so it's a current
snapshot instead of one that goes stale after the next epic. Tagged with
the Claude Code agent recommended for a future session tackling it —
**Explore** for read-only research, **Plan** for architecture/design before
non-trivial code, **general-purpose** for the actual multi-step
implementation, **claude-code-guide** where the step is specifically about
the Claude Code CLI/SDK itself. Small one-shot steps with no real
research/design component are left untagged (just do them).

### M7 — Repo screenshot
Last, on purpose — a single current snapshot taken once the major UI
work above (M5 Contacts) has landed, rather than one that goes stale after
the next epic. (M2 Browser, M3 News, M4 rich HTML, M5 Contacts, and M6
Navigation already landed, see CHANGELOG.)
- [ ] Build a fake dataset (dummy threads/events/tasks/Drive files, zero
  real PII) and drive the app against it with the existing `run_test`
  pilot + `save_screenshot` → cairosvg pipeline (AGENTS.md §6) to produce
  a PNG. *(general-purpose)*
- [ ] Add it to the top of README.md as the project hero image.

## P2 — UX polish

- [ ] **Task subtasks + add/delete.** `TaskDetailModal` shows subtasks
  read-only. Add create/check/delete for tasks and subtasks (need
  `gauth.create_task` / `delete_task` / `patch_subtask`).
- [ ] **Calendar create event** from a modal (date/time/title) →
  `gauth.create_event`. Currently read-only.
- [ ] **Threading depth.** Show full thread tree (multiple messages) with
  expand/collapse; today each thread shows the latest message only.
- [ ] **Search within panes** (filter emails/tasks live as you type).

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
