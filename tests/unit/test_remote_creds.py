"""Unit tests for google_tui.remote_creds' save/load/get/remove/list_hosts
round trip. tests/conftest.py has already redirected platformdirs to an
isolated temp dir before this module (or any google_tui module) was
imported, so CREDS_PATH here is never the real ~/.config/google-tui file.
"""
from google_tui import remote_creds


def test_set_and_get_credentials_round_trip():
    remote_creds.set_credentials(None, "ftp", "ftp.example.com", 21, "alice", "s3cret")
    result = remote_creds.get(None, "ftp", "ftp.example.com", 21)
    assert result == ("alice", "s3cret")


def test_get_missing_host_returns_none():
    assert remote_creds.get(None, "ssh", "nowhere.example.com", 22) is None


def test_composite_key_distinguishes_protocol_and_port():
    remote_creds.set_credentials(None, "ftp", "shared-host.example.com", 21, "ftp-user", "ftp-pass")
    remote_creds.set_credentials(None, "ssh", "shared-host.example.com", 22, "ssh-user", "ssh-pass")
    assert remote_creds.get(None, "ftp", "shared-host.example.com", 21) == ("ftp-user", "ftp-pass")
    assert remote_creds.get(None, "ssh", "shared-host.example.com", 22) == ("ssh-user", "ssh-pass")


def test_remove_deletes_entry():
    remote_creds.set_credentials(None, "ftp", "toremove.example.com", 21, "u", "p")
    assert remote_creds.get(None, "ftp", "toremove.example.com", 21) is not None
    remote_creds.remove(None, "ftp", "toremove.example.com", 21)
    assert remote_creds.get(None, "ftp", "toremove.example.com", 21) is None


def test_remove_nonexistent_entry_does_not_raise():
    remote_creds.remove(None, "ftp", "never-saved.example.com", 21)


def test_list_hosts_reports_saved_entries():
    remote_creds.set_credentials(None, "ftp", "list-a.example.com", 21, "u", "p")
    remote_creds.set_credentials(None, "ssh", "list-b.example.com", 22, "u", "p")
    hosts = remote_creds.list_hosts(None)
    assert ("ftp", "list-a.example.com", 21) in hosts
    assert ("ssh", "list-b.example.com", 22) in hosts


def test_list_hosts_dedupes_via_sorted_set():
    # A direct regression test for the builtin-shadowing bug fixed when
    # set_credentials was still named `set` — list_hosts' internal
    # `sorted(set(out))` call must resolve to the builtin `set` type, not
    # this module's own credential-saving function.
    remote_creds.set_credentials(None, "ftp", "dedupe.example.com", 21, "u1", "p1")
    remote_creds.set_credentials(None, "ftp", "dedupe.example.com", 21, "u2", "p2")
    hosts = remote_creds.list_hosts(None)
    assert hosts.count(("ftp", "dedupe.example.com", 21)) == 1


def test_legacy_bare_hostname_entry_falls_back_for_ftp():
    creds = remote_creds.load_all(None)
    creds["legacy.example.com"] = {"username": "legacy-user", "password": "legacy-pass"}
    remote_creds.save_all(None, creds)
    assert remote_creds.get(None, "ftp", "legacy.example.com", 21) == ("legacy-user", "legacy-pass")
    # SSH never gets the bare-hostname fallback -- that convention predates
    # SSH support entirely and was always FTP-only.
    assert remote_creds.get(None, "ssh", "legacy.example.com", 22) is None


def test_save_all_encrypts_when_key_supplied():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    remote_creds.set_credentials(key, "ftp", "encrypted.example.com", 21, "eu", "ep")
    raw = remote_creds.CREDS_PATH.read_text()
    assert "encrypted.example.com" not in raw  # not stored in plaintext on disk
    assert remote_creds.get(key, "ftp", "encrypted.example.com", 21) == ("eu", "ep")
    # Wrong/missing key can't decrypt -- degrades to empty rather than raising.
    assert remote_creds.get(None, "ftp", "encrypted.example.com", 21) is None
