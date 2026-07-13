# google-tui

A multi-pane terminal UI (TUI) for your Google Workspace, built with
[Textual](https://textual.textualize.io/).

## Features

Five full-width **tabs** live in the blue bar: **Mail**, **Calendar**,
**Drive**, **Search**, **Settings** (`Ctrl+1..5`). The Mail tab holds four
**panes**: Email, Events, Tasks, Hermes (`Alt+1..4`, or `Alt+arrows` to move
relatively).

**Works offline, to a degree.** The app is cache-first: whatever it fetched
last time shows up instantly on launch, while it reconnects to Google in the
background (`Connecting…` → `Synced HH:MM` in the title bar). If it can't
reach Google at all, you still get your cached inbox, calendar, tasks, and
any Drive files you've previously viewed — the title bar shows `Offline
(cached HH:MM)`, and actions that need a live connection (reply, forward,
toggling a task) are disabled with a warning instead of failing silently.

- **Mail tab — Email pane** (left, full height): threaded Gmail list with a
  lightbar. `Enter`/`Space` opens the thread; `r` / `a` / `f` reply /
  reply-all / forward (compose modal). Unread threads are marked with a bullet.
- **Mail tab — Events pane** (upcoming): next ~3 weeks of events, lightbar,
  `Enter`/`Space` for detail.
- **Mail tab — Tasks pane:** all Google Task lists, lightbar. `Space` toggles
  complete, `Enter` shows details/subtasks.
- **Mail tab — Hermes pane:** type a question and `Enter`. General questions
  are answered by the Nous LLM using your live Google context; requests that
  look like actions are delegated to the full Hermes agent.
- **Calendar tab:** a full **Month** view (events listed inside each day's
  square, `+N more` overflow opens a modal with the day's full list) and a
  **Week** view (hour-grid, day columns, event blocks) — modeled on Google
  Calendar's web UI. `[` / `]` page the month or week.
- **Drive tab:** folder browser on the left; a live preview pane on the
  right shows file metadata (owner, type, path, created/modified) and, for
  non-binary/non-image files, a text preview. Files you've viewed are
  available offline too.
- **Search tab:** text-based web search via your configured searxng backend
  (shells `hermes web search`).
- **Settings tab:** turn on encrypt-at-rest for the local cache (off by
  default — it costs nothing until you ask for it), choose how the
  encryption key is handled, or clear the local cache.

## Layout & keys

```
┌[Mail¹] Calendar² Drive³ Search⁴ Settings⁵ ── Synced 14:32 ────┐  ← blue bar
├─ EMAIL ──────────────────────┐ ┌─ EVENTS ─────────────────────┤
│ ▸ Frank Krizan                │ │ ▸ 07/13 Tick/Flea Appt       │
│   Fwd: [DigiPi] …             │ │ ▸ 07/15 OHD Water Testing    │
│                                │ ├─ TASKS ──────────────────────┤
│                                │ │ [ ] Buy cat food             │
│                                │ │ [x] Pay electric bill        │
│                                │ ├─ HERMES ASK ─────────────────┤
│                                │ │ > ask a question, Enter      │
└────────────────────────────────┘ └───────────────────────────────┘
```

| Key | Action |
|-----|--------|
| `Ctrl+1..5` | switch tab (Mail / Calendar / Drive / Search / Settings) |
| `Ctrl+Left/Right` | cycle tabs — use this if `Ctrl+1..5` doesn't reach the app (common in browser-based terminals, which reserve `Ctrl+1..8` for switching *their own* tabs) |
| `Alt+1..4` | jump to Mail pane (Email / Events / Tasks / Hermes) |
| `Alt+Left/Right/Up/Down` | move to the adjacent Mail pane |
| `Tab` / `Shift+Tab` | cycle Mail panes |
| `r` `a` `f` | reply / reply-all / forward (Email pane, disabled while offline) |
| `Space` | contextual: expand thread (Email), toggle complete (Tasks, disabled while offline), event detail (Events) |
| `Enter` | open selected item's detail |
| `[` `]` | previous / next month or week (Calendar tab) |
| `Ctrl+R` | reconnect / refresh all data |
| `Ctrl+P` | command palette |
| `Ctrl+H` | help (full keybinding reference) |
| `Ctrl+Q` / `Esc` | quit / close modal |

The layout is resize-reactive (Textual auto-reflows on terminal resize); the
bottom help bar wraps its own text if the terminal is narrow.

## Setup

Requires Python 3.11+. Uses your existing Hermes Google token at
`~/.hermes/google_token.json` (Gmail/Calendar/Drive/Tasks scopes) and the
Nous inference key in `~/.hermes/config.yaml` for the Ask pane.

```bash
cd google-tui
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -e .
google-tui          # launches the TUI
```

## Project layout

```
google-tui/
├── pyproject.toml
├── google_tui/
│   ├── __init__.py
│   ├── __main__.py        # `python -m google_tui` entry
│   ├── gauth.py          # Google auth + Gmail/Cal/Tasks/Drive helpers
│   ├── ask.py            # Hermes Ask (LLM) + search backends
│   ├── cache.py          # local SQLite cache, optional per-row encryption
│   ├── settings.py       # user preferences (settings.json)
│   └── main.py          # the Textual app, tabs, panes, and modals
└── README.md
```

## Notes

- The Hermes Ask pane calls `tencent/hy3:free` via the Nous endpoint by default
  (configurable in `ask.py`). Action-type questions shell `hermes` so the full
  agent (tools/skills) handles them.
- Replying/forwarding uses Gmail threads and sets In-Reply-To automatically.
- Drive plaintext rendering exports Google-native files (Docs→txt, Sheets→csv).
- Local cache lives at `~/.cache/google-tui/cache.db`; preferences at
  `~/.config/google-tui/settings.json` (both via `platformdirs`, so the
  actual path follows XDG conventions on Linux). Encryption is off by
  default; turn it on from the Settings tab. Small "browse" data (thread
  subjects, event/task summaries) is cached in bulk; larger content (email
  bodies aren't cached at all yet — only summaries; Drive file text) is
  cached lazily, only after you've actually viewed it once online, so
  encryption overhead never scales with your whole account, only with what
  you've looked at.
