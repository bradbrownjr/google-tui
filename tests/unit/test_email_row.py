"""Unit tests for the Email-list row builder's ★ star column (ROADMAP P2
"star from the list") and its cell-width column alignment. Pure string
function over a thread-summary dict.
"""
from rich.cells import cell_len

from google_tui.main import _email_collapsed_line, _EMAIL_ROW_DEFAULT_W


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


# --- cell-width column alignment ------------------------------------------
# Emoji (🚀, width 2) and CJK glyphs (width 2) render wider than their code
# point count; measuring columns with len()/slicing shifted the subject/chips/
# date columns on any row containing one. Rows must be EXACTLY the target width
# in rendered cells regardless of content so the date stays right-pinned.
_LABELS = {"L": {"name": "Records", "type": "user"}}
_DATE = "Tue, 21 Jul 2026 10:23:00 +0000"


def _wide_row(**over) -> str:
    th = {"threadId": "t", "subject": "Hi", "from": "a@b.com", "count": 1,
          "unread": True, "date": _DATE, "labelIds": ["INBOX", "L"]}
    if "from_" in over:
        th["from"] = over.pop("from_")
    th.update(over)
    return _email_collapsed_line(th, labels_by_id=_LABELS,
                                 width=_EMAIL_ROW_DEFAULT_W)


def test_row_is_exactly_target_cell_width_for_ascii():
    assert cell_len(_wide_row()) == _EMAIL_ROW_DEFAULT_W


def test_emoji_subject_does_not_shift_columns():
    row = _wide_row(subject="🚀 Introducing Gemini 3.6 Flash")
    assert cell_len(row) == _EMAIL_ROW_DEFAULT_W


def test_cjk_sender_and_subject_stay_aligned():
    row = _wide_row(from_="山田太郎", subject="日本語のメール件名テスト")
    assert cell_len(row) == _EMAIL_ROW_DEFAULT_W


def test_overflowing_cjk_subject_truncates_to_target_width():
    long_cjk = "日本語" * 40  # 240 cells wide, far past the column
    row = _wide_row(subject=long_cjk)
    assert cell_len(row) == _EMAIL_ROW_DEFAULT_W
    assert "…" in row


def test_date_is_right_pinned_across_mixed_content():
    rows = [
        _wide_row(subject="Plain ascii subject", from_="Bradley"),
        _wide_row(subject="🚀 emoji subject", from_="Google AI Studio"),
        _wide_row(subject="日本語の件名", from_="山田太郎"),
    ]
    # All identical rendered width => the trailing date column lands in the
    # same cells on every row.
    assert {cell_len(r) for r in rows} == {_EMAIL_ROW_DEFAULT_W}
    for r in rows:
        assert r.endswith("07/21 10:23AM")
