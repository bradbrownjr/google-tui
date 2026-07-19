"""Pilot scenario: the Dashboard tab's four external cards (WEATHER, STOCKS,
WORD OF THE DAY, PICTURE OF THE DAY -- ROADMAP P4, 2026-07-19) populate from
their fetchers (mocked here, real ones live in fetchers.py) once enabled +
configured, and the two link-bearing cards' Enter action opens the right URL
in the Browser tab. All four cards start disabled by default (Settings.
dashboard_panes_enabled), so this scenario explicitly opts them in --
unlike every other pilot scenario, which never touches them at all.

Usage: python -m tests.pilot.dashboard_external_cards
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-dash-extras-")

from textual.widgets import Input, Label, ListView  # noqa: E402

from google_tui.main import DASH_PANE_IDS, GoogleTUI  # noqa: E402
from tests.pilot.fakes import (  # noqa: E402
    applied, base_patches, FAKE_STOCKS, FAKE_WEATHER, FAKE_WIKI_POTD, FAKE_WORD_OF_DAY,
)


def _item_text(list_view: ListView, item_id: str) -> str:
    item = list_view.query_one(f"#{item_id}")
    return str(item.query_one(Label).content)


async def run() -> None:
    app = GoogleTUI()
    # Must happen before run_test (which triggers compose()/on_mount) --
    # __init__ already loaded self.settings, so this is the one window where
    # mutating it is picked up by both the initial compose() checklist and
    # on_mount's _apply_dashboard_panes_enabled.
    app.settings.dashboard_panes_enabled = list(DASH_PANE_IDS)
    app.settings.weather_location = "Testville, TS"
    app.settings.stock_symbols = ["AAPL"]

    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)  # startup live refresh (fetches all four, mocked)
            await pilot.pause()

            app.action_goto_tab_dashboard()
            await pilot.pause()

            weather_text = _item_text(app.query_one("#dash-weather-list", ListView), "dash-weather-info")
            assert FAKE_WEATHER["location"] in weather_text, weather_text
            assert "72" in weather_text, weather_text

            stocks_text = _item_text(app.query_one("#dash-stocks-list", ListView), "ds-AAPL")
            assert "AAPL" in stocks_text and "123.45" in stocks_text, stocks_text

            word_text = _item_text(app.query_one("#dash-word-list", ListView), "dw-open")
            assert FAKE_WORD_OF_DAY["word"] in word_text, word_text

            potd_text = _item_text(app.query_one("#dash-potd-list", ListView), "dp-open")
            assert FAKE_WIKI_POTD["title"] in potd_text, potd_text

            # Enter on the WORD OF THE DAY card's single row opens its link
            # in the Browser tab (no in-terminal detail view for either of
            # these two cards -- see _open_dashboard_link). Driven through a
            # real focus + keypress (not a hand-built ListView.Selected)
            # so this exercises the actual on_list_view_selected dispatch
            # Textual's own ListView key handling triggers.
            app._focus_dash_pane("dash-word")
            await pilot.pause()
            await pilot.press("down")  # highlight the (only) row -- ListView.index starts None
            await pilot.press("enter")
            await pilot.pause()
            assert app._main_tabs().active == "tab-browser", app._main_tabs().active
            assert app.query_one("#browser-url", Input).value == FAKE_WORD_OF_DAY["link"]

            # Same for PICTURE OF THE DAY, from the Dashboard tab again.
            app.action_goto_tab_dashboard()
            await pilot.pause()
            app._focus_dash_pane("dash-potd")
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app._main_tabs().active == "tab-browser", app._main_tabs().active
            assert app.query_one("#browser-url", Input).value == FAKE_WIKI_POTD["link"]

    print("dashboard_external_cards PASSED")


if __name__ == "__main__":
    asyncio.run(run())
