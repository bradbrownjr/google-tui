"""Unit tests for google_tui.drive_sources' pure helper functions. No network,
no paramiko/ftplib connections — those live in tests/pilot/ scenarios instead
where they can be exercised through the mocked backend classes.
"""
import pytest

from google_tui import drive_sources


def test_parse_unix_ls_lines_basic_entries():
    lines = [
        "total 12",
        "drwxr-xr-x  2 user user 4096 Jan  2 03:04 subdir",
        "-rw-r--r--  1 user user  123 Jan  2 03:04 readme.txt",
        "-rw-r--r--  1 user user   99 Jan  2 03:04 file with spaces.txt",
    ]
    entries = dict(drive_sources._parse_unix_ls_lines(lines))
    assert entries["subdir"]["type"] == "dir"
    assert entries["readme.txt"]["type"] == "file"
    assert entries["readme.txt"]["size"] == 123
    assert "file with spaces.txt" in entries


def test_parse_unix_ls_lines_skips_dot_entries():
    lines = [
        "drwxr-xr-x  2 user user 4096 Jan  2 03:04 .",
        "drwxr-xr-x  2 user user 4096 Jan  2 03:04 ..",
        "-rw-r--r--  1 user user   10 Jan  2 03:04 real.txt",
    ]
    entries = dict(drive_sources._parse_unix_ls_lines(lines))
    assert "." not in entries and ".." not in entries
    assert "real.txt" in entries


def test_parse_unix_ls_lines_empty_input():
    assert drive_sources._parse_unix_ls_lines([]) == []


def test_guess_mime_folder_is_folder_mimetype():
    assert drive_sources._guess_mime("anything", True) == "application/vnd.folder"


def test_guess_mime_known_extension():
    assert drive_sources._guess_mime("notes.txt", False) == "text/plain"
    assert drive_sources._guess_mime("photo.jpg", False).startswith("image/")


def test_guess_mime_unknown_extension_falls_back():
    mime = drive_sources._guess_mime("mystery.qzx", False)
    assert mime  # non-empty fallback, doesn't raise


def test_mlsd_modify_to_iso_roundtrip():
    iso = drive_sources._mlsd_modify_to_iso("20260717120000")
    assert iso.startswith("2026-07-17T12:00:00")


def test_mlsd_modify_to_iso_malformed_input_does_not_raise():
    assert drive_sources._mlsd_modify_to_iso("") == ""
    assert drive_sources._mlsd_modify_to_iso("not-a-timestamp") == ""


def test_parse_ftp_url_defaults_to_anonymous():
    host, port, path, user, passwd = drive_sources.parse_ftp_url("ftp://ftp.example.com/pub/file.txt")
    assert host == "ftp.example.com"
    assert port == drive_sources.FTP_DEFAULT_PORT
    assert path == "/pub/file.txt"
    assert user == "anonymous"
    assert passwd == "anonymous@"


def test_parse_ftp_url_explicit_credentials_and_port():
    host, port, path, user, passwd = drive_sources.parse_ftp_url(
        "ftp://alice:s3cret@ftp.example.com:2121/home"
    )
    assert host == "ftp.example.com"
    assert port == 2121
    assert path == "/home"
    assert user == "alice"
    assert passwd == "s3cret"


def test_parse_ftp_url_missing_host_raises():
    with pytest.raises(drive_sources.RemoteBackendError):
        drive_sources.parse_ftp_url("ftp:///no/host/here")


def test_parse_sftp_url_defaults_no_anonymous_convention():
    host, port, path, user, passwd = drive_sources.parse_sftp_url("sftp://example.com/srv/data")
    assert host == "example.com"
    assert port == drive_sources.SSH_DEFAULT_PORT
    assert path == "/srv/data"
    assert user == ""
    assert passwd == ""


def test_parse_sftp_url_missing_host_raises():
    with pytest.raises(drive_sources.RemoteBackendError):
        drive_sources.parse_sftp_url("sftp:///no/host")


def test_source_key_for_composite_key():
    assert drive_sources.source_key_for("ftp", "ftp.example.com", 21) == "ftp:ftp.example.com:21"
    assert drive_sources.source_key_for("ssh", "example.com", 22) == "ssh:example.com:22"


def test_build_source_dispatches_on_protocol():
    ftp_src = drive_sources.build_source("ftp", "ftp.example.com", 21, "anonymous", "anonymous@")
    assert isinstance(ftp_src, drive_sources.FtpSource)
    assert ftp_src.source_key == "ftp:ftp.example.com:21"

    ssh_src = drive_sources.build_source("ssh", "example.com", 22, "alice", "hunter2")
    assert isinstance(ssh_src, drive_sources.SshSource)
    assert ssh_src.source_key == "ssh:example.com:22"
    ssh_src.close()


def test_build_source_unknown_protocol_raises():
    with pytest.raises(Exception):
        drive_sources.build_source("gopher", "example.com", 70, "", "")
