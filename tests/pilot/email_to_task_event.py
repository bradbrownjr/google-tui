"""Pilot scenario: create a Google Task ('t') and a Calendar event ('e')
from the highlighted Email thread — prefilled from the subject with a link
back to the thread in the notes/description.

Usage: python -m tests.pilot.email_to_task_event
"""
import asyncio
from unittest.mock import MagicMock, patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-mail2te-")

from textual.screen import ModalScreen  # noqa: E402

from google_tui import gauth  # noqa: E402
from google_tui.main import CreateEventModal, EmailToTaskModal, GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

FAKE_THREADS = [
    {"threadId": "th1", "subject": "Weekend plans?",
     "from": "Priya Rao <priya.rao@example.com>",
     "date": "Thu, 16 Jul 2026 09:00:00 +0000", "count": 1, "unread": True,
     "snippet": "Are we still on for hiking Saturday?", "labelIds": ["INBOX"]},
]

PERMALINK = "https://mail.google.com/mail/u/0/#all/th1"


async def run() -> None:
    app = GoogleTUI()
    create_task = MagicMock(return_value={"id": "task1"})
    create_event = MagicMock(return_value={"id": "ev1"})
    patches = [p for p in base_patches() if p.attribute != "list_threads"] + [
        patch.object(gauth, "list_threads", return_value=(FAKE_THREADS, None)),
        patch.object(gauth, "create_task", create_task),
        patch.object(gauth, "create_event", create_event),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            app.action_goto_tab_mail()
            await pilot.pause()
            assert app.query_one("#email-list").children, "email list empty"
            await pilot.press("down")
            await pilot.pause()

            # --- Email → Task ('t') ---
            await pilot.press("t")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert isinstance(app.screen, EmailToTaskModal), f"task modal not open: {app.screen!r}"
            assert app.screen.query_one("#ett-title").value == "Weekend plans?"
            notes = app.screen.query_one("#ett-notes").text
            assert PERMALINK in notes, f"permalink missing from task notes: {notes!r}"
            app.screen._try_create()
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert create_task.called, "create_task not called"
            ct = create_task.call_args
            assert ct.args[2] == "Weekend plans?", f"task title wrong: {ct}"
            assert PERMALINK in (ct.kwargs.get("notes") or ""), f"task notes wrong: {ct}"
            assert not isinstance(app.screen, ModalScreen), "task modal stayed open"

            # --- Email → Event ('e') ---
            await asyncio.sleep(0.3)
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("e")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert isinstance(app.screen, CreateEventModal), f"event modal not open: {app.screen!r}"
            assert app.screen.query_one("#ce-title").value == "Weekend plans?"
            assert PERMALINK in app.screen.description
            app.screen._try_create()
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert create_event.called, "create_event not called"
            ce = create_event.call_args
            assert ce.args[1] == "Weekend plans?", f"event summary wrong: {ce}"
            assert PERMALINK in (ce.kwargs.get("description") or ""), f"event desc wrong: {ce}"

    print("email_to_task_event PASSED")


if __name__ == "__main__":
    asyncio.run(run())
