"""Unit tests for the Email-list row builder's ★ star column (ROADMAP P2
"star from the list"). Pure string function over a thread-summary dict.
"""
from google_tui.main import _email_collapsed_line


def _row(**over) -> str:
    th = {"threadId": "t", "subject": "Hi", "from": "a@b.com", "count": 1,
          "unread": False, "date": "", "labelIds": []}
    th.update(over)
    return _email_collapsed_line(th)


def test_starred_thread_shows_star_glyph():
    assert "★" in _row(labelIds=["INBOX", "STARRED"])


def test_unstarred_thread_has_no_star_glyph():
    assert "★" not in _row(labelIds=["INBOX"])


def test_missing_labelids_is_not_starred():
    th = {"threadId": "t", "subject": "Hi", "from": "a@b.com", "count": 1,
          "unread": True, "date": ""}  # no labelIds key at all
    assert "★" not in _email_collapsed_line(th)


def test_unread_and_star_marks_coexist():
    row = _row(unread=True, labelIds=["STARRED"])
    # Both the unread bullet and the star sit in the leading marker columns.
    assert row.startswith("•★")
