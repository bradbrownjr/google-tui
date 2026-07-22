"""Pilot scenario (2026-07-22 Settings/Browser overhaul): the Browser tab's
bookmarks table gets keyboard focus automatically (fixing the arrow/Home/End
"doesn't move" complaint), "S" cycles sort mode with a live footer update,
"Delete" removes the highlighted bookmark, and Settings -> Browser is its own
sub-tab holding the moved home/start-page/remote-hosts controls.

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.bookmarks_sort
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-bookmarks-sort-")

from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches, dt_iso  # noqa: E402
from textual.widgets import DataTable, Input  # noqa: E402

BOOKMARKS = [
    {"type": "bookmark", "label": "Bravo", "url": "https://bravo.example",
     "added_at": dt_iso(-5), "last_opened_at": dt_iso(-1)},
    {"type": "bookmark", "label": "Alpha", "url": "https://alpha.example",
     "added_at": dt_iso(-1), "last_opened_at": dt_iso(-5)},
    {"type": "bookmark", "label": "Charlie", "url": "https://charlie.example"},
]


def _keys(table) -> list[str]:
    return [rk.value for rk in table.rows]


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(1)
            await pilot.pause()
            # In-place mutation, not reassignment: GoogleTUI.__init__ already
            # captured a reference to the original list in
            # self._bookmark_current_list, and this is the tab's FIRST
            # activation (no "B" press to re-sync that reference) -- exactly
            # the path that exercises the on_tabbed_content_tab_activated
            # focus fix below.
            app.settings.browser_bookmarks[:] = [dict(b) for b in BOOKMARKS]
            app.action_goto_tab_browser()
            await pilot.pause()

            table = app.query_one("#browser-bookmarks", DataTable)
            # Focus fix: entering the Browser tab with bookmarks showing
            # focuses the table itself, not the URL Input -- otherwise
            # arrow/Home/End never reach DataTable's own cursor bindings.
            assert app.focused is table, app.focused

            # Default sort is "name" (alphabetical): Alpha, Bravo, Charlie.
            def _labels():
                # Cells are "<icon> <label>" (see _bookmark_scheme_style) --
                # strip the icon, this scenario only cares about ordering.
                return [table.get_cell_at((i, 0)).plain.split(" ", 1)[1]
                        for i in range(table.row_count)]
            assert _labels() == ["Alpha", "Bravo", "Charlie"], _labels()

            # Arrow/Home/End move the row cursor (the reported-broken behavior).
            table.move_cursor(row=0)
            await pilot.press("down")
            await pilot.pause()
            assert table.cursor_row == 1, table.cursor_row
            await pilot.press("end")
            await pilot.pause()
            assert table.cursor_row == 2, table.cursor_row
            await pilot.press("home")
            await pilot.pause()
            assert table.cursor_row == 0, table.cursor_row

            # "S" cycles sort mode: name -> added -> used -> name, and the
            # footer text reflects the live mode (not a stale static string).
            assert "S Sort: Name" in app._context_help_text()
            await pilot.press("s")
            await pilot.pause()
            assert app.settings.browser_bookmark_sort == "added"
            assert "S Sort: Added" in app._context_help_text()
            assert _labels() == ["Alpha", "Bravo", "Charlie"], _labels()  # Alpha added most recently

            await pilot.press("s")
            await pilot.pause()
            assert app.settings.browser_bookmark_sort == "used"
            assert "S Sort: Used" in app._context_help_text()
            assert _labels() == ["Bravo", "Alpha", "Charlie"], _labels()  # Bravo opened most recently

            await pilot.press("s")
            await pilot.pause()
            assert app.settings.browser_bookmark_sort == "name"

            # Delete removes the highlighted bookmark and persists it.
            table.move_cursor(row=0)  # Alpha
            await pilot.pause()
            await pilot.press("delete")
            await pilot.pause()
            assert _labels() == ["Bravo", "Charlie"], _labels()
            assert all(b.get("label") != "Alpha" for b in app.settings.browser_bookmarks)

            # Settings -> Browser is its own sub-tab with the moved controls.
            app.action_goto_tab_settings()
            await pilot.pause()
            tabs = app.query_one("#settings-tabs")
            tabs.active = "settings-tab-browser"
            await pilot.pause()
            assert app.query_one("#settings-browser-home-url", Input) is not None
            assert app.query_one("#settings-browser-start-page") is not None
            assert app.query_one("#settings-remote-hosts-list", DataTable) is not None

    print("bookmarks_sort PASSED")


if __name__ == "__main__":
    asyncio.run(run())
