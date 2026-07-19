"""Pilot scenario: the app boots cleanly against a fabricated dataset and
every main tab is reachable. Runs as its own process -- see AGENTS.md §6 /
tests/isolate.py for why (chaining multiple GoogleTUI() instances in one
process caused a real DuplicateIds crash previously).

Usage: python -m tests.pilot.startup_smoke
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-startup-")

from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()

            tabs = app.query_one("#main-tabs")
            tab_ids = {t.id for t in tabs.query("TabPane")}
            for expected in ("tab-mail", "tab-dashboard", "tab-calendar",
                              "tab-drive", "tab-browser", "tab-settings",
                              "tab-news", "tab-navigation", "tab-contacts"):
                assert expected in tab_ids, f"missing {expected} in {tab_ids}"

            for action in (
                app.action_goto_tab_dashboard,
                app.action_goto_tab_calendar,
                app.action_goto_tab_drive,
                app.action_goto_tab_browser,
                app.action_goto_tab_news,
                app.action_goto_tab_navigation,
                app.action_goto_tab_contacts,
                app.action_goto_tab_settings,
                app.action_goto_tab_mail,
            ):
                action()
                await pilot.pause()

    print("startup_smoke PASSED")


if __name__ == "__main__":
    asyncio.run(run())
