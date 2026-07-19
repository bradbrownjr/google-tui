"""Unit tests for google_tui.cache.Cache's category/key -> JSON payload
store. tests/conftest.py has already redirected platformdirs to an isolated
temp dir before import, so CACHE_DB_PATH here is never the real
~/.cache/google-tui/cache.db.
"""
import pytest

from google_tui import cache as cache_module


@pytest.fixture
def db():
    # One sqlite file is shared by every Cache(...) instance in this process
    # (CACHE_DB_PATH is a module-level constant) -- clear it so each test
    # starts from a known-empty state regardless of what earlier tests wrote.
    c = cache_module.Cache(key=None)
    c.clear_all()
    return c


def test_put_and_get_round_trip(db):
    db.put("thread_summary", "th1", {"subject": "hello", "unread": True})
    assert db.get("thread_summary", "th1") == {"subject": "hello", "unread": True}


def test_get_missing_key_returns_none(db):
    assert db.get("thread_summary", "does-not-exist") is None


def test_put_overwrites_existing_row(db):
    db.put("thread_summary", "th2", {"subject": "first"})
    db.put("thread_summary", "th2", {"subject": "second"})
    assert db.get("thread_summary", "th2") == {"subject": "second"}


def test_categories_are_namespaced_independently(db):
    # Regression precedent for the Drive-sources preview-cache namespacing
    # fix: two different categories can use the identical key without
    # colliding, e.g. drive_file_text:google vs drive_file_text:ftp:host:21.
    db.put("drive_file_text:google", "/readme.txt", {"text": "google copy"})
    db.put("drive_file_text:ftp:host.example.com:21", "/readme.txt", {"text": "ftp copy"})
    assert db.get("drive_file_text:google", "/readme.txt") == {"text": "google copy"}
    assert db.get("drive_file_text:ftp:host.example.com:21", "/readme.txt") == {"text": "ftp copy"}


def test_get_all_returns_every_row_in_category(db):
    db.put("thread_summary", "a", {"n": 1})
    db.put("thread_summary", "b", {"n": 2})
    db.put("other_category", "c", {"n": 3})
    all_rows = db.get_all("thread_summary")
    assert all_rows == {"a": {"n": 1}, "b": {"n": 2}}


def test_put_many_bulk_writes(db):
    db.put_many("thread_summary", {"x": {"n": 1}, "y": {"n": 2}})
    assert db.get("thread_summary", "x") == {"n": 1}
    assert db.get("thread_summary", "y") == {"n": 2}


def test_put_many_empty_dict_is_a_noop(db):
    db.put_many("thread_summary", {})  # must not raise


def test_delete_removes_row(db):
    db.put("thread_summary", "del1", {"n": 1})
    db.delete("thread_summary", "del1")
    assert db.get("thread_summary", "del1") is None


def test_clear_all_empties_every_category(db):
    db.put("thread_summary", "a", {"n": 1})
    db.put("other_category", "b", {"n": 2})
    db.clear_all()
    assert db.get("thread_summary", "a") is None
    assert db.get("other_category", "b") is None


def test_encrypted_round_trip_with_key():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    encrypted_db = cache_module.Cache(key=key)
    encrypted_db.put("thread_summary", "e1", {"subject": "secret"})
    assert encrypted_db.get("thread_summary", "e1") == {"subject": "secret"}


def test_get_with_wrong_key_degrades_to_none_not_exception():
    from cryptography.fernet import Fernet
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()
    db_a = cache_module.Cache(key=key_a)
    db_a.put("thread_summary", "wrongkey1", {"subject": "secret"})
    db_b = cache_module.Cache(key=key_b)
    assert db_b.get("thread_summary", "wrongkey1") is None


def test_canary_round_trip():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    canary = cache_module.make_canary(key)
    assert cache_module.verify_canary(key, canary) is True
    other_key = Fernet.generate_key()
    assert cache_module.verify_canary(other_key, canary) is False


def test_derive_key_from_passphrase_is_deterministic_for_same_salt():
    salt = cache_module.new_salt()
    key1 = cache_module.derive_key_from_passphrase("correct horse battery staple", salt)
    key2 = cache_module.derive_key_from_passphrase("correct horse battery staple", salt)
    assert key1 == key2


def test_derive_key_from_passphrase_differs_for_different_salt():
    key1 = cache_module.derive_key_from_passphrase("same passphrase", cache_module.new_salt())
    key2 = cache_module.derive_key_from_passphrase("same passphrase", cache_module.new_salt())
    assert key1 != key2
