"""Pilot scenario: typing an sftp:// address in the Browser tab redirects to
the Drive tab and opens RemoteHostModal pre-filled from the URL, instead of
fetching inline -- Browser's ftp:// handling was fully reassigned to Drive
(CHANGELOG [2026-07-18]). Also a regression check for the latent bug this
fixed: sftp:// used to fall through _classify_address into a literal web
search for the whole URL string.

Usage: python -m tests.pilot.browser_sftp_redirect
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-browser-")

from google_tui.main import GoogleTUI, RemoteHostModal  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()
            app.action_goto_tab_browser()
            await pilot.pause()

            url_input = app.query_one("#browser-url")
            url_input.value = "sftp://example.com/srv/data"
            url_input.focus()
            await pilot.pause()
            await pilot.press("enter")
            await asyncio.sleep(0.3)
            await pilot.pause()

            assert app._main_tabs().active == "tab-drive", app._main_tabs().active
            assert isinstance(app.screen, RemoteHostModal), app.screen.__class__.__name__
            modal = app.screen
            assert modal._host == "example.com" and modal._protocol == "ssh" and modal._port == 22, \
                (modal._host, modal._protocol, modal._port)

    print("browser_sftp_redirect PASSED")


if __name__ == "__main__":
    asyncio.run(run())
