"""User preferences (plaintext). Must stay plaintext: we need to know the
encryption key method BEFORE we can derive or verify any key.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import platformdirs

SETTINGS_PATH = Path(platformdirs.user_config_dir("google-tui")) / "settings.json"


@dataclass
class Settings:
    encrypt_at_rest: bool = False
    key_method: str = "keyfile"  # "keyfile" | "passphrase"
    kdf_salt: str | None = None  # base64, passphrase mode only
    canary: str | None = None  # base64 Fernet token of a known string, passphrase mode only
    default_label_id: str = "INBOX"  # Gmail label id shown in the Email pane on launch
    default_start_tab: str = "tab-dashboard"  # main-tabs id shown on launch (Settings -> General); GoogleTUI falls back to "tab-dashboard" if this holds a stale/unknown id
    show_sender_address: bool = False  # show raw "Name <addr>" in the Email list vs. name-only
    ai_provider: str = "hermes"  # "hermes" | "claude_code" | "opencode" | "gemini_cli"
    nous_api_key: str | None = None  # overrides ~/.hermes/config.yaml if set
    feed_urls: list[str] = field(default_factory=list)  # subscribed RSS/Atom feeds (News tab, P1 M3)
    search_provider: str = "google"  # "google" | "duckduckgo" | "searxng" (Browser tab Search mode)
    google_cse_api_key: str | None = None  # Google Custom Search JSON API key
    google_cse_id: str | None = None  # Programmable Search Engine ID ("cx")
    searxng_url: str | None = None  # base URL of a SearXNG instance, e.g. https://searx.example.org
    routes_api_key: str | None = None  # Google Routes API key (Navigation tab, M6)
    browser_home_url: str = "https://www.google.com"  # Browser tab's H home destination
    # Browser tab bookmarks: a list of {"type": "bookmark", "label", "url"} or
    # {"type": "folder", "label", "children": [...]} dicts, editable via the
    # Browser tab's bookmarks table ("B" to show, Ctrl+B to add the current
    # page, Delete to remove the highlighted one). Default matches the app's
    # original hardcoded starter list. Each dict may also carry "added_at"/
    # "last_opened_at" ISO-8601 UTC timestamps (stamped by main.py on create/
    # open, not present here) -- missing on a legacy entry is fine, sort code
    # treats that as "" (oldest/never-used), never a crash.
    browser_bookmarks: list[dict] = field(default_factory=lambda: [
        {"type": "bookmark", "label": "Google", "url": "https://www.google.com"},
        {"type": "bookmark", "label": "Wikipedia", "url": "https://en.wikipedia.org"},
        {"type": "bookmark", "label": "Gopherpedia", "url": "gopher://gopher.floodgap.com"},
        {"type": "bookmark", "label": "Gemini Protocol", "url": "gemini://geminiprotocol.net/"},
    ])
    # "bookmarks" | "home" -- what the Browser tab shows the first time it's
    # activated each session. Defaults to "bookmarks" to match the app's
    # original (pre-this-setting) behavior.
    browser_start_page: str = "bookmarks"
    # "name" | "added" | "used" -- Browser tab bookmarks list order, cycled by
    # "S" (shown live in that tab's shortcut bar).
    browser_bookmark_sort: str = "name"
    # Saved remote-host (FTP/SSH) credentials are NOT stored here -- see
    # remote_creds.py's module docstring for why (Settings is plaintext;
    # credentials need the same optional encryption the local cache uses,
    # in their own file so Settings' "Clear Cache" button can't wipe them).
    check_for_updates: bool = True  # fast-forward the git checkout on launch (see updater.py)
    ascii_mode: bool = False  # ASCII-safe rendering (plain borders/digits/arrows/punctuation) for terminals that mangle Unicode
    # Cache limits (Outlook-style). Both are opt-in; 0 == no limit. Enforced on
    # launch and on demand from Settings -> General; see Cache.prune(). Nothing
    # in the cache is irreplaceable — everything is refetchable and revalidated
    # — so eviction costs a little latency, never data.
    cache_retention_days: int = 0  # drop cached items not seen in this many days
    cache_max_mb: int = 0  # evict least-recently-seen items to stay under this
    email_preview_default_visible: bool = False  # Mail tab's "p"-toggled preview pane, on launch
    quote_on_reply: bool = True  # prepend a "> "-quoted prior message to reply/reply-all compose bodies (Gmail's web client does this by default too)
    # Dashboard tab card library (2026-07-18, Settings -> Dashboard): which of
    # the Dashboard's cards are enabled. Default is every currently-shipped
    # card, WEATHER/STOCKS/WORD OF THE DAY/PICTURE OF THE DAY included as of
    # 2026-07-22 (they now have sensible zero-config defaults below, so an
    # empty-state row on a fresh install would just be needless friction).
    # Ids match main.py's DASH_PANE_IDS -- a stale id here (from a
    # since-removed card) is filtered out defensively by GoogleTUI.
    # _apply_dashboard_panes_enabled, not here, since settings.py doesn't
    # know about main.py's card registry. Toggle any card off from
    # Settings -> Dashboard.
    dashboard_panes_enabled: list[str] = field(
        default_factory=lambda: ["dash-mail", "dash-news", "dash-word", "dash-potd",
                                  "dash-clock", "dash-calendar", "dash-today", "tasks",
                                  "dash-weather", "dash-stocks", "hermes"])
    # Dashboard "external cards" config (ROADMAP P4, 2026-07-19). Left unset
    # on purpose: an unset weather_location means "auto" -- GoogleTUI.
    # _resolve_weather_location guesses a location from the caller's IP
    # (GeoIP), falling back to "Portland, ME" if that lookup fails or is
    # unavailable -- so the WEATHER card shows real conditions out of the
    # box. Set a location below (Settings -> Dashboard) to override the
    # guess. stock_symbols defaults to a well-known trio below rather than
    # empty for the same "useful out of the box" reason; clear the Input
    # there to turn the STOCKS card's fetch off entirely.
    weather_location: str | None = None  # free-text, e.g. "Seattle, WA" (Open-Meteo geocodes it); None = auto (GeoIP or Portland, ME)
    stock_symbols: list[str] = field(default_factory=lambda: ["GOOG", "MSFT", "AAPL"])  # e.g. ["AAPL", "MSFT"]; empty disables the card
    # CLOCK card (2026-07-23): big block-digit local time (Textual's built-in
    # Digits widget, no external dependency) plus one plain-text line per
    # entry here -- each an IANA zone name (or "UTC") shown as "HH:MM:SS
    # <zone>" below the big local time. Invalid/unrecognized zone names are
    # just skipped (GoogleTUI._update_dash_clock), never a crash. Defaults to
    # UTC alone, the common ham-radio "local + Zulu" pairing; clear the list
    # (Settings -> Dashboard) to show local time only.
    clock_timezones: list[str] = field(default_factory=lambda: ["UTC"])
    # Snoozed threads (ROADMAP P2): {thread_id: remind-at ISO datetime}. Gmail
    # has no native snooze, so the app removes INBOX now and re-adds it when
    # the time passes (checked each online refresh — see
    # GoogleTUI._resurface_due_snoozes). Persisted here so a snooze survives
    # a restart and resurfaces on the next launch if it came due while closed.
    snoozed: dict = field(default_factory=dict)


# Pre-2026-07-22 defaults for the two fields load_settings migrates below.
# save_settings always writes every field (asdict), so an existing install's
# settings.json already has these two keys pinned at their OLD default --
# the new dataclass defaults above never take effect for it, only for a
# settings.json that doesn't exist yet. See load_settings' migration.
_LEGACY_DASHBOARD_PANES_DEFAULT = ["events", "tasks", "dash-mail", "dash-news", "hermes"]


def load_settings() -> Settings:
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return Settings()
    settings = Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})
    # One-time migration (2026-07-22): move installs still sitting on the old
    # "external Dashboard cards are opt-in, stock_symbols always starts
    # empty" defaults onto the new "everything enabled, useful out of the
    # box" ones -- only when the saved value still matches the OLD default
    # exactly, so any deliberate customization (a trimmed pane list, a
    # symbol list cleared on purpose) is left untouched.
    if data.get("dashboard_panes_enabled") == _LEGACY_DASHBOARD_PANES_DEFAULT:
        settings.dashboard_panes_enabled = Settings.__dataclass_fields__["dashboard_panes_enabled"].default_factory()
    if data.get("stock_symbols") == []:
        settings.stock_symbols = Settings.__dataclass_fields__["stock_symbols"].default_factory()
    # One-time migrations (2026-07-23, same day, two iterations of the same
    # card): the standalone "events"/TODAY card was briefly folded into a
    # single "dash-time" card (clock + mini calendar + today's events), then
    # immediately split back into three separate cards ("dash-clock",
    # "dash-calendar", "dash-today" -- navigating a compact combined
    # calendar turned out to be awkward). Both migrations always apply (a
    # straight id rename/expansion, not a "restore some default" heuristic),
    # so anyone who had the old id enabled/disabled keeps that same choice
    # for its successor(s), and both can fire in sequence for an install that
    # never got a chance to load in between (events -> dash-time -> the three
    # new ids).
    if "events" in settings.dashboard_panes_enabled and "dash-time" not in settings.dashboard_panes_enabled:
        settings.dashboard_panes_enabled = [
            "dash-time" if p == "events" else p for p in settings.dashboard_panes_enabled]
    if "dash-time" in settings.dashboard_panes_enabled and "dash-clock" not in settings.dashboard_panes_enabled:
        expanded = []
        for p in settings.dashboard_panes_enabled:
            if p == "dash-time":
                expanded.extend(["dash-clock", "dash-calendar", "dash-today"])
            else:
                expanded.append(p)
        settings.dashboard_panes_enabled = expanded
    if "clock_show_utc" in data and "clock_timezones" not in data:
        settings.clock_timezones = ["UTC"] if data["clock_show_utc"] else []
    return settings


def save_settings(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2))
