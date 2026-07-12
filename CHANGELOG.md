# CHANGELOG.md — google-tui

Format: keep newest at top. One entry per meaningful change. Reference files
touched and any breaking notes.

## [Unreleased]

### Added
- `AGENTS.md`, `ROADMAP.md`, `CHANGELOG.md` for cold-start continuation.
- `google-tui` launcher at `/home/bradb/.local/bin/google-tui` (on PATH)
  that bakes in venv activation — runs from any shell without sourcing `.venv`.

### Changed
- `_mk_id` helper moved to MODULE level (was a class method) so `DriveModal`
  can use it; fixed a latent bug where naming a method `_id` collided with
  Textual's internal `DOMNode._id` (caused `'NoneType' not callable` at
  `refresh_all`). (main.py)

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
