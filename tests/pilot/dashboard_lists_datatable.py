"""Pilot scenario (ROADMAP P3): the Dashboard TODAY (events) and TASKS cards
render as DataTables — Tasks keeps its bold group-header rows, and selection
by row key still opens EventModal / TaskModal and resolves _selected_task.

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.dashboard_lists_datatable
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-dashlists-")

from google_tui.main import GoogleTUI, EventModal, TaskModal  # noqa: E402
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

            # TODAY card: one event row, When+Summary columns.
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

    print("dashboard_lists_datatable PASSED")


if __name__ == "__main__":
    asyncio.run(run())
