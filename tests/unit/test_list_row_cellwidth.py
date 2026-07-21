"""Cell-width alignment regression tests for the non-Email list row builders
(News / Contacts / Drive / Tasks / Events) and the shared _truncate helper.

Emoji (🚀, 📁, ⏳ — 2 cells) and CJK glyphs (2 cells) render wider than their
code point count, so the old len()/slice column math let a wide glyph overrun
its column and shove later columns / unpin fixed fields. Every builder now
measures in rendered cells; these tests pin that so the bug can't return.
"""
from rich.cells import cell_len

from google_tui.main import (
    _truncate, _cell_col,
    _news_line, _NEWS_FEED_W,
    _contact_line, _CONTACT_NAME_W,
    _drive_line,
    _task_line,
    _event_line,
)

_EMOJI = "🚀 launch"          # leading 2-cell emoji
_CJK = "日本語のタイトル" * 5   # 70 cells, well past any column


# --- _truncate / _cell_col primitives -------------------------------------

def test_truncate_measures_cells_not_codepoints():
    # 10 rockets = 20 cells; trimming to 8 cells must yield <= 8 cells, not 8
    # code points (which would render as 16).
    out = _truncate("🚀" * 10, 8)
    assert cell_len(out) <= 8
    assert out.endswith("…")


def test_truncate_leaves_short_text_untouched():
    assert _truncate("hi", 10) == "hi"


def test_cell_col_pads_to_exact_cell_width():
    assert cell_len(_cell_col("山田", 10)) == 10  # 4 cells + 6 pad
    assert cell_len(_cell_col("ascii", 10)) == 10


def test_cell_col_truncates_wide_content_to_exact_width():
    assert cell_len(_cell_col(_CJK, 12)) == 12


# --- News row: the [feed] bracket must close at the same cell every row ----

def _news(feed, title):
    return _news_line({"published": "2026-07-21T10:00:00Z",
                       "feed_title": feed, "title": title}, 60)


def _cell_offset(row, sub):
    """Rendered-cell column where `sub` begins in `row` (str.index gives a
    code-point offset, which differs from the cell offset for CJK/emoji)."""
    return cell_len(row[:row.index(sub)])


def test_news_feed_bracket_aligns_across_scripts():
    rows = [_news("Ars Technica", _EMOJI),
            _news("日本のニュース", _CJK),
            _news("XDA", "Plain title")]
    # The feed column is padded to a fixed cell width, so the closing bracket
    # lands at the same cell -> titles all start in the same column.
    close = [_cell_offset(r, "]") for r in rows]
    assert len(set(close)) == 1


def test_news_row_never_exceeds_target_width():
    assert cell_len(_news("日本のニュース", _CJK)) <= 60


# --- Contact row: name column pads to exact cell width --------------------

def test_contact_name_column_is_cell_padded():
    row = _contact_line({"name": "山田太郎", "email": "y@x.jp"}, 60)
    # name field + 1 gap, then the address; the address must start at the same
    # rendered cell whether the name is CJK or ASCII.
    ascii_row = _contact_line({"name": "Bob", "email": "b@x.com"}, 60)
    assert (_cell_offset(row, "y@x.jp")
            == _cell_offset(ascii_row, "b@x.com")
            == _CONTACT_NAME_W + 1)


# --- Drive / Task / Event: flexible field can't overrun the row width ------

def test_drive_row_within_width_with_cjk_name():
    assert cell_len(_drive_line({"is_folder": True, "name": _CJK}, 30)) <= 30


def test_task_row_within_width_with_pending_emoji_and_cjk():
    row = _task_line({"_pending": True, "status": "needsAction",
                      "title": _CJK}, 40)
    assert cell_len(row) <= 40
    assert row.startswith("⏳ [ ] ")


def test_event_row_within_width_with_pending_emoji_and_cjk():
    row = _event_line({"_pending": True,
                       "start": {"dateTime": "2026-07-21T18:00:00Z"},
                       "summary": _CJK}, 40)
    assert cell_len(row) <= 40
