"""Pilot scenario: highlighting a thread and pressing 'r' opens the reply
ComposeModal. Follows the exact pattern documented in AGENTS.md §6.

Usage: python -m tests.pilot.email_reply_modal
"""
import asyncio
from unittest.mock import MagicMock, patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-email-")

from textual.screen import ModalScreen  # noqa: E402

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402


FAKE_FULL_THREAD = [{
    "id": "m1", "from": "Priya Rao <priya.rao@example.com>", "to": "me@example.com",
    "date": "Thu, 16 Jul 2026 09:00:00 +0000", "subject": "Weekend plans?",
    "body": "Are we still on for hiking Saturday morning?", "html_body": "",
    "label_ids": ["INBOX", "UNREAD"],
}]

# ComposeModal's reply/reply-all prefill (main.py's ComposeModal.on_mount)
# hits the raw Gmail API object directly (format="full", to pick up To/Cc
# headers for reply-all) rather than going through gauth.get_thread -- so
# the fake "gmail" service here needs the actual users().threads().get()
# .execute() chain wired up, not just a gauth-level patch.
_FAKE_GMAIL_THREAD_RESPONSE = {
    "messages": [{
        "id": "m1",
        "payload": {
            "headers": [
                {"name": "From", "value": "Priya Rao <priya.rao@example.com>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "Weekend plans?"},
                {"name": "Date", "value": "Thu, 16 Jul 2026 09:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    }],
}


def _fake_services_with_gmail_thread() -> dict:
    gmail = MagicMock()
    gmail.users.return_value.threads.return_value.get.return_value.execute.return_value = \
        _FAKE_GMAIL_THREAD_RESPONSE
    return {"gmail": gmail, "calendar": object(), "drive": object(), "tasks": object(), "people": object()}


async def run() -> None:
    app = GoogleTUI()
    patches = [p for p in base_patches() if p.attribute != "services"] + [
        patch.object(gauth, "services", side_effect=_fake_services_with_gmail_thread),
        patch.object(gauth, "get_thread", return_value=FAKE_FULL_THREAD),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            app.action_goto_tab_mail()
            await pilot.pause()
            lst = app.query_one("#email-list")
            assert lst.children, "email list is empty, nothing to select"
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await asyncio.sleep(0.5)
            await pilot.pause()
            assert isinstance(app.screen, ModalScreen), f"ThreadModal did not open: {app.screen!r}"

            await pilot.press("r")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert isinstance(app.screen, ModalScreen), f"ComposeModal did not open: {app.screen!r}"
            body_widget = app.screen.query_one("#c-body")
            assert body_widget is not None

    print("email_reply_modal PASSED")


if __name__ == "__main__":
    asyncio.run(run())
