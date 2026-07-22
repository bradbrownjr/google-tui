"""Unit tests for the Email-list row's leading marker column — the unread
bullet and the ★ star (ROADMAP P2 "star from the list").

The list is a Textual DataTable now (ROADMAP P3), which measures each cell's
display width itself, so the old string-level cell-width/alignment tests (emoji
and CJK truncation to an exact rendered width) are gone with the `_cell_col`
arithmetic they covered — that correctness is now the widget's, exercised
end-to-end by tests/pilot/mail_datatable_expand.py. What survives as pure logic
is the mark column, tested here.
"""
from google_tui.main import _email_marks


def _marks(**over) -> str:
    th = {"unread": False, "labelIds": []}
    th.update(over)
    return _email_marks(th)


def test_starred_thread_shows_star_glyph():
    assert "★" in _marks(labelIds=["INBOX", "STARRED"])


def test_unstarred_thread_has_no_star_glyph():
    assert "★" not in _marks(labelIds=["INBOX"])


def test_missing_labelids_is_not_starred():
    assert "★" not in _email_marks({"unread": True})  # no labelIds key at all


def test_unread_and_star_marks_coexist():
    # Both the unread bullet and the star sit in the two leading marker cells.
    assert _marks(unread=True, labelIds=["STARRED"]) == "•★"


def test_read_unstarred_is_two_blanks():
    assert _marks(unread=False, labelIds=["INBOX"]) == "  "
