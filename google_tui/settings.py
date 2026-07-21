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
    # Browser tab's bookmarks ListView ("B" to show, Ctrl+B to add the current
    # page). Default matches the app's original hardcoded starter list.
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
    # card (matches pre-toggle behavior). Ids match main.py's DASH_PANE_IDS --
    # a stale id here (from a since-removed card) is filtered out defensively
    # by GoogleTUI._apply_dashboard_panes_enabled, not here, since settings.py
    # doesn't know about main.py's card registry.
    dashboard_panes_enabled: list[str] = field(
        default_factory=lambda: ["events", "tasks", "dash-mail", "dash-news", "hermes"])
    # Dashboard "external cards" config (ROADMAP P4, 2026-07-19). Both start
    # unset/empty on purpose: dash-weather/dash-stocks are excluded from
    # dashboard_panes_enabled's default above too, so a fresh install shows
    # neither card until the user opts in from Settings -> Dashboard (an
    # unconfigured weather/stocks card would just be an empty-state row).
    weather_location: str | None = None  # free-text, e.g. "Seattle, WA" (Open-Meteo geocodes it)
    stock_symbols: list[str] = field(default_factory=list)  # e.g. ["AAPL", "MSFT"]
    # Snoozed threads (ROADMAP P2): {thread_id: remind-at ISO datetime}. Gmail
    # has no native snooze, so the app removes INBOX now and re-adds it when
    # the time passes (checked each online refresh — see
    # GoogleTUI._resurface_due_snoozes). Persisted here so a snooze survives
    # a restart and resurfaces on the next launch if it came due while closed.
    snoozed: dict = field(default_factory=dict)


def load_settings() -> Settings:
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return Settings()
    return Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})


def save_settings(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2))
