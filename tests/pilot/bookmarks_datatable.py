"""Pilot scenario (ROADMAP P3): the Browser bookmarks list is a DataTable —
folder navigation and the '.. (up)' row work through row keys, and a leaf
bookmark navigates the browser.

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.bookmarks_datatable
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-bookmarks-")

from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

BOOKMARKS = [
    {"type": "folder", "label": "Work", "children": [
        {"type": "bookmark", "label": "Example", "url": "https://example.com"},
    ]},
    {"type": "bookmark", "label": "Gemini", "url": "gemini://geminiprotocol.net"},
]


def _keys(table) -> list[str]:
    return [rk.value for rk in table.rows]


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(1)
            await pilot.pause()
            app.settings.browser_bookmarks = BOOKMARKS
            app.action_goto_tab_browser()
            await pilot.pause()
            app.action_browser_show_bookmarks()
            await pilot.pause()

            table = app.query_one("#browser-bookmarks", DataTable)
            # Root: folder + leaf, no up-row.
            assert _keys(table) == ["bm-0", "bm-1"], _keys(table)

            # Enter on the folder → navigate in; up-row + the child appear.
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert _keys(table) == ["bm-up", "bm-0"], _keys(table)

            # Enter on '.. (up)' → back to root.
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert _keys(table) == ["bm-0", "bm-1"], _keys(table)

    print("bookmarks_datatable PASSED")


if __name__ == "__main__":
    asyncio.run(run())
