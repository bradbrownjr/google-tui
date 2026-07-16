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


# Transcribed verbatim from the former GoogleTUI.BINDINGS list — order
# preserved (it matters for anything that walks BINDINGS in sequence).
GLOBAL_ACTIONS: list[ActionSpec] = [
    ActionSpec("switch_left", "alt+left", "Pane Left"),
    ActionSpec("switch_right", "alt+right", "Pane Right"),
    ActionSpec("switch_up", "alt+up", "Pane Up"),
    ActionSpec("switch_down", "alt+down", "Pane Down"),
    ActionSpec("cycle", "tab", "Cycle"),
    ActionSpec("cycle_back", "shift+tab", "Cycle"),
    ActionSpec("goto_tab_mail", "f1,ctrl+1", "Mail"),
    ActionSpec("goto_tab_calendar", "f2,ctrl+2", "Calendar"),
    ActionSpec("goto_tab_drive", "f3,ctrl+3", "Drive"),
    ActionSpec("goto_tab_browser", "f4,ctrl+4", "Browser"),
    ActionSpec("goto_tab_news", "f5,ctrl+5", "News"),
    ActionSpec("goto_tab_navigation", "f6,ctrl+6", "Navigation"),
    ActionSpec("goto_tab_contacts", "f7,ctrl+7", "Contacts"),
    ActionSpec("goto_tab_settings", "f8,ctrl+8", "Settings"),
    ActionSpec("cycle_tab_back", "ctrl+left", "Prev Tab"),
    ActionSpec("cycle_tab", "ctrl+right", "Next Tab"),
    ActionSpec("goto_pane_email", "alt+1", "Email"),
    ActionSpec("goto_pane_events", "alt+2", "Events"),
    ActionSpec("goto_pane_tasks", "alt+3", "Tasks"),
    ActionSpec("goto_pane_hermes", "alt+4", "Hermes"),
    ActionSpec("reply", "r", "Reply"),
    ActionSpec("reply_all", "a", "Reply All"),
    ActionSpec("forward", "f", "Forward"),
    ActionSpec("compose_new", "c", "Compose"),
    ActionSpec("mark_unread", "u", "Unread"),
    ActionSpec("focus_label_select", "l", "Labels"),
    ActionSpec("focus_search", "/", "Search"),
    ActionSpec("context_space", "space", "Context"),
    ActionSpec("browser_home", "alt+h", "Home"),
    ActionSpec("cal_prev", "[", "Prev"),
    ActionSpec("cal_next", "]", "Next"),
    ActionSpec("new_event", "n", "New Event"),
    ActionSpec("refresh", "ctrl+r", "Refresh"),
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
        Binding(spec.keys, spec.id, spec.label, show=spec.show_in_help)
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
    "F1-F8 Tab   Alt+# Pane   Alt+←→↑↓ Move Pane   "
    "Ctrl+P Commands   F12 Mouse   Ctrl+H Help   Ctrl+Q Quit"
)

# Keyed "pane:<id>" for Mail-tab panes, "tab:<id>" for every other tab —
# matches GoogleTUI._context_help_text's former if/elif exactly.
CONTEXT_HELP: dict[str, str] = {
    "pane:email": "Enter Open   c Compose   r Reply   a Reply All   f Forward   u Unread   Space Expand   l Labels   / Search",
    "pane:events": "Enter/Space Detail   n New Event   / Search",
    "pane:tasks": "Space Toggle Complete   Enter Detail   / Search",
    "pane:hermes": "Enter Ask",
    "tab:tab-calendar": "[ / ] Prev/Next Month or Week   Enter Day Detail   n New Event",
    "tab:tab-drive": "Enter Open Folder / Reload Preview   / Search (this folder)",
    "tab:tab-browser": "Enter Load/Search   Alt+←/→ Back/Forward   Alt+H Home   Tab Toggle Focus   0-9+Enter Link",
    "tab:tab-news": "Enter/Space Open Entry   / Search",
    "tab:tab-navigation": "Enter/Go Compute Route   Export Save Itinerary To File",
    "tab:tab-settings": "Alt+←/→ Switch Section   Toggle encryption   Choose key method   Clear local cache   "
                         "Manage feeds   Search provider   Routes API key",
    "tab:tab-contacts": "Type to search (or / from elsewhere in the tab)   Enter/Space Detail (compose to contact)   Refresh",
    # ThreadModal's own contextual help row (P2 2026-07-15). Rendered as a
    # clickable help bar via help_markup() below — each "Key Label" pair
    # becomes a Textual @click action link so a mouse user can trigger the
    # action; every other pane/tab's row gets the same treatment (see
    # _CLICK_ACTIONS). The plain-text form here is the fallback (and what
    # non-clickable renders / HelpModal would show).
    "modal:ThreadModal": ("←/→ Prev/Next   R Reply   A Reply All   F Forward   "
                          "D Trash   S Archive   L Labels   / Search   Esc Close"),
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
    "pane:email": {
        "c Compose": "compose_new",
        "r Reply": "reply",
        "a Reply All": "reply_all",
        "f Forward": "forward",
        "u Unread": "mark_unread",
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
        "Alt+H Home": "browser_home",
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
  F1..F8           Switch tab (Mail / Calendar / Drive / Browser / News / Navigation / Settings / Contacts) —
                   also works as Ctrl+1..8, kept as a secondary alias for
                   terminals where F-keys are intercepted (e.g. a window
                   manager's fullscreen bindings)
  Ctrl+Left/Right  Cycle tabs (the universal fallback if neither F1..F8 nor
                   Ctrl+1..8 reaches the app — some terminals/multiplexers/
                   browsers swallow both)
  Alt+1..4         Jump to Mail pane (Email / Events / Tasks / Hermes)
  Alt+arrows       Move to the adjacent Mail pane
  Tab / Shift+Tab  Cycle Mail panes
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
  Email pane:   Enter open thread, Space expand/collapse (shows snippet),
                l show labels filter (Esc hides), c Compose new, r Reply,
                a Reply All, f Forward, / search (live filter over
                subject/from/snippet)
  Events pane:  Enter/Space open event detail, n new event, / search (live
                filter over summary/description)
  Tasks pane:   Space toggle complete, Enter open detail, / search (live
                filter over title/notes)
  Hermes pane:  type a question, Enter to ask

  Thread view (opened via Enter): R/A/F Reply / Reply All / Forward — same
  keys as the Email pane, now with visible button hints — Esc/Close closes.

CALENDAR TAB
  [ / ]         Previous / next month (or week, in Week view)
  Enter/click   Open a day's full event list (Month view)
                Open an event, or a chooser if several share an hour (Week view)
  n             New event (also works from the Mail tab's Events pane) —
                title + date + start/end time, or an all-day toggle

DRIVE TAB
  Up/Down       Move selection — preview pane updates live
  Enter/click   Open a folder, or re-load a file's preview
  / search      Live filter by name over the CURRENT folder's file list
                (not the whole Drive tree)

BROWSER TAB
  Enter (address bar)    Load URL, or run a search (bare text w/ no scheme searches)
  Bookmark buttons       Starter destinations (Google/Wikipedia/Gopherpedia/
                         Gemini Protocol) shown until you navigate anywhere,
                         then hidden for the rest of the session
  Alt+Left / Alt+Right   Back / forward through this session's history
  Alt+H                  Go to your configured home page (Settings -> General)
  Tab                    Toggle focus: address bar <-> page content
  0-9 then Enter (page)  Jump to numbered link
  Esc (page)             Cancel a pending number entry

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
  Sub-tabs      General / AI Provider / News Feeds / Search / Navigation —
                Alt+Left/Right cycles between them while the Settings tab
                is active
  Switch        Toggle encrypt-at-rest for the local cache (General)
  RadioSet      Choose passphrase-at-launch vs. local key file (General)
  Button        Clear the local cache immediately (General)
  RadioSet      Choose AI provider for the Hermes Ask pane (AI Provider)
  Input+Button  Set/save the Nous API key (AI Provider)
  Input+Button  Add a News-tab feed subscription (URL) (News Feeds)
  Button        Remove the selected feed subscription (News Feeds)
  RadioSet      Choose the Browser tab's search provider: Google /
                DuckDuckGo / SearXNG (Search)
  Input+Button  Set Google Custom Search API key + Search Engine ID, or a
                SearXNG instance URL, then save (Search)
  Input+Button  Set/save the Routes API key used by the Navigation tab
                (Navigation)

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
