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
    ai_provider: str = "hermes"  # "hermes" | "claude_code" | "opencode" | "gemini_cli"
    nous_api_key: str | None = None  # overrides ~/.hermes/config.yaml if set
    feed_urls: list[str] = field(default_factory=list)  # subscribed RSS/Atom feeds (News tab, P1 M3)


def load_settings() -> Settings:
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return Settings()
    return Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})


def save_settings(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2))
