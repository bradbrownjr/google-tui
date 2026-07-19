"""Pilot scenario: Google Drive listing/preview still work through the
drive_sources.DriveBackend abstraction (regression coverage for the
_drive_items_by_cid / is_folder refactor -- see CHANGELOG [2026-07-18]).

Usage: python -m tests.pilot.drive_google_regression
"""
import asyncio
from unittest.mock import patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-drive-google-")

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

FAKE_META = {
    "d1": {"id": "d1", "name": "Notes.txt", "mimeType": "text/plain",
           "owners": [], "createdTime": "", "modifiedTime": ""},
}


async def run() -> None:
    app = GoogleTUI()
    patches = base_patches() + [
        patch.object(gauth, "get_file_metadata", side_effect=lambda svc, fid: FAKE_META[fid]),
        patch.object(gauth, "read_drive_text", return_value=("Notes.txt", "text/plain", "hello from Notes.txt")),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()
            app.action_goto_tab_drive()
            await pilot.pause()
            await pilot.press("down")
            await asyncio.sleep(0.5)
            await pilot.pause()

            assert app.drive_backend.source_key == "google", app.drive_backend.source_key
            f = app._drive_items_by_cid.get("d-d1")
            assert f is not None and f["is_folder"] is False, f

            body = "\n".join(str(line) for line in app.query_one("#drive-preview-text").lines)
            assert "Notes.txt" in body, body

    print("drive_google_regression PASSED")


if __name__ == "__main__":
    asyncio.run(run())
