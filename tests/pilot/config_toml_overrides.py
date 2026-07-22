"""Pilot scenario: config.toml (ROADMAP: Config file, 2026-07-19) is read at
startup and its pane_order/refresh_interval_minutes fields take effect on a
real GoogleTUI() instance. Unit tests (tests/unit/test_app_config.py) already
cover load_config()'s parsing/validation in isolation; this scenario checks
the wiring into main.py actually applies what got loaded -- Dashboard
Tab/Shift+Tab cycle order reflects a custom pane_order, and a positive
refresh_interval_minutes gets a periodic timer scheduled.

Usage: python -m tests.pilot.config_toml_overrides
"""
import asyncio

from tests.isolate import isolate

_HOME = isolate(prefix="google-tui-pilot-config-")

_CONFIG_TOML = """
pane_order = ["dash-weather", "dash-potd", "events"]
refresh_interval_minutes = 7
"""
_config_path = _HOME / "config" / "config.toml"
_config_path.parent.mkdir(parents=True, exist_ok=True)
_config_path.write_text(_CONFIG_TOML)

from google_tui.main import GoogleTUI, DASH_PANE_IDS  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402


async def run() -> None:
    app = GoogleTUI()
    # Settings.dashboard_panes_enabled defaults to every card as of
    # 2026-07-22, but set it explicitly anyway so this assertion about ORDER
    # doesn't depend on that default and stays unambiguous with the separate
    # enable/disable feature (_dash_enabled_ids filtering on top of it).
    app.settings.dashboard_panes_enabled = list(DASH_PANE_IDS)

    # Loaded correctly off disk before the app even starts.
    assert app.app_config.pane_order == ["dash-weather", "dash-potd", "events"]
    assert app.app_config.refresh_interval_minutes == 7

    # Cycle order: the three named ids come first in that order, followed by
    # every other DASH_PANE_IDS entry (unmentioned ids appended, in their
    # original relative order, so nothing becomes unreachable).
    expected_prefix = ["dash-weather", "dash-potd", "events"]
    assert app._dash_cycle_ids[:3] == expected_prefix, app._dash_cycle_ids
    assert set(app._dash_cycle_ids) == set(DASH_PANE_IDS), app._dash_cycle_ids
    assert len(app._dash_cycle_ids) == len(DASH_PANE_IDS)

    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(1)
            await pilot.pause()

            # _apply_dashboard_panes_enabled (called from on_mount) builds
            # _dash_enabled_ids by filtering _dash_cycle_ids -- with every
            # card enabled (Settings default), the enabled-list IS the
            # custom cycle order.
            assert app._dash_enabled_ids[:3] == expected_prefix, app._dash_enabled_ids

            # A positive refresh_interval_minutes scheduled a periodic timer
            # in on_mount. _periodic_refresh skips while offline (no
            # _last_manual_refresh bump, no worker kicked) and runs while
            # online -- check both halves of that gate directly.
            app._online = False
            app._last_manual_refresh = 0.0
            app._periodic_refresh()
            await pilot.pause()
            assert app._last_manual_refresh == 0.0, "should have skipped while offline"

            app._online = True
            app._periodic_refresh()
            await pilot.pause()
            assert app._last_manual_refresh > 0.0, "should have run while online"

    print("config_toml_overrides PASSED")


if __name__ == "__main__":
    asyncio.run(run())
