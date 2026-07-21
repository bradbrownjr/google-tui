"""Pilot scenario: attachments (ROADMAP P2). View + download a received
attachment from ThreadModal ('g'), and attach a local file on a reply.

Usage: python -m tests.pilot.mail_attachments
"""
import asyncio
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.isolate import isolate

home = isolate(prefix="google-tui-pilot-attach-")

from textual.screen import ModalScreen  # noqa: E402

from google_tui import gauth  # noqa: E402
from google_tui.main import EXPORT_DIR, AttachmentsModal, ComposeModal, GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

FAKE_THREADS = [
    {"threadId": "th1", "subject": "Report", "from": "priya@example.com",
     "date": "Thu, 16 Jul 2026 09:00:00 +0000", "count": 1, "unread": True,
     "snippet": "see attached", "labelIds": ["INBOX"]},
]

# gauth.get_thread returns messages with attachments — one inlined (no network
# needed to download), one that needs messages().attachments().get.
FAKE_MSGS = [{
    "id": "m1", "from": "priya@example.com", "to": "me@example.com",
    "subject": "Report", "date": "Thu, 16 Jul 2026 09:00:00 +0000",
    "body": "see attached", "html_body": "", "label_ids": ["INBOX"],
    "attachments": [
        {"message_id": "m1", "filename": "inline.txt", "mime_type": "text/plain",
         "size": 5, "attachment_id": None,
         "inline_data": base64.urlsafe_b64encode(b"hello").decode()},
        {"message_id": "m1", "filename": "report.pdf", "mime_type": "application/pdf",
         "size": 2048, "attachment_id": "att-1", "inline_data": None},
    ],
}]

_GMAIL_THREAD = {"messages": [{"id": "m1", "payload": {"headers": [
    {"name": "From", "value": "priya@example.com"},
    {"name": "To", "value": "me@example.com"},
    {"name": "Subject", "value": "Report"},
    {"name": "Date", "value": "Thu, 16 Jul 2026 09:00:00 +0000"},
], "mimeType": "text/plain", "body": {"data": ""}}}]}


def _fake_services() -> dict:
    gmail = MagicMock()
    gmail.users.return_value.threads.return_value.get.return_value.execute.return_value = _GMAIL_THREAD
    return {"gmail": gmail, "calendar": object(), "drive": object(),
            "tasks": object(), "people": object()}


async def run() -> None:
    app = GoogleTUI()
    download = MagicMock(return_value=b"PDFDATA")
    reply_to = MagicMock(return_value=None)
    patches = [p for p in base_patches()
               if p.attribute not in ("services", "list_threads", "get_thread", "reply_to")] + [
        patch.object(gauth, "services", side_effect=_fake_services),
        patch.object(gauth, "list_threads", return_value=(FAKE_THREADS, None)),
        patch.object(gauth, "get_thread", return_value=FAKE_MSGS),
        patch.object(gauth, "download_attachment", download),
        patch.object(gauth, "reply_to", reply_to),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            app.action_goto_tab_mail()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")          # open ThreadModal
            await asyncio.sleep(0.5)
            await pilot.pause()

            # --- 'g' opens the attachments viewer ---
            await pilot.press("g")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert isinstance(app.screen, AttachmentsModal), f"attachments modal not open: {app.screen!r}"
            assert len(app.screen.attachments) == 2

            # Download the INLINE attachment (index 0) — no network, written from
            # the inlined bytes.
            att_screen = app.screen
            att_screen._download(att_screen.query_one("#att-list").children[0])
            await asyncio.sleep(0.4)
            await pilot.pause()
            saved = EXPORT_DIR / "inline.txt"
            assert saved.is_file(), f"inline attachment not saved to {saved}"
            assert saved.read_bytes() == b"hello"

            # Download the NON-inline one (index 1) — goes through download_attachment.
            att_screen._download(att_screen.query_one("#att-list").children[1])
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert download.called, "download_attachment not called for non-inline part"
            assert download.call_args.args[1:] == ("m1", "att-1")
            assert (EXPORT_DIR / "report.pdf").read_bytes() == b"PDFDATA"

            app.screen.dismiss(None)            # close attachments modal
            await pilot.pause()
            app.screen.dismiss(None)            # close ThreadModal
            await pilot.pause()

            # --- Compose reply + attach a local file ---
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("r")
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert isinstance(app.screen, ComposeModal), f"compose not open: {app.screen!r}"
            f = home / "myfile.txt"
            f.write_text("payload")
            app.screen.query_one("#c-attach").value = str(f)
            app.screen._add_attachment()
            await pilot.pause()
            assert app.screen._attachments == [str(f)], f"attach not recorded: {app.screen._attachments}"
            app.screen._send_now()
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert reply_to.called, "reply not sent"
            assert reply_to.call_args.kwargs.get("attachments") == [str(f)], \
                f"attachment not passed to reply_to: {reply_to.call_args}"

    print("mail_attachments PASSED")


if __name__ == "__main__":
    asyncio.run(run())
