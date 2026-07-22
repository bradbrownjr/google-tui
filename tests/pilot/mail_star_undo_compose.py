"""Pilot scenario: ROADMAP P2 mail-completeness batch —
  * star / unstar a thread from the Email list ('*' / action_star),
  * Undo (Ctrl+Z / action_undo) reversing a trash/archive,
  * ComposeModal's new Cc / Bcc fields + Save Draft button.

Follows the AGENTS.md §6 pilot pattern (own subprocess, all gauth mocked).

Usage: python -m tests.pilot.mail_star_undo_compose
"""
import asyncio
from unittest.mock import MagicMock, patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-starundo-")

from textual.screen import ModalScreen  # noqa: E402

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from textual.widgets import DataTable  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

# A single inbox thread carrying labelIds so the ★ column has state to toggle.
FAKE_THREADS = [
    {"threadId": "th1", "subject": "Weekend plans?",
     "from": "Priya Rao <priya.rao@example.com>",
     "date": "Thu, 16 Jul 2026 09:00:00 +0000", "count": 1, "unread": True,
     "snippet": "Are we still on for hiking?", "labelIds": ["INBOX", "UNREAD"]},
]

# ComposeModal reply-all prefill reads the raw gmail thread for To/Cc headers.
_GMAIL_THREAD = {
    "messages": [{
        "id": "m1",
        "payload": {"headers": [
            {"name": "From", "value": "Priya Rao <priya.rao@example.com>"},
            {"name": "To", "value": "me@example.com"},
            {"name": "Cc", "value": "hiking-club@example.com"},
            {"name": "Subject", "value": "Weekend plans?"},
            {"name": "Date", "value": "Thu, 16 Jul 2026 09:00:00 +0000"},
        ], "mimeType": "text/plain", "body": {"data": ""}},
    }],
}


def _fake_services() -> dict:
    gmail = MagicMock()
    gmail.users.return_value.threads.return_value.get.return_value.execute.return_value = _GMAIL_THREAD
    return {"gmail": gmail, "calendar": object(), "drive": object(),
            "tasks": object(), "people": object()}


async def _wait_worker() -> None:
    # action_star/action_undo run their gauth write on an exclusive worker
    # thread; give it a moment to land before asserting.
    await asyncio.sleep(0.4)


async def run() -> None:
    app = GoogleTUI()
    modify_labels = MagicMock(return_value={})
    untrash = MagicMock(return_value={})
    create_draft = MagicMock(return_value={"id": "draft1"})
    patches = [p for p in base_patches()
               if p.attribute not in ("services", "list_threads")] + [
        patch.object(gauth, "services", side_effect=_fake_services),
        patch.object(gauth, "list_threads", return_value=(FAKE_THREADS, None)),
        patch.object(gauth, "modify_labels", modify_labels),
        patch.object(gauth, "untrash_thread", untrash),
        patch.object(gauth, "create_draft", create_draft),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            app.action_goto_tab_mail()
            await pilot.pause()
            assert app.query_one("#email-list", DataTable).row_count, "email list is empty"
            await pilot.pause()

            # --- Compose reply-all: Cc/Bcc fields + Save Draft ---
            # (done first: action_star/undo below each fire a mail refresh that
            # rebuilds the list and drops the highlight.)
            await pilot.press("a")
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert isinstance(app.screen, ModalScreen), f"ComposeModal not open: {app.screen!r}"
            cc = app.screen.query_one("#c-cc")
            app.screen.query_one("#c-bcc")            # exists or raises
            app.screen.query_one("#save-draft")       # exists or raises
            assert "hiking-club@example.com" in cc.value, \
                f"reply-all didn't prefill Cc: {cc.value!r}"

            app.screen._save_draft()
            await pilot.pause()
            assert create_draft.called, "Save Draft didn't call create_draft"
            assert not isinstance(app.screen, ModalScreen), "compose modal stayed open after draft"

            # --- Star from the list ---
            # Re-select: the save-draft refresh rebuilt the list.
            await asyncio.sleep(0.4)
            await pilot.press("down")
            await pilot.pause()
            app.action_star()
            await _wait_worker()
            assert modify_labels.called, "action_star didn't call modify_labels"
            kw = modify_labels.call_args.kwargs
            assert kw.get("add") == ["STARRED"], f"expected add STARRED, got {kw}"
            modify_labels.reset_mock()

            # --- Undo an archive (reverse == re-add INBOX) ---
            app._record_mail_undo("archive", "th1")
            app.action_undo()
            assert getattr(app, "_pending_undo", None) is None, "undo not consumed"
            await _wait_worker()
            assert modify_labels.called, "action_undo didn't call modify_labels"
            assert modify_labels.call_args.kwargs.get("add") == ["INBOX"]

            # --- Undo a trash (reverse == untrash) ---
            app._record_mail_undo("trash", "th1")
            app.action_undo()
            await _wait_worker()
            assert untrash.called, "trash undo didn't call untrash_thread"

    print("mail_star_undo_compose PASSED")


if __name__ == "__main__":
    asyncio.run(run())
