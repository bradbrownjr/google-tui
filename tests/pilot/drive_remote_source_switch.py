"""Pilot scenario: switching the Drive-tab source picker to a saved FTP host
routes listing/preview/download through drive_sources.FtpSource, and the
preview cache is namespaced per-source (two hosts sharing a path don't
collide) -- see CHANGELOG [2026-07-18].

Usage: python -m tests.pilot.drive_remote_source_switch
"""
import asyncio

from tests.isolate import isolate

_ISOLATED_HOME = isolate(prefix="google-tui-pilot-drive-remote-")

from google_tui import drive_sources, remote_creds  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from unittest.mock import patch  # noqa: E402

FAKE_FTP_FILES = [
    {"id": "/pub/readme.txt", "name": "readme.txt", "mimeType": "text/plain",
     "is_folder": False, "modifiedTime": "", "size": "42"},
]


async def run() -> None:
    app = GoogleTUI()
    patches = base_patches() + [
        patch.object(remote_creds, "list_hosts", return_value=[("ftp", "ftp.example.com", 21)]),
        patch.object(remote_creds, "get", return_value=("anonymous", "anonymous@")),
        patch.object(drive_sources.FtpSource, "list_children", return_value=(FAKE_FTP_FILES, None)),
        patch.object(drive_sources.FtpSource, "get_metadata",
                      return_value={"name": "readme.txt", "mimeType": "text/plain", "size": "42",
                                    "owner": None, "createdTime": None, "modifiedTime": ""}),
        patch.object(drive_sources.FtpSource, "read_preview_text", return_value="hello from ftp"),
        patch.object(drive_sources.FtpSource, "download", return_value=("readme.txt", b"hello from ftp")),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()
            app.action_goto_tab_drive()
            await pilot.pause()
            app._refresh_drive_source_select()
            await pilot.pause()

            sel = app.query_one("#drive-source-select")
            source_key = drive_sources.source_key_for("ftp", "ftp.example.com", 21)
            sel.value = source_key
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert app.drive_backend.source_key == source_key, app.drive_backend.source_key

            await pilot.press("down")
            await asyncio.sleep(0.5)
            await pilot.pause()
            body = "\n".join(str(line) for line in app.query_one("#drive-preview-text").lines)
            assert "hello from ftp" in body, body

            cached = app._cache.get(f"drive_file_text:{source_key}", "/pub/readme.txt")
            assert cached is not None and cached["text"] == "hello from ftp", cached

            await pilot.press("d")
            await asyncio.sleep(0.5)
            await pilot.pause()

    downloaded = list((_ISOLATED_HOME / "documents" / "google-tui").glob("*"))
    assert downloaded and downloaded[0].name == "readme.txt", downloaded
    assert downloaded[0].read_bytes() == b"hello from ftp"

    print("drive_remote_source_switch PASSED")


if __name__ == "__main__":
    asyncio.run(run())
