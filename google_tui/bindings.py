"""Single source of truth for google-tui's keyboard shortcuts.

Before this module, a key/action pairing lived in up to four independently
hand-maintained places (App.BINDINGS, the global help row, the per-tab/pane
context help row, and HelpModal's HELP_TEXT), so they silently drifted out
of sync — e.g. ComposeModal's Ctrl+Enter was undiscoverable. ActionSpec is
now the one place a shortcut is declared; BINDINGS lists are generated from
it so they can't drift apart again.

HELP_GLOBAL_TEXT, CONTEXT_HELP, and HELP_TEXT stay hand-curated strings
rather than being mechanically rebuilt from individual ActionSpecs: the
current text often condenses several bindings into one summary phrase
(e.g. "Ctrl+1..8 Switch tab" covers eight separate ActionSpecs), which is a
deliberate editorial choice, not something a generator should reverse-
engineer. Centralizing them here alongside ACTIONS still means there's one
file to edit when a shortcut changes, instead of three scattered ones.
"""
from __future__ import annotations

from dataclasses import dataclass

from textual.binding import Binding


@dataclass(frozen=True)
class ActionSpec:
    id: str
    keys: str | None
    label: str
    scope: str = "global"
    show_in_help: bool = True
    bindable: bool = True
    # Textual checks priority bindings App-down before the event ever reaches
    # the focused widget/Screen. Needed for "cycle"/"cycle_back": Screen's own
    # BINDINGS silently claims bare tab/shift+tab first otherwise (see the
    # comment on those two ActionSpecs below).
    priority: bool = False


# Transcribed verbatim from the former GoogleTUI.BINDINGS list — order
# preserved (it matters for anything that walks BINDINGS in sequence).
GLOBAL_ACTIONS: list[ActionSpec] = [
    ActionSpec("switch_left", "alt+left", "Pane Left"),
    ActionSpec("switch_right", "alt+right", "Pane Right"),
    ActionSpec("switch_up", "alt+up", "Pane Up"),
    ActionSpec("switch_down", "alt+down", "Pane Down"),
    # priority=True: Textual's Screen base class binds bare tab/shift+tab to
    # app.focus_next/focus_previous itself (show=False, non-priority), and
    # wins over these same-key bindings whenever any widget has focus --
    # Screen sits closer than App in the non-priority binding chain, so its
    # binding matches first. priority=True moves these into the App-down
    # pass that runs before that chain is ever walked. action_cycle/
    # action_cycle_back raise SkipAction on tabs where they don't apply, so
    # the key falls through to Screen's default focus_next/previous there
    # instead of being silently swallowed.
    ActionSpec("cycle", "tab", "Cycle", priority=True),
    ActionSpec("cycle_back", "shift+tab", "Cycle", priority=True),
    ActionSpec("goto_tab_dashboard", "f1,ctrl+1", "Dashboard"),
    ActionSpec("goto_tab_mail", "f2,ctrl+2", "Mail"),
    ActionSpec("goto_tab_calendar", "f3,ctrl+3", "Calendar"),
    ActionSpec("goto_tab_drive", "f4,ctrl+4", "Drive"),
    ActionSpec("goto_tab_browser", "f5,ctrl+5", "Browser"),
    ActionSpec("goto_tab_news", "f6,ctrl+6", "News"),
    ActionSpec("goto_tab_navigation", "f7,ctrl+7", "Navigation"),
    ActionSpec("goto_tab_contacts", "f8,ctrl+8", "Contacts"),
    # No F-key alias: F9+ isn't reliably delivered by every terminal (the
    # same reasoning that already capped the alias range at F8) -- Ctrl+9
    # is the only way in for this 9th tab.
    ActionSpec("goto_tab_settings", "ctrl+9", "Settings"),
    ActionSpec("cycle_tab_back", "ctrl+left", "Prev Tab"),
    ActionSpec("cycle_tab", "ctrl+right", "Next Tab"),
    ActionSpec("goto_pane_email", "alt+1", "Email"),
    ActionSpec("goto_pane_events", "alt+2", "Events"),
    ActionSpec("goto_pane_tasks", "alt+3", "Tasks"),
    ActionSpec("goto_pane_hermes", "alt+4", "Hermes"),
    # Pops up a small quick-ask modal for the configured AI provider
    # (Settings -> AI Provider) from ANY tab/screen, without navigating to
    # the Dashboard tab the way Alt+4 does -- see HermesAskModal. Ctrl+<letter>
    # is a real ASCII control code every terminal transmits reliably (same
    # reasoning already established for Ctrl+R/Ctrl+H/Ctrl+Q/Ctrl+P below),
    # unlike the Ctrl+<digit>/F9+ caveats noted elsewhere in this file.
    ActionSpec("hermes_popup", "ctrl+k", "Ask"),
    ActionSpec("reply", "r", "Reply"),
    ActionSpec("reply_all", "a", "Reply All"),
    ActionSpec("forward", "f", "Forward"),
    ActionSpec("compose_new", "c", "Compose"),
    ActionSpec("mark_unread", "u", "Unread"),
    # `*` (Textual key name "asterisk") toggles STARRED on the highlighted
    # Email-list thread. Chose `*` over Gmail's `s` because `s` is already
    # ThreadModal's Archive binding — keeping `s` free of a second meaning
    # avoids a "same key, different action depending on whether a thread is
    # open" trap; `*` is unclaimed and reads as the star glyph.
    ActionSpec("star", "asterisk", "Star"),
    # Turn the highlighted Email-list thread into a Google Task ('t') or a
    # Calendar event ('e'). Both no-op off the Mail tab (like the other
    # single-letter mail actions), so a bare 't'/'e' elsewhere is harmless.
    ActionSpec("email_to_task", "t", "To Task"),
    ActionSpec("email_to_event", "e", "To Event"),
    # Multi-select: `x` checks/unchecks the highlighted thread (Gmail-style),
    # `X` (shift+x) opens the bulk-action chooser for the checked set. Both
    # Mail-tab-gated. Distinct Textual keys ("x" vs "X").
    ActionSpec("select_thread", "x", "Select"),
    ActionSpec("bulk_actions", "X", "Bulk"),
    # Snooze the highlighted thread ('z'): out of the Inbox now, back later.
    # Mail-tab-gated like the others.
    ActionSpec("snooze", "z", "Snooze"),
    ActionSpec("focus_label_select", "l", "Labels"),
    ActionSpec("focus_search", "/", "Search"),
    ActionSpec("context_space", "space", "Context"),
    ActionSpec("browser_home", "h", "Home"),
    ActionSpec("browser_show_bookmarks", "b", "Bookmarks"),
    ActionSpec("browser_bookmark_page", "ctrl+b", "Bookmark Page"),
    ActionSpec("browser_cycle_sort", "s", "Sort"),
    ActionSpec("browser_delete_bookmark", "delete", "Delete Bookmark"),
    ActionSpec("cal_prev", "[", "Prev"),
    ActionSpec("cal_next", "]", "Next"),
    ActionSpec("new_event", "n", "New Event"),
    # Single binding shared by Drive and Mail (Email preview pane) -- same
    # dual-context-single-action pattern as "n"/new_event (Calendar tab vs
    # Mail's Events pane): action_toggle_preview branches on the active tab.
    ActionSpec("toggle_preview", "p", "Toggle Preview"),
    ActionSpec("download_drive_file", "d", "Download"),
    ActionSpec("refresh", "ctrl+r", "Refresh"),
    # Reverses the most recent trash/archive within a short window (see
    # GoogleTUI._UNDO_WINDOW_SECONDS). Ctrl+Z is ASCII SUB — delivered as a
    # key in Textual's raw-mode terminal (no SIGTSTP suspend), same as the
    # other Ctrl+<letter> bindings here. Shown in the Mail context help row,
    # not the global one, since trash/archive are Mail-only.
    ActionSpec("undo", "ctrl+z", "Undo"),
    ActionSpec("help", "ctrl+h", "Help"),
    ActionSpec("toggle_mouse", "f12", "Mouse"),
    ActionSpec("quit", "ctrl+q", "Quit"),
]

# ThreadModal is a ModalScreen (`is_modal = True`), which truncates
# Textual's binding-chain walk at the modal boundary — so the app-level
# r/a/f bindings above never reach it while it's open. These give it its
# own real bindings instead of relying on dead keys.
THREAD_MODAL_ACTIONS: list[ActionSpec] = [
    ActionSpec("reply", "r", "Reply", scope="modal:ThreadModal"),
    ActionSpec("reply_all", "a", "Reply All", scope="modal:ThreadModal"),
    ActionSpec("forward", "f", "Forward", scope="modal:ThreadModal"),
    ActionSpec("trash", "d", "Trash", scope="modal:ThreadModal"),
    ActionSpec("archive", "s", "Archive", scope="modal:ThreadModal"),
    ActionSpec("labels", "l", "Labels", scope="modal:ThreadModal"),
    ActionSpec("email_to_task", "t", "To Task", scope="modal:ThreadModal"),
    ActionSpec("email_to_event", "e", "To Event", scope="modal:ThreadModal"),
    ActionSpec("attachments", "g", "Attachments", scope="modal:ThreadModal"),
    ActionSpec("prev_message", "left", "Prev", scope="modal:ThreadModal"),
    ActionSpec("next_message", "right", "Next", scope="modal:ThreadModal"),
    ActionSpec("focus_search", "slash", "Search", scope="modal:ThreadModal"),
]

# Not wired into Textual's BINDINGS (ComposeModal handles it via a raw
# on_key check, kept as-is). Recorded here only so the registry is the one
# place that "knows" this key exists. Deliberately hidden from every UI
# surface (help bar / HelpModal): a visible "(Ctrl+Enter)" hint shipped
# 2026-07-14 and was reverted the same day after live testing showed many
# terminals don't transmit Ctrl+Enter distinctly from Enter — see
# CHANGELOG.md. Don't re-add visibility here.
COMPOSE_MODAL_ACTIONS: list[ActionSpec] = [
    ActionSpec("ctrl_enter_send", "ctrl+enter", "Send", scope="modal:ComposeModal",
               show_in_help=False),
]

ACTIONS: dict[tuple[str, str], ActionSpec] = {
    (spec.scope, spec.id): spec
    for spec in [*GLOBAL_ACTIONS, *THREAD_MODAL_ACTIONS, *COMPOSE_MODAL_ACTIONS]
}


def bindings_for_scope(scope: str) -> list[Binding]:
    """Build a Textual BINDINGS list for one scope, in declaration order."""
    return [
        Binding(spec.keys, spec.id, spec.label, show=spec.show_in_help,
                priority=spec.priority)
        for (sc, _id), spec in ACTIONS.items()
        if sc == scope and spec.bindable and spec.keys
    ]


# Settings -> General -> "ASCII-safe mode" swaps these for the plain-ASCII
# equivalents on the right, applied by ``ascii_safe()`` below. Kept as a
# find/replace over the normal (Unicode) strings below rather than a second
# hand-maintained copy of every help string, so there's exactly one place to
# edit when help text changes.
_ARROW_ASCII = {"←": "<-", "→": "->", "↑": "^", "↓": "v"}


def ascii_safe(text: str) -> str:
    """Swap arrow glyphs for ASCII-safe equivalents. Callers (help bar,
    HelpModal) decide whether to apply this based on ``Settings.ascii_mode``
    — this module stays a dumb string transform, no Settings import here.
    """
    for glyph, repl in _ARROW_ASCII.items():
        text = text.replace(glyph, repl)
    return text


HELP_GLOBAL_TEXT = (
    "F1-F8,Ctrl+9 Tab   Alt+# Pane   Alt+←→↑↓ Move Pane   Ctrl+K Ask   "
    "Ctrl+P Commands   F12 Mouse   Ctrl+H Help   Ctrl+Q Quit"
)

# Keyed "pane:<id>" for the Dashboard tab's cards, "tab:<id>" for every
# other tab (including tab-mail, which is Email-only -- see
# GoogleTUI._context_help_scope). The Dashboard became the real Google-native
# dashboard 2026-07-17 (a TODAY/TASKS/MAIL/NEWS card grid + Hermes) --
# pane:events (now "TODAY") and pane:tasks keep their prior text since those
# cards' interactions didn't change; pane:dash-mail/dash-news are from
# 2026-07-17, pane:dash-weather/dash-stocks/dash-word/dash-potd from
# 2026-07-19 (ROADMAP P4's external cards).
CONTEXT_HELP: dict[str, str] = {
    "tab:tab-mail": "Enter Open   c Compose   r Reply   a Reply All   f Forward   u Unread   * Star   z Snooze   t Task   e Event   x Select   X Bulk   Space Expand   l Labels   / Search   p Toggle Preview   Ctrl+Z Undo",
    "pane:events": "Enter/Space Detail   n New Event   / Search",
    "pane:tasks": "Space Toggle Complete   Enter Detail   / Search",
    "pane:dash-mail": "Enter/Space Open Thread (header: open Mail tab)",
    "pane:dash-news": "Enter/Space Open Headline",
    "pane:dash-weather": "Configure in Settings -> Dashboard",
    "pane:dash-stocks": "Configure in Settings -> Dashboard",
    "pane:dash-word": "Enter Open Full Entry",
    "pane:dash-potd": "Enter Open Image Page",
    "pane:hermes": "Enter Ask",
    "tab:tab-calendar": "[ / ] Prev/Next Month or Week   Enter Day Detail   n New Event",
    "tab:tab-drive": "Enter Open Folder / Reload Preview   / Search (this folder)   p Toggle Preview   d Download",
    # "S Sort: <mode>" is appended live by GoogleTUI._context_help_text (the
    # current bookmark sort mode isn't static text -- see that method).
    "tab:tab-browser": "Enter Load/Search   Alt+←/→ Back/Forward   H Home   B Bookmarks   Ctrl+B Bookmark Page   "
                        "Delete Remove Bookmark   Tab Toggle Focus   0-9+Enter Link",
    "tab:tab-news": "Enter/Space Open Entry   / Search",
    "tab:tab-navigation": "Enter/Go Compute Route   Export Save Itinerary To File",
    "tab:tab-settings": "Alt+←/→ Switch Section   Toggle encryption   Choose key method   Clear local cache   "
                         "Browser home/start page   Manage feeds   Search provider   Routes API key   "
                         "Enable/disable Dashboard cards",
    "tab:tab-contacts": "Type to search (or / from elsewhere in the tab)   Enter/Space Detail (compose to contact)   Refresh",
    # ThreadModal's own contextual help row (P2 2026-07-15). Rendered as a
    # clickable help bar via help_markup() below — each "Key Label" pair
    # becomes a Textual @click action link so a mouse user can trigger the
    # action; every other pane/tab's row gets the same treatment (see
    # _CLICK_ACTIONS). The plain-text form here is the fallback (and what
    # non-clickable renders / HelpModal would show).
    "modal:ThreadModal": ("←/→ Prev/Next   R Reply   A Reply All   F Forward   "
                          "D Trash   S Archive   L Labels   T Task   E Event   "
                          "G Attachments   / Search   Esc Close"),
}

# Maps each CONTEXT_HELP scope's "Key Label" spans to the action they should
# invoke when clicked — generalizes what was originally a ThreadModal-only
# affordance (every other section used to show shortcut keys as inert text)
# to every pane/tab's context help row, so the whole app follows one scheme
# instead of ThreadModal alone having clickable shortcuts AND a redundant row
# of full-size buttons repeating the same commands (removed — see CHANGELOG).
# Keyed by the exact substring as it appears in CONTEXT_HELP[scope]. Spans
# left out have no single zero-argument action to click: some depend on
# which list item is highlighted (a bare "Enter" selection), others bundle
# two distinct keys into one reading unit a single click can't disambiguate
# ("[ / ] Prev/Next", "Alt+←/→ Back/Forward", ThreadModal's own "←/→
# Prev/Next"). "Esc Close" is the one exception: THREAD_MODAL_ACTIONS has no
# ActionSpec for it (Esc is handled ad hoc in ThreadModal.on_key), but
# ThreadModal.action_close exists precisely so this span can be clickable —
# it's the mouse user's only way to close the modal now that the button row
# is gone.
_CLICK_ACTIONS: dict[str, dict[str, str]] = {
    "tab:tab-mail": {
        "c Compose": "compose_new",
        "r Reply": "reply",
        "a Reply All": "reply_all",
        "f Forward": "forward",
        "u Unread": "mark_unread",
        "* Star": "star",
        "z Snooze": "snooze",
        "t Task": "email_to_task",
        "e Event": "email_to_event",
        "x Select": "select_thread",
        "X Bulk": "bulk_actions",
        "Space Expand": "context_space",
        "l Labels": "focus_label_select",
        "/ Search": "focus_search",
    },
    "pane:events": {
        "n New Event": "new_event",
        "/ Search": "focus_search",
    },
    "pane:tasks": {
        "Space Toggle Complete": "context_space",
        "/ Search": "focus_search",
    },
    "tab:tab-calendar": {
        "n New Event": "new_event",
    },
    "tab:tab-browser": {
        "H Home": "browser_home",
        "B Bookmarks": "browser_show_bookmarks",
        "Ctrl+B Bookmark Page": "browser_bookmark_page",
        "Delete Remove Bookmark": "browser_delete_bookmark",
        # Matches only the fixed "S Sort" prefix -- the dynamic ": <mode>"
        # suffix _context_help_text appends stays outside the click span,
        # which is fine, .replace() still finds "S Sort" as a substring.
        "S Sort": "browser_cycle_sort",
    },
    "tab:tab-news": {
        "/ Search": "focus_search",
    },
    "modal:ThreadModal": {
        "R Reply": "reply",
        "A Reply All": "reply_all",
        "F Forward": "forward",
        "D Trash": "trash",
        "S Archive": "archive",
        "L Labels": "labels",
        "T Task": "email_to_task",
        "E Event": "email_to_event",
        "G Attachments": "attachments",
        "Esc Close": "close",
    },
}


def _click_target(scope: str) -> str:
    """modal:* scopes' actions live on the active ModalScreen — GoogleTUI
    also has action_reply/reply_all/forward, but those act on the Email
    list, so a modal's own "Reply" must route to ``screen.``, not ``app.``,
    or it'd fire the wrong handler. Every other scope's actions are plain
    App-level actions.
    """
    return "screen" if scope.startswith("modal:") else "app"


def apply_click_actions(text: str, scope: str) -> str:
    """Wrap `scope`'s clickable "Key Label" spans (see _CLICK_ACTIONS) in
    `text` with a Textual [@click=...] action link. A span not found verbatim
    in `text` is silently left alone — callers that line-wrap long help text
    apply this per already-wrapped line, so a span split across a wrap
    boundary just stays plain text on that occasion rather than erroring.
    """
    target = _click_target(scope)
    for span, action in _CLICK_ACTIONS.get(scope, {}).items():
        text = text.replace(span, f"[@click={target}.{action}]{span}[/]")
    return text


def help_markup(scope: str, ascii_mode: bool = False) -> str:
    """Render a CONTEXT_HELP entry as a Textual-markup string whose
    actionable "Key Label" spans are clickable [@click=...] action links.

    Returned string is meant for a ``Static``/``Label`` with markup enabled
    (the default). Spans with no mapped action stay plain text. Applies the
    same arrow-glyph ASCII fallback the rest of the help bar uses when
    ``ascii_mode`` is set.
    """
    text = CONTEXT_HELP.get(scope, "")
    if ascii_mode:
        text = ascii_safe(text)
    return apply_click_actions(text, scope)

# Transcribed verbatim from the former module-level HELP_TEXT constant.
HELP_TEXT = """\
GLOBAL
  F1..F8           Switch tab (Dashboard / Mail / Calendar / Drive / Browser /
                   News / Navigation / Contacts) — also works as Ctrl+1..8,
                   kept as a secondary alias for terminals where F-keys are
                   intercepted (e.g. a window manager's fullscreen bindings).
                   Settings is the 9th tab, Ctrl+9 only — no F-key alias, since
                   F9+ isn't reliably delivered by every terminal.
  Ctrl+Left/Right  Cycle tabs (the universal fallback if neither F1..F8 nor
                   Ctrl+1..8 reaches the app — some terminals/multiplexers/
                   browsers swallow both)
  Alt+1..4         Jump to a pane: 1 Email (Mail tab), 2/3/4 Today/Tasks/
                   Hermes (Dashboard tab). The Dashboard's Mail and News
                   cards have no digit — reach them with Tab or Alt+arrows.
  Alt+arrows       Move to the adjacent Dashboard card (2x2 grid + Hermes;
                   skips over any card disabled in Settings → Dashboard)
  Tab / Shift+Tab  Cycle Dashboard cards (enabled ones only)
  Ctrl+K           Quick-ask the configured AI assistant (Settings → AI
                   Provider) from anywhere — pops up a small modal, Esc closes.
                   Doesn't share history with the Dashboard's Hermes card.
  Ctrl+R           Reconnect / refresh live data
  Ctrl+P           Command palette
  Ctrl+H           This help
  F12              Release/recapture the mouse. While the app holds the mouse
                   your terminal can't draw its own selection, so you can't
                   drag-copy text (a URL, say) the way you normally would.
                   F12 hands the mouse back; F12 again takes it. You can also
                   drag-select inside the app and press Ctrl+C, which copies
                   over SSH via OSC 52 where the terminal allows it.
  Ctrl+Q           Quit

MAIL TAB
  Email-only: Enter open thread, Space expand/collapse (shows snippet),
  l show labels filter (Esc hides), c Compose new, r Reply, a Reply All,
  f Forward, u mark unread, * star/unstar (★ column), z snooze (out of the
  Inbox now, back at a chosen time — checked each refresh, resurfaces on the
  next launch if it came due while closed), t turn the thread into a Google
  Task, e turn it into a Calendar event (both prefill from the subject + a
  link back to the thread), x check/uncheck for a bulk action, X open the
  bulk-action chooser (Archive / Trash / Apply Label over every checked
  thread), / search (live filter over subject/from/snippet).
  p toggles a preview pane on the right showing the highlighted thread's
  latest message (hidden by default — Settings → General to change the
  default) — while visible, it live-updates as you move the highlight,
  Outlook-reading-pane style; press p again to hide it. Alt+Right moves
  focus into the preview pane (Alt+Left back to the thread list) once it's
  visible.

  Thread view (opened via Enter): R/A/F Reply / Reply All / Forward — same
  keys as the Email pane, now with visible button hints — D Trash, S Archive,
  L Labels, G attachments (view/download the thread's attachments to
  Documents/google-tui/), Esc/Close closes. Any attachments are also listed
  under each message's From/Date header. After a Trash or Archive, Ctrl+Z
  (back in the list) undoes it within a minute — restores from Trash / moves
  back to Inbox.

  Compose (r/a/f/c): To / Cc / Bcc / Subject fields — reply-all pre-fills Cc
  from the thread. The "Attach a file" row adds a local file by path (Enter
  or the Attach button; add several); attachments ride along on Send and
  Save Draft. "Send" (5s undo countdown) or "Save Draft" to file it in Gmail
  Drafts instead. Both queue offline and replay on reconnect.

DASHBOARD TAB (card grid + Hermes; Google-native cards 2026-07-17, external
cards 2026-07-19. All cards are enabled by default; Settings → Dashboard
lets you enable/disable any card and configure Weather/Stocks — that
checklist is a "library" future cards can still grow into.)
  Today card:   today's events (all-day + timed); Enter/Space open detail,
                n new event, / search (live filter over summary/description)
  Tasks card:   tasks grouped Overdue / Due today / Upcoming / No due date /
                Done; Space toggle complete, Enter open detail, / search
  Mail card:    unread count + most-recent unread threads; Enter/Space opens
                the thread (or, on the count header, jumps to the Mail tab)
  News card:    top headlines from your subscribed feeds, rotating; Enter/
                Space opens the entry
  Weather card: current conditions (Open-Meteo, no API key) for the location
                set in Settings → Dashboard, defaulting to one guessed from
                your IP (or Portland, ME if that fails) when unset
  Stocks card:  latest quotes (Stooq, no API key) for the symbols set in
                Settings → Dashboard, defaulting to GOOG/MSFT/AAPL; clear
                the symbol list there to turn the card's fetch off
  Word of the day card:
                today's word + definition (Merriam-Webster); Enter opens the
                full dictionary entry in the Browser tab
  Picture of the day card:
                today's featured Wikipedia image's caption (no in-terminal
                image rendering yet); Enter opens the image page in the
                Browser tab
  Hermes card:  type a question, Enter to ask. Its title always shows the
                currently configured AI provider (Settings → AI Provider),
                e.g. "CLAUDE CODE ASK". Also reachable as a Ctrl+K popup
                from anywhere — see GLOBAL above.

CALENDAR TAB
  [ / ]         Previous / next month (or week, in Week view)
  Enter/click   Open a day's full event list (Month view)
                Open an event, or a chooser if several share an hour (Week view)
  n             New event (also works from the Dashboard tab's Events pane) —
                title + date + start/end time, or an all-day toggle

DRIVE TAB
  Source picker  A Select at the top of the tab: "Google Drive" plus any
                 saved FTP/SSH (SFTP, falling back to legacy SCP if a
                 server disables the SFTP subsystem) hosts, plus "+ Add
                 remote host…" to connect a new one. Switching sources
                 reloads the file list/preview/download against whichever
                 backend is selected — everything below works the same
                 regardless of source.
  Up/Down       Move selection — preview pane updates live
  Enter/click   Open a folder, or re-load a file's preview
  / search      Live filter by name over the CURRENT folder's file list
                (not the whole tree)
  p             Toggle the preview/info column — hide it to give the file
                list the full width
  d             Download the selected file to Documents/google-tui/ (a
                Google-native Doc/Sheet/Slide/Drawing is exported first,
                same conversion the text preview uses); no-ops on folders
                or offline (Google source only — FTP/SSH connect live
                regardless of Google's reachability)

BROWSER TAB
  Enter (address bar)    Load URL, or run a search (bare text w/ no scheme searches)
  Bookmarks list         A folder-nested list (arrow/Home/End/PageUp/Down +
                         Enter to descend, "up" row to go back) of saved
                         destinations across web/Gopher/Gemini/FTP/SSH,
                         color-coded by protocol. Focused automatically
                         whenever it's the visible view. Shown on first
                         Browser-tab visit if Settings -> Browser's start
                         page is "Bookmarks" (the default); B re-shows it
                         any time; Ctrl+B saves the current page into it;
                         Delete removes the highlighted bookmark or folder
                         (with its contents) — no confirmation prompt, same
                         as removing a News feed subscription.
  S                      Cycle the bookmarks list's sort order: Name ->
                         Added -> Used -> Name. Shown live in this tab's
                         shortcut bar.
  Alt+Left / Alt+Right   Back / forward through this session's history
  H                      Go to your configured home page (Settings -> Browser)
  Tab                    Toggle focus: address bar <-> page content
  0-9 then Enter (page)  Jump to numbered link
  Esc (page)             Cancel a pending number entry
  ftp:// / sftp:// URL   Switches to the Drive tab instead of loading inline
                         — remote-filesystem browsing (FTP/SSH) lives there
                         now, with a proper file list/preview/download, not
                         as a Browser page. Prompts to connect (and
                         optionally save) the host if it isn't saved yet.

NEWS TAB
  Enter/Space   Open the selected entry (rendered via the shared Document view)
  / search      Live filter by title/summary over the combined entry list
  Entries from every subscribed feed are combined, newest first. Manage
  subscriptions (add/remove feed URLs) from the Settings tab.

NAVIGATION TAB
  Origin/Destination inputs, then Enter or the Go button, compute a driving
  route via the Google Routes API (free-text addresses — no need for exact
  coordinates). Shows total distance/duration plus a turn-by-turn step list.
  Export     Save the current itinerary to a text file (Documents/google-tui)
  Needs a Routes API key, set in Settings -> Navigation.

SETTINGS TAB
  Sub-tabs      General / Browser / AI Provider / News Feeds / Search /
                Navigation / Dashboard — Alt+Left/Right cycles between them
                while the Settings tab is active
  Switch        Show the Mail tab's preview pane by default at launch —
                "p" still toggles it per-session either way (General)
  Switch        Toggle encrypt-at-rest for the local cache (General)
  RadioSet      Choose passphrase-at-launch vs. local key file (General)
  Button        Clear the local cache immediately (General)
  Input+Button  Set/save the Browser tab's home page ("H") (Browser)
  Select        Choose what the Browser tab shows on first visit each
                session: Bookmarks (default) or Home (Browser)
  DataTable+Button  Saved remote hosts (FTP/SSH, added from the Drive tab's
                    source picker) — remove one (Browser)
  RadioSet      Choose AI provider for the Hermes Ask pane (AI Provider)
  Input+Button  Set/save the Nous API key (AI Provider)
  Input+Button  Add a News-tab feed subscription (URL) (News Feeds)
  Button        Remove the selected feed subscription (News Feeds)
  Button        Browse popular feeds — categorized checklist (general/world/
                local/tech/cybersecurity/ham radio/electronics/sports news),
                check to subscribe, uncheck to unsubscribe (News Feeds)
  RadioSet      Choose the Browser tab's search provider: Google /
                DuckDuckGo / SearXNG (Search)
  Input+Button  Set Google Custom Search API key + Search Engine ID, or a
                SearXNG instance URL, then save (Search)
  Input+Button  Set/save the Routes API key used by the Navigation tab
                (Navigation)
  Input+Button  Set/save the Weather card's location (blank = auto-detect
                via GeoIP, or Portland, ME) and the Stocks card's ticker
                list (default GOOG/MSFT/AAPL; blank disables) (Dashboard)
  Checklist     Enable/disable Dashboard cards (Today/Tasks/Mail/News/
                Weather/Stocks/Word of the Day/Picture of the Day/Hermes) —
                all enabled by default, at least one stays enabled (Dashboard)

CONTACTS TAB
  Type to search    Live fuzzy filter (name or email) over your fetched
                    Google Contacts — no re-query as you type. Auto-focused
                    on tab activation; press / from anywhere in this tab
                    (e.g. after moving focus to the contact list) to jump
                    back to it.
  Enter/Space       Open the highlighted contact's detail (name/email/phone),
                    with a "Compose Email" button to start a new message to them
  Refresh           Re-fetch contacts from Google now
  (Blank Compose New moved to the Email pane's "c" key)
  Contacts are fetched lazily (once, the first time you open this tab, not
  on every startup/Ctrl+R) since they change far less often than mail/
  calendar/drive. Needs the contacts.readonly scope on your Google token —
  if that's missing, this notifies an error instead of crashing (SETUP.md §7).
  ComposeModal's To field also fuzzy-suggests from these same contacts as
  you type a name.

Reply/Forward/Toggle-complete are disabled while offline (shown in the
title bar as "Offline (cached HH:MM)"); browsing cached data still works.
"""
