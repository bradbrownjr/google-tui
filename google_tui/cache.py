"""Local SQLite cache for Google data, with optional per-row Fernet
encryption. Rows are split into small "browse" data (thread summaries,
event/task summaries, drive listings/metadata) that's cheap to bulk-decrypt,
and large "content" data (thread bodies, drive file text) that's decrypted
one row at a time, only when actually opened. This keeps the encryption
overhead proportional to what's on screen, not the size of the cache.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import platformdirs
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

CACHE_DB_PATH = Path(platformdirs.user_cache_dir("google-tui")) / "cache.db"
KEY_FILE_PATH = Path(platformdirs.user_config_dir("google-tui")) / "cache.key"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
CANARY_PLAINTEXT = b"google-tui-canary"


def new_salt() -> bytes:
    return os.urandom(16)


def derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def read_or_create_keyfile() -> bytes:
    if KEY_FILE_PATH.exists():
        return KEY_FILE_PATH.read_bytes()
    KEY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    KEY_FILE_PATH.write_bytes(key)
    KEY_FILE_PATH.chmod(0o600)
    return key


def make_canary(key: bytes) -> str:
    return Fernet(key).encrypt(CANARY_PLAINTEXT).decode("ascii")


def verify_canary(key: bytes, canary: str) -> bool:
    try:
        return Fernet(key).decrypt(canary.encode("ascii")) == CANARY_PLAINTEXT
    except InvalidToken:
        return False


class Cache:
    """category/key -> JSON payload, optionally Fernet-encrypted per row."""

    def __init__(self, key: bytes | None):
        self._fernet = Fernet(key) if key else None
        self._lock = threading.Lock()
        CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache_items ("
                "category TEXT NOT NULL, key TEXT NOT NULL, payload BLOB NOT NULL, "
                "updated_at TEXT NOT NULL, PRIMARY KEY (category, key))"
            )
            self._conn.commit()

    def _encode(self, value: Any) -> bytes:
        raw = json.dumps(value).encode("utf-8")
        return self._fernet.encrypt(raw) if self._fernet else raw

    def _decode(self, blob: bytes) -> Any:
        raw = self._fernet.decrypt(blob) if self._fernet else blob
        return json.loads(raw)

    def get(self, category: str, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM cache_items WHERE category = ? AND key = ?",
                (category, key),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._decode(row[0])
        except InvalidToken:
            return None

    def get_all(self, category: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, payload FROM cache_items WHERE category = ?", (category,)
            ).fetchall()
        out: dict[str, Any] = {}
        for key, payload in rows:
            try:
                out[key] = self._decode(payload)
            except InvalidToken:
                continue
        return out

    def put(self, category: str, key: str, value: Any) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        blob = self._encode(value)
        with self._lock:
            self._conn.execute(
                "INSERT INTO cache_items (category, key, payload, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(category, key) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at",
                (category, key, blob, now),
            )
            self._conn.commit()

    def put_many(self, category: str, items: dict[str, Any]) -> None:
        if not items:
            return
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows = [(category, key, self._encode(value), now) for key, value in items.items()]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO cache_items (category, key, payload, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(category, key) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at",
                rows,
            )
            self._conn.commit()

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cache_items")
            self._conn.commit()
