# google-tui

A multi-pane terminal UI (TUI) for your Google Workspace, built with
[Textual](https://textual.textualize.io/).

## Features

- **Email (left, full height):** threaded Gmail list with a lightbar. `Enter`
  opens the thread; `r` / `a` / `f` reply / reply-all / forward (compose modal).
  Unread threads are marked with a bullet.
- **Calendar (upcoming):** next ~3 weeks of events, lightbar-selectable for a
  detail dialog. `c` opens a full **month + week** calendar modal.
- **Tasks:** all Google Task lists, lightbar. `Space` toggles complete,
  `Enter` shows details/subtasks.
- **Hermes Ask (compact, bottom-right):** type a question and `Enter`. General
  questions are answered by the Nous LLM using your live Google context; requests
  that look like actions are delegated to the full Hermes agent.
- **Drive button:** browse folders, open nested folders, read Google Docs/Sheets
  exported to plaintext, and download binary files.
- **Search button:** text-based web search via your configured searxng backend
  (shells `hermes web search`).

## Layout & keys

```
┌─ EMAIL (threads) ──┐ ┌─ CALENDAR ──────┐
│ ▸ Frank Krizan       │ │ ▸ 07/13 Tick/Flea │
│   Fwd: [DigiPi]     │ │ ▸ 07/15 OHD Water│
├──────────────────────┤ ├─ TASKS ──────────┤
│                      │ │ [ ] Buy cat food   │
│                      │ │ [x] Pay electric  │
└──────────────────────┘ ├─ HERMES ASK ─────┤
                          │ > ask a question… │
                          └───────────────────┘
```

| Key | Action |
|-----|--------|
| `Alt+Left/Right/Up/Down` | switch pane |
| `Tab` / `Shift+Tab` | cycle panes |
| `1` `2` `3` `4` | jump to Email / Calendar / Tasks / Hermes |
| `r` `a` `f` | reply / reply-all / forward (email pane) |
| `Space` | toggle task complete |
| `Enter` | open selected item's detail |
| `c` | full calendar view |
| `d` | Drive browser |
| `s` | Google search |
| `Ctrl+R` | refresh all panes |
| `q` / `Esc` | quit / close modal |

The layout is resize-reactive (Textual auto-reflows on terminal resize).

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
│   └── main.py          # the Textual app, panes, and modals
└── README.md
```

## Notes

- The Hermes Ask pane calls `tencent/hy3:free` via the Nous endpoint by default
  (configurable in `ask.py`). Action-type questions shell `hermes` so the full
  agent (tools/skills) handles them.
- Replying/forwarding uses Gmail threads and sets In-Reply-To automatically.
- Drive plaintext rendering exports Google-native files (Docs→txt, Sheets→csv).
