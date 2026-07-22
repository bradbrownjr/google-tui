"""Pilot scenario (ROADMAP P3): the Drive tab as a DataTable — folder
navigation, the '.. (up)' chrome row, and the 'Load more' sentinel all still
work once selection moved from ListItem ids to DataTable row keys.

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.drive_datatable_nav
"""
import asyncio
from unittest.mock import patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-drivenav-")

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

FOLDER = {"id": "fold1", "name": "Docs",
          "mimeType": "application/vnd.google-apps.folder", "parents": ["root"]}
FILE_ROOT = {"id": "d1", "name": "Notes.txt", "mimeType": "text/plain",
             "parents": ["root"], "size": "512"}
FILE_ROOT2 = {"id": "d2", "name": "More.txt", "mimeType": "text/plain",
              "parents": ["root"], "size": "8"}
CHILD = {"id": "c1", "name": "Inside.txt", "mimeType": "text/plain",
         "parents": ["fold1"], "size": "10"}


def fake_list_drive(svc, folder_id="root", page_token=None):
    if folder_id == "root":
        if page_token is None:
            return ([FOLDER, FILE_ROOT], "TOKEN2")  # page 1 + a next token
        return ([FILE_ROOT2], None)                 # page 2 (load-more)
    if folder_id == "fold1":
        return ([CHILD], None)
    return ([], None)


def _keys(table) -> list[str]:
    return [rk.value for rk in table.rows]


async def run() -> None:
    app = GoogleTUI()
    patches = base_patches() + [
        patch.object(gauth, "list_drive", side_effect=fake_list_drive),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(1)
            await pilot.pause()
            app.action_goto_tab_drive()
            await pilot.pause()
            table = app.query_one("#drive-list", DataTable)

            # Root: folder + file + Load-more sentinel; NO up-row at "/".
            assert _keys(table) == ["d-fold1", "d-d1", "load-more-drive"], _keys(table)

            # Load more → page 2 merges in, sentinel gone (token now None).
            app.action_load_more_drive()
            await asyncio.sleep(0.5)
            await pilot.pause()
            assert _keys(table) == ["d-fold1", "d-d1", "d-d2"], _keys(table)

            # Enter on the folder row → navigate in; up-row + child appear.
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert app._drive_path == "/Docs/", app._drive_path
            assert _keys(table) == ["d-up", "d-c1"], _keys(table)

            # Enter on the '.. (up)' row → back to root.
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert app._drive_path == "/", app._drive_path
            assert "d-fold1" in _keys(table), _keys(table)

    print("drive_datatable_nav PASSED")


if __name__ == "__main__":
    asyncio.run(run())
