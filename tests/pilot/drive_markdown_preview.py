"""Pilot scenario: a ".md" Drive file switches the preview pane from the
plain RichLog to the Markdown-aware DocumentView (ROADMAP P4, 2026-07-19).

Usage: python -m tests.pilot.drive_markdown_preview
"""
import asyncio
from unittest.mock import patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-drive-markdown-")

from google_tui import gauth  # noqa: E402
from google_tui.render import DocumentView  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from textual.widgets import RichLog  # noqa: E402

FAKE_DRIVE = [{"id": "d1", "name": "README.md", "mimeType": "text/markdown",
               "modifiedTime": "", "parents": ["root"], "size": "64"}]
FAKE_META = {
    "d1": {"id": "d1", "name": "README.md", "mimeType": "text/markdown",
           "owners": [], "createdTime": "", "modifiedTime": ""},
}
MARKDOWN_BODY = "# Project Notes\n\n- first item\n- second item\n"


async def run() -> None:
    app = GoogleTUI()
    patches = base_patches() + [
        patch.object(gauth, "list_drive", return_value=(FAKE_DRIVE, None)),
        patch.object(gauth, "get_file_metadata", side_effect=lambda svc, fid: FAKE_META[fid]),
        patch.object(gauth, "read_drive_text", return_value=("README.md", "text/markdown", MARKDOWN_BODY)),
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

            doc_widget = app.query_one("#drive-preview-doc", DocumentView)
            text_widget = app.query_one("#drive-preview-text", RichLog)
            assert not doc_widget.has_class("hidden"), "DocumentView should be visible for a .md file"
            assert text_widget.has_class("hidden"), "RichLog should be hidden for a .md file"
            assert doc_widget.document is not None
            headings = [b for b in doc_widget.document.blocks if b.kind == "heading"]
            assert any("Project Notes" in b.text for b in headings), doc_widget.document.blocks
            list_items = [b for b in doc_widget.document.blocks if b.kind == "list_item"]
            assert len(list_items) == 2, doc_widget.document.blocks

    print("drive_markdown_preview PASSED")


if __name__ == "__main__":
    asyncio.run(run())
