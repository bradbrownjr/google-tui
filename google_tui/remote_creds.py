"""Saved remote-filesystem host credentials (Drive tab: FTP/SSH sources).

Generalized from the former ``ftp_creds.py`` (FTP-only, Browser tab) once
remote-filesystem browsing moved into the Drive tab alongside Google Drive
and gained SSH (SFTP/SCP) support -- see ROADMAP/AGENTS.md.

Deliberately NOT stored via ``cache.Cache``, even though it reuses that
module's Fernet/scrypt helpers: the Cache is subject to Settings' "Clear
Cache" button and the retention-day/size-cap pruning in Settings -> General,
neither of which should be able to silently delete a saved login. This is
its own small file instead.

Also deliberately NOT stored in ``settings.py``: that module is explicitly
all-plaintext by design (see its docstring), and a saved password is exactly
the kind of value that shouldn't be written to disk unencrypted if the user
has already opted into encrypt-at-rest for everything else.

Encrypted with the SAME key material the local cache uses (``key: bytes |
None``, the derived-from-passphrase or keyfile bytes GoogleTUI already holds
as ``self._encrypt_key`` once Settings -> General's encrypt-at-rest is on)
when a key is supplied; stored as plain JSON otherwise, matching Settings'
own plaintext-by-default posture. If decryption fails (e.g. the key changed
under it), callers get back an empty dict rather than an exception — the
same graceful-degradation precedent ``cache.Cache`` uses for a bad row.

Entries are keyed by a composite ``source_key`` (``f"{protocol}:{host}:
{port}"``) rather than bare hostname, since the same hostname could plausibly
be saved for both FTP and SSH, or on two different ports -- the original
FTP-only version keyed on bare hostname alone, which would have silently
collided in that case. ``get()`` falls back to a bare-hostname lookup so
logins saved by that original version aren't orphaned by this change.

CREDS_PATH keeps its original filename (not renamed to match this module)
since it's a persistent user-data location, not an implementation detail —
renaming it would orphan every existing saved login on upgrade.
"""
from __future__ import annotations

import json
from pathlib import Path

import platformdirs
from cryptography.fernet import Fernet, InvalidToken

CREDS_PATH = Path(platformdirs.user_config_dir("google-tui")) / "ftp_credentials.json"


def _source_key(protocol: str, host: str, port: int) -> str:
    return f"{protocol}:{host}:{port}"


def load_all(key: bytes | None) -> dict[str, dict[str, str]]:
    """source_key -> {"username", "password", "protocol", "host", "port"}."""
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


def get(key: bytes | None, protocol: str, host: str, port: int) -> tuple[str, str] | None:
    creds = load_all(key)
    entry = creds.get(_source_key(protocol, host, port))
    if entry is None:
        # Backward compat: the pre-generalization format keyed on bare
        # hostname only, always implicitly FTP.
        entry = creds.get(host) if protocol == "ftp" else None
    if not entry:
        return None
    return entry.get("username", ""), entry.get("password", "")


def set_credentials(key: bytes | None, protocol: str, host: str, port: int, username: str, password: str) -> None:
    creds = load_all(key)
    creds[_source_key(protocol, host, port)] = {
        "username": username, "password": password, "protocol": protocol, "host": host, "port": port,
    }
    save_all(key, creds)


def remove(key: bytes | None, protocol: str, host: str, port: int) -> None:
    creds = load_all(key)
    removed = False
    source_key = _source_key(protocol, host, port)
    if source_key in creds:
        del creds[source_key]
        removed = True
    if protocol == "ftp" and host in creds:
        del creds[host]
        removed = True
    if removed:
        save_all(key, creds)


def list_hosts(key: bytes | None) -> list[tuple[str, str, int]]:
    """(protocol, host, port) for every saved entry, sorted for display —
    backs the Drive-tab source picker and Settings' saved-hosts list.
    Legacy bare-hostname entries (protocol/host/port not in their own value
    dict) are reported as ("ftp", <that hostname>, 21).
    """
    out = []
    for source_key, entry in load_all(key).items():
        if "protocol" in entry and "host" in entry and "port" in entry:
            out.append((entry["protocol"], entry["host"], int(entry["port"])))
        elif ":" not in source_key:
            out.append(("ftp", source_key, 21))
    return sorted(set(out))
