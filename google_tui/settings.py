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
    browser_home_url: str = "https://www.google.com"  # Browser tab's Alt+H home destination
    check_for_updates: bool = True  # fast-forward the git checkout on launch (see updater.py)
    ascii_mode: bool = False  # ASCII-safe rendering (plain borders/digits/arrows/punctuation) for terminals that mangle Unicode
    # Cache limits (Outlook-style). Both are opt-in; 0 == no limit. Enforced on
    # launch and on demand from Settings -> General; see Cache.prune(). Nothing
    # in the cache is irreplaceable — everything is refetchable and revalidated
    # — so eviction costs a little latency, never data.
    cache_retention_days: int = 0  # drop cached items not seen in this many days
    cache_max_mb: int = 0  # evict least-recently-seen items to stay under this
    email_preview_default_visible: bool = False  # Mail tab's "p"-toggled preview pane, on launch


def load_settings() -> Settings:
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return Settings()
    return Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})


def save_settings(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2))
