"""Regression tests for the News duplicate-widget-id crash.

Live sessions crashed with Textual DuplicateIds / MountError when two news
entries produced the same widget id — a story syndicated across feeds (shared
guid/link), a feed configured twice, or two distinct ids that _mk_id slugifies
to the same string. Both the News tab and the Dashboard news card mount one
widget per row keyed on the entry id, so a collision took down the whole list.
"""
from google_tui.main import _unique_id, _dedup_by_key, _mk_id


def test_unique_id_passes_through_unseen():
    seen: set = set()
    assert _unique_id("n-abc", seen) == "n-abc"
    assert "n-abc" in seen


def test_unique_id_suffixes_repeat():
    seen: set = set()
    assert _unique_id("n-abc", seen) == "n-abc"
    assert _unique_id("n-abc", seen) == "n-abc-2"
    assert _unique_id("n-abc", seen) == "n-abc-3"


def test_unique_id_preserves_prefix_for_startswith_dispatch():
    # on_list_view_selected dispatches on cid.startswith("n-"); the suffix must
    # not break that.
    seen: set = set()
    _unique_id("n-abc", seen)
    assert _unique_id("n-abc", seen).startswith("n-")


def test_slug_collision_between_distinct_ids_is_disambiguated():
    # `a/b` and `a-b` slugify to the same _mk_id — the exact class of collision
    # that crashed the mount.
    seen: set = set()
    first = _unique_id(_mk_id("n", "a/b"), seen)
    second = _unique_id(_mk_id("n", "a-b"), seen)
    assert first != second


def test_dedup_drops_repeated_ids_keeping_first():
    entries = [
        {"id": "http://x/1", "title": "A"},
        {"id": "http://x/1", "title": "A (syndicated copy)"},
        {"id": "http://x/2", "title": "B"},
    ]
    out = _dedup_by_key(entries)
    assert [e["title"] for e in out] == ["A", "B"]


def test_dedup_preserves_order():
    entries = [{"id": str(i)} for i in [3, 1, 3, 2, 1]]
    assert [e["id"] for e in _dedup_by_key(entries)] == ["3", "1", "2"]


def test_dedup_empty():
    assert _dedup_by_key([]) == []
