"""Shared helpers for the DataTable-backed list views (ROADMAP P3 migration).

Every list (Mail, News, Contacts, Drive, Tasks, Events, …) was a `ListView` of
`ListItem(Label(one hand-padded string))`, so column alignment was our own
display-width arithmetic (`_cell_col`/`_truncate` in main.py) — which bit twice
with emoji/CJK width shifts. `DataTable` owns real columns and measures each
cell's rendered width itself, so that whole layer disappears. What is still
shared across the flat lists, and lives here:

* **The one flex column.** DataTable has no native "column that fills the
  remaining width", so the responsive subject/name/title column becomes a
  single width calc (`flex_fill_width`) off the table's content region.
* **A uniform rebuild.** `rebuild_flat_table` clears + re-adds columns (with the
  flex column sized) + rows in one call, mirroring the Calendar grids' proven
  `clear(columns=True)` → `add_column(width=…)` → `add_row(…)` idiom
  (`_apply_cal_month`). Cells are Rich `Text` so per-cell styling (dim message
  rows, the multi-select tint) rides along and width is measured correctly.

`DataTable.clear()` is synchronous (unlike `ListView.clear()`, whose async
`AwaitRemove` forced the generation-counter workers in the old apply paths), so
callers can clear-and-repopulate inline on the main thread.
"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable

# A column whose width is None is THE flexible one, sized by flex_fill_width.
# Only one flex column is supported per table (there's one "remaining width").
Column = tuple[str, "int | None"]


def flex_fill_width(table: DataTable, fixed_total: int, n_cols: int,
                    flex_min: int, fallback: int) -> int:
    """Width to give the single flexible column so its row fills the table:
    the content region minus every fixed column's width and the cell padding
    DataTable draws on both sides of every column, floored at `flex_min`.

    Returns a `flex_min`-vs-`fallback` sensible default before the table has
    been laid out (content_size is 0 until the first layout pass — e.g. when
    rows are built during startup)."""
    content_w = table.content_size.width or table.size.width
    if content_w <= 0:
        return max(flex_min, fallback)
    pad = getattr(table, "cell_padding", 1) * 2
    avail = content_w - fixed_total - pad * n_cols
    return max(avail, flex_min)


def current_row_key(table: DataTable) -> "str | None":
    """The row key (the string the caller passed to `add_row(key=…)`) under the
    row cursor, or None when the table is empty. Replaces ListView's
    `highlighted_child.id` for the keyboard-action handlers."""
    if table.row_count == 0:
        return None
    try:
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
    except Exception:
        return None


def _as_cell(cell) -> Text:
    """Normalize a cell to a single-line, ellipsis-on-overflow Rich `Text`
    (keeps any style already on a passed-in Text — that's how the dim message
    rows and the multi-select tint are carried)."""
    if isinstance(cell, Text):
        cell.no_wrap = True
        if cell.overflow is None:
            cell.overflow = "ellipsis"
        return cell
    return Text(str(cell), no_wrap=True, overflow="ellipsis")


def rebuild_flat_table(table: DataTable, columns: list[Column],
                       rows: list[tuple[str, list]], *,
                       flex_min: int = 10, fallback_width: int = 80) -> None:
    """(Re)build a flat single-level DataTable in one shot.

    `columns` — (label, width) pairs; exactly one width may be None to mark the
    flexible column. `rows` — (row_key, cells) pairs, cells in column order.
    Row keys must be unique (DataTable raises on a duplicate) — see
    `main._unique_id` for the dedup the callers apply to slugified ids."""
    table.clear(columns=True)
    fixed_total = sum(w for _, w in columns if w is not None)
    flex_w = flex_fill_width(table, fixed_total, len(columns), flex_min, fallback_width)
    for label, width in columns:
        table.add_column(label, width=(flex_w if width is None else width))
    for key, cells in rows:
        table.add_row(*[_as_cell(c) for c in cells], key=key)
