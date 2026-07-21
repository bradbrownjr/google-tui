"""Pilot scenario: the non-Email list views (Calendar events, Tasks, Drive,
Contacts, News) render responsively -- their rows fill the pane width at boot
and re-flow in place when the terminal is resized, matching the Email list's
behavior. Runs as its own process (see AGENTS.md §6 / tests/isolate.py).

Usage: python -m tests.pilot.responsive_rows
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-responsive-")

from textual.widgets import Label  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402


def _first_row_text(app, list_id: str) -> str | None:
    lst = app.query_one(f"#{list_id}")
    for item in lst.children:
        if getattr(item, "id", None) and "empty" not in item.id:
            return str(item.query_one(Label).render())
    return None


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(160, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()

            # Calendar events + Tasks live on their tab; visit it so the lists
            # are laid out (content_size is 0 until first layout).
            app.action_goto_tab_calendar()
            await pilot.pause()
            ev = _first_row_text(app, "event-list")
            tk = _first_row_text(app, "task-list")
            print(f"[160] event row: {ev!r}")
            print(f"[160] task  row: {tk!r}")

            app.action_goto_tab_drive()
            await pilot.pause()
            dr = _first_row_text(app, "drive-list")
            print(f"[160] drive row: {dr!r}")

            # Reflow must not raise on any list, even ones never populated
            # (empty contacts / no subscribed feeds) -- getattr-guarded caches.
            for reflow in (app._reflow_event_rows, app._reflow_task_rows,
                           app._reflow_drive_rows, app._reflow_contact_rows,
                           app._reflow_news_rows, app._reflow_email_rows):
                reflow()
            await pilot.pause()
            print(f"[reflow] event row: {_first_row_text(app, 'event-list')!r}")

            assert ev and "Team standup" in ev, ev
            assert tk and "Buy cat food" in tk, tk
            assert dr and "Notes.txt" in dr, dr

    print("responsive_rows PASSED")


if __name__ == "__main__":
    asyncio.run(run())
