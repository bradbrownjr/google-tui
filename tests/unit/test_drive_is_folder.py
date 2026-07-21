"""Regression tests for the Drive KeyError('is_folder') app-exit crash.

The Drive render path subscripts f["is_folder"]. Live listings carry it (the
backend's list_children sets it), but the offline `drive_listing` cache
persists whatever shape was written — a cache saved by a version predating the
field held raw Google dicts, so after upgrading with a warm cache the first row
render raised KeyError('is_folder') in the apply worker and exited the app.
_with_is_folder() normalizes any ingested file dict so that can't happen.
"""
from google_tui.main import _with_is_folder, _drive_line, _DRIVE_FOLDER_MIME


def test_folder_mime_becomes_is_folder_true():
    f = {"id": "1", "name": "Docs", "mimeType": _DRIVE_FOLDER_MIME}
    assert _with_is_folder(f)["is_folder"] is True


def test_non_folder_mime_becomes_is_folder_false():
    f = {"id": "2", "name": "a.pdf", "mimeType": "application/pdf"}
    assert _with_is_folder(f)["is_folder"] is False


def test_missing_mimetype_defaults_to_file():
    # A degenerate cached dict lacking even mimeType must not raise.
    f = {"id": "3", "name": "weird"}
    assert _with_is_folder(f)["is_folder"] is False


def test_existing_is_folder_is_not_overwritten():
    # Live data already resolved this — a folder whose mime we don't recompute.
    f = {"id": "4", "name": "x", "mimeType": "application/pdf", "is_folder": True}
    assert _with_is_folder(f)["is_folder"] is True


def test_stale_cache_dict_renders_without_crashing():
    # The exact failure: a raw Google dict (no is_folder) reaching the row
    # builder. Pre-fix this raised KeyError; post-fix it renders.
    raw = {"id": "5", "name": "Report", "mimeType": _DRIVE_FOLDER_MIME}
    line = _drive_line(_with_is_folder(raw))
    assert "📁" in line and "Report" in line
