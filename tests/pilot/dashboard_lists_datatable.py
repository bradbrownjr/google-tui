"""Pilot scenario (ROADMAP P3): the Dashboard TIME card's #event-list (today's
events, folded into TIME from the old standalone TODAY card 2026-07-23) and
TASKS card render as DataTables — Tasks keeps its bold group-header rows, and
selection by row key still opens EventModal / TaskModal and resolves
_selected_task.

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.dashboard_lists_datatable
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-dashlists-")

from google_tui.main import (  # noqa: E402
    GoogleTUI, EventModal, TaskModal, ThreadModal, NewsEntryModal)
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from textual.widgets import DataTable  # noqa: E402


def _keys(table) -> list[str]:
    return [rk.value for rk in table.rows]


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(160, 48)) as pilot:
            await asyncio.sleep(1.5)
            await pilot.pause()
            app.action_goto_tab_dashboard()
            await pilot.pause()

            events = app.query_one("#event-list", DataTable)
            tasks = app.query_one("#task-list", DataTable)

            # TIME card's events list: one event row, When+Summary columns.
            assert "e-ev1" in _keys(events), _keys(events)
            summary = " ".join(c.plain if hasattr(c, "plain") else str(c)
                               for c in events.get_row("e-ev1"))
            assert "Team standup" in summary, summary

            # TASKS card: a bold group-header row plus the task row.
            assert "hdr-none" in _keys(tasks), _keys(tasks)
            assert "k-list1-t1" in _keys(tasks), _keys(tasks)
            header_cell = tasks.get_row("hdr-none")[0]
            assert "bold" in str(header_cell.style), header_cell.style

            # _selected_task resolves from the row cursor (was highlighted_child).
            tasks.focus()
            tasks.move_cursor(row=tasks.get_row_index("k-list1-t1"))
            await pilot.pause()
            sel = app._selected_task()
            assert sel and sel["id"] == "t1", sel
            # A cursor parked on a header row selects nothing.
            tasks.move_cursor(row=tasks.get_row_index("hdr-none"))
            await pilot.pause()
            assert app._selected_task() is None

            # Enter on the task row opens TaskModal.
            tasks.move_cursor(row=tasks.get_row_index("k-list1-t1"))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TaskModal), type(app.screen).__name__
            app.pop_screen()
            await pilot.pause()

            # Enter on the event row opens EventModal.
            events.focus()
            events.move_cursor(row=events.get_row_index("e-ev1"))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EventModal), type(app.screen).__name__
            app.pop_screen()
            await pilot.pause()

            # MAIL mini-card: bold unread-count header + a thread row; Enter on
            # the thread row opens ThreadModal.
            dmail = app.query_one("#dash-mail-list", DataTable)
            assert "dm-open" in _keys(dmail) and "dm-th1" in _keys(dmail), _keys(dmail)
            dmail.focus()
            dmail.move_cursor(row=dmail.get_row_index("dm-th1"))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ThreadModal), type(app.screen).__name__
            app.pop_screen()
            await pilot.pause()

            # NEWS mini-card: inject an entry, Enter opens NewsEntryModal.
            app._apply_news_data([{
                "id": "dn1", "title": "Headline one", "link": "http://x/dn1",
                "summary": "s", "published": "2026-07-21T10:00:00Z", "feed_title": "Feed"}])
            await pilot.pause()
            dnews = app.query_one("#dash-news-list", DataTable)
            dn_keys = [k for k in _keys(dnews) if k.startswith("dn-")]
            assert dn_keys, _keys(dnews)
            dnews.focus()
            dnews.move_cursor(row=dnews.get_row_index(dn_keys[0]))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NewsEntryModal), type(app.screen).__name__
            app.pop_screen()
            await pilot.pause()

    print("dashboard_lists_datatable PASSED")


if __name__ == "__main__":
    asyncio.run(run())
