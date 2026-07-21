"""Pilot scenario: a transient error on the live threads().get() while opening
a reply must NOT crash the app — ComposeModal.on_mount falls back to the cached
thread summary and notifies, instead of letting the exception exit the app.

Regression for the class of `'object' object has no attribute 'users'` /
RefreshError / SSL crashes logged in the wild from ComposeModal's unguarded
on_mount fetch.

Usage: python -m tests.pilot.email_reply_fetch_error
"""
import asyncio
from unittest.mock import MagicMock, patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-replyerr-")

from textual.screen import ModalScreen  # noqa: E402

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import base_patches, applied  # noqa: E402


FAKE_FULL_THREAD = [{
    "id": "m1", "from": "Priya Rao <priya.rao@example.com>", "to": "me@example.com",
    "date": "Thu, 16 Jul 2026 09:00:00 +0000", "subject": "Weekend plans?",
    "body": "Are we still on for hiking Saturday morning?", "html_body": "",
    "label_ids": ["INBOX", "UNREAD"],
}]


def _fake_services_gmail_raises() -> dict:
    """A gmail service whose threads().get().execute() blows up — the exact
    call ComposeModal.on_mount makes for reply/forward prefill."""
    gmail = MagicMock()
    gmail.users.return_value.threads.return_value.get.return_value.execute.side_effect = \
        RuntimeError("simulated transient Gmail failure")
    return {"gmail": gmail, "calendar": object(), "drive": object(),
            "tasks": object(), "people": object()}


async def run() -> None:
    app = GoogleTUI()
    patches = [p for p in base_patches() if p.attribute != "services"] + [
        patch.object(gauth, "services", side_effect=_fake_services_gmail_raises),
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

            # 'r' opens ComposeModal, whose on_mount fetch will raise. The app
            # must survive and the modal must still open, prefilled from cache.
            await pilot.press("r")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert app.is_running, "app exited after a failed reply prefill fetch"
            assert isinstance(app.screen, ModalScreen), f"ComposeModal did not open: {app.screen!r}"
            subject = app.screen.query_one("#c-subject").value
            assert subject.startswith("Re:"), f"subject not prefilled from cache: {subject!r}"

    print("email_reply_fetch_error PASSED")


if __name__ == "__main__":
    asyncio.run(run())
