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

    def rekey(self, key: bytes | None) -> None:
        """Swap the encryption key in place — e.g. Settings' encrypt-at-rest
        toggle or key method changing mid-session. Callers are expected to
        have already cleared the cache first: rows written under the old key
        aren't re-encrypted, they'd just fail to decrypt under the new one.
        """
        self._fernet = Fernet(key) if key else None

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

    def delete(self, category: str, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM cache_items WHERE category = ? AND key = ?", (category, key))
            self._conn.commit()

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cache_items")
            self._conn.commit()
        self.vacuum()

    # ---- size accounting & pruning -------------------------------------
    #
    # Everything in this cache is refetchable, and (since the historyId /
    # modifiedTime revalidation went in) re-fetching a pruned row is cheap and
    # automatic. That's what makes eviction safe: pruning can only ever cost a
    # little latency the next time you open that thread/file, never data.
    #
    # Age is measured by `updated_at`, which is refreshed every time a row is
    # re-written — i.e. on every refresh that still sees it. That makes it a
    # "last seen" stamp rather than a "first cached" one, which is exactly the
    # semantics you want: a thread still in your inbox or an entry still in a
    # feed keeps getting touched and never ages out, while a thread that fell
    # off the list, a feed entry that scrolled away, or a Drive file you haven't
    # opened in months goes stale and becomes evictable.

    def db_size(self) -> int:
        """Bytes actually on disk (0 if the file doesn't exist yet)."""
        try:
            return CACHE_DB_PATH.stat().st_size
        except FileNotFoundError:
            return 0

    def stats(self) -> dict:
        """Size accounting for the Settings pane: total bytes on disk, row
        count, and a per-category breakdown (rows + approximate payload bytes).

        Category bytes are the summed payload lengths, so they add up to a bit
        less than the file on disk — SQLite has page overhead, indexes, and free
        pages left behind by deletes. Reported separately rather than fudged.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT category, COUNT(*), SUM(LENGTH(payload)), MIN(updated_at) "
                "FROM cache_items GROUP BY category"
            ).fetchall()
        cats = [
            {"category": c, "rows": n, "bytes": int(b or 0), "oldest": oldest}
            for c, n, b, oldest in rows
        ]
        cats.sort(key=lambda c: c["bytes"], reverse=True)
        return {
            "db_bytes": self.db_size(),
            "payload_bytes": sum(c["bytes"] for c in cats),
            "rows": sum(c["rows"] for c in cats),
            "categories": cats,
        }

    def vacuum(self) -> None:
        """Reclaim disk. A DELETE only frees SQLite *pages*, it does not shrink
        the file — without this, pruning would report freeing space while the
        file on disk stayed exactly as large, which is the one thing a user
        watching a size number will not forgive."""
        try:
            with self._lock:
                self._conn.execute("VACUUM")
                self._conn.commit()
        except sqlite3.Error:
            pass  # e.g. VACUUM inside a transaction; not worth failing a prune over

    def prune(self, max_age_days: int = 0, max_bytes: int = 0) -> dict:
        """Enforce the retention window and the size cap. Both are opt-in: 0
        means "no limit". Returns {"by_age": n, "by_size": n} rows removed."""
        removed_age = 0
        removed_size = 0

        if max_age_days > 0:
            cutoff = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=max_age_days)
            ).isoformat()
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM cache_items WHERE updated_at < ?", (cutoff,))
                removed_age = cur.rowcount or 0
                self._conn.commit()

        # Size cap: evict least-recently-seen rows until the payload total fits.
        # We budget against summed payload bytes (not the file size) because the
        # file only shrinks at VACUUM, so using it as the loop condition would
        # never converge. Vacuum once at the end instead.
        if max_bytes > 0:
            with self._lock:
                total = self._conn.execute(
                    "SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM cache_items"
                ).fetchone()[0]
                if total > max_bytes:
                    over = total - max_bytes
                    freed = 0
                    doomed: list[tuple[str, str]] = []
                    for cat, key, size in self._conn.execute(
                        "SELECT category, key, LENGTH(payload) FROM cache_items "
                        "ORDER BY updated_at ASC"
                    ):
                        doomed.append((cat, key))
                        freed += size
                        if freed >= over:
                            break
                    self._conn.executemany(
                        "DELETE FROM cache_items WHERE category = ? AND key = ?", doomed)
                    removed_size = len(doomed)
                    self._conn.commit()

        if removed_age or removed_size:
            self.vacuum()
        return {"by_age": removed_age, "by_size": removed_size}


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
