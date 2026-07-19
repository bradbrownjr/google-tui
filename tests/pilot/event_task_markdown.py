"""Pilot scenario: EventModal/TaskModal render their description/notes
through render.parse_feed_entry -> DocumentView instead of raw text
interpolation (ROADMAP P4, 2026-07-19) -- catches compose()/CSS mistakes
(mismatched widget ids, bad "#ev-desc #doc-title" selectors, etc.) that a
pure render.py unit test can't see, since those only surface once Textual
actually mounts the modal.

Usage: python -m tests.pilot.event_task_markdown
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-event-task-md-")

from textual.screen import ModalScreen  # noqa: E402

from google_tui.render import DocumentView  # noqa: E402
from google_tui.main import EventModal, TaskModal, GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

FAKE_EVENT = {
    "id": "ev1",
    "summary": "Team standup",
    "start": {"dateTime": "2026-07-20T09:00:00Z"},
    "end": {"dateTime": "2026-07-20T09:30:00Z"},
    "htmlLink": "https://calendar.example.com/ev1",
    "description": "# Agenda\n- status updates\n- blockers\n",
}
FAKE_TASK = {
    "id": "t1",
    "title": "Ship the release",
    "status": "needsAction",
    "due": "2026-07-21T00:00:00Z",
    "notes": "# Checklist\n- run tests\n- tag release\n",
}


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()

            app.push_screen(EventModal(FAKE_EVENT))
            await pilot.pause()
            assert isinstance(app.screen, ModalScreen)
            ev_desc = app.screen.query_one("#ev-desc", DocumentView)
            headings = [b for b in ev_desc.document.blocks if b.kind == "heading"]
            assert any("Agenda" in b.text for b in headings), ev_desc.document.blocks
            list_items = [b for b in ev_desc.document.blocks if b.kind == "list_item"]
            assert len(list_items) == 2, ev_desc.document.blocks
            app.pop_screen()
            await pilot.pause()

            app.push_screen(TaskModal(app.svc, FAKE_TASK, [FAKE_TASK]))
            await pilot.pause()
            assert isinstance(app.screen, ModalScreen)
            tk_desc = app.screen.query_one("#tk-desc", DocumentView)
            headings = [b for b in tk_desc.document.blocks if b.kind == "heading"]
            assert any("Checklist" in b.text for b in headings), tk_desc.document.blocks
            list_items = [b for b in tk_desc.document.blocks if b.kind == "list_item"]
            assert len(list_items) == 2, tk_desc.document.blocks
            app.pop_screen()
            await pilot.pause()

    print("event_task_markdown PASSED")


if __name__ == "__main__":
    asyncio.run(run())
