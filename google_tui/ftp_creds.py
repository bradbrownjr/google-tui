"""Saved FTP host credentials (Browser tab).

Deliberately NOT stored via ``cache.Cache``, even though it reuses that
module's Fernet/scrypt helpers: the Cache is subject to Settings' "Clear
Cache" button and the retention-day/size-cap pruning in Settings -> General,
neither of which should be able to silently delete a saved login. This is
its own small file instead.

Also deliberately NOT stored in ``settings.py``: that module is explicitly
all-plaintext by design (see its docstring), and a saved FTP password is
exactly the kind of value that shouldn't be written to disk unencrypted if
the user has already opted into encrypt-at-rest for everything else.

Encrypted with the SAME key material the local cache uses (``key: bytes |
None``, the derived-from-passphrase or keyfile bytes GoogleTUI already holds
as ``self._encrypt_key`` once Settings -> General's encrypt-at-rest is on)
when a key is supplied; stored as plain JSON otherwise, matching Settings'
own plaintext-by-default posture. If decryption fails (e.g. the key changed
under it), callers get back an empty dict rather than an exception — the
same graceful-degradation precedent ``cache.Cache`` uses for a bad row.
"""
from __future__ import annotations

import json
from pathlib import Path

import platformdirs
from cryptography.fernet import Fernet, InvalidToken

CREDS_PATH = Path(platformdirs.user_config_dir("google-tui")) / "ftp_credentials.json"


def load_all(key: bytes | None) -> dict[str, dict[str, str]]:
    """host -> {"username": str, "password": str}."""
    try:
        raw = json.loads(CREDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    blob = raw.get("encrypted")
    if blob is None:
        # Written while encryption was off (or pre-dates it) -- plain dict.
        return {k: v for k, v in raw.items() if k != "encrypted"}
    if key is None:
        return {}  # encrypted on disk but no key available to read it
    try:
        return json.loads(Fernet(key).decrypt(blob.encode("ascii")))
    except (InvalidToken, ValueError):
        return {}


def save_all(key: bytes | None, creds: dict[str, dict[str, str]]) -> None:
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if key is None:
        CREDS_PATH.write_text(json.dumps(creds, indent=2))
        return
    token = Fernet(key).encrypt(json.dumps(creds).encode("utf-8")).decode("ascii")
    CREDS_PATH.write_text(json.dumps({"encrypted": token}, indent=2))


def get(key: bytes | None, host: str) -> tuple[str, str] | None:
    entry = load_all(key).get(host)
    if not entry:
        return None
    return entry.get("username", ""), entry.get("password", "")


def set(key: bytes | None, host: str, username: str, password: str) -> None:
    creds = load_all(key)
    creds[host] = {"username": username, "password": password}
    save_all(key, creds)


def remove(key: bytes | None, host: str) -> None:
    creds = load_all(key)
    if host in creds:
        del creds[host]
        save_all(key, creds)
