"""Pilot scenario: multi-select bulk actions (ROADMAP P2). Check threads with
'x', open the bulk chooser with 'X', and archive the checked set at once.

Usage: python -m tests.pilot.mail_multiselect
"""
import asyncio
from unittest.mock import MagicMock, patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-multisel-")

from google_tui import gauth  # noqa: E402
from google_tui.main import BulkActionModal, GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

FAKE_THREADS = [
    {"threadId": f"th{i}", "subject": f"Subject {i}", "from": f"p{i}@example.com",
     "date": "Thu, 16 Jul 2026 09:00:00 +0000", "count": 1, "unread": True,
     "snippet": f"snippet {i}", "labelIds": ["INBOX"]}
    for i in range(1, 4)
]


async def run() -> None:
    app = GoogleTUI()
    archive = MagicMock(return_value={})
    patches = [p for p in base_patches() if p.attribute != "list_threads"] + [
        patch.object(gauth, "list_threads", return_value=(FAKE_THREADS, None)),
        patch.object(gauth, "archive_thread", archive),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            app.action_goto_tab_mail()
            await pilot.pause()
            assert len(app.query_one("#email-list").children) >= 3, "need >=3 threads"

            await pilot.press("down")           # highlight row 1
            await pilot.pause()
            await pilot.press("x")               # check th1, cursor -> row 2
            await pilot.pause()
            await pilot.press("x")               # check th2, cursor -> row 3
            await pilot.pause()
            assert app._email_selected == {"th1", "th2"}, \
                f"unexpected selection: {app._email_selected}"

            # 'X' opens the bulk chooser.
            await pilot.press("X")
            await asyncio.sleep(0.2)
            await pilot.pause()
            assert isinstance(app.screen, BulkActionModal), f"bulk modal not open: {app.screen!r}"
            assert app.screen.count == 2

            app.screen.dismiss("archive")
            await asyncio.sleep(0.5)
            await pilot.pause()
            assert archive.call_count == 2, f"expected 2 archive calls, got {archive.call_count}"
            archived_ids = {c.args[1] for c in archive.call_args_list}
            assert archived_ids == {"th1", "th2"}, f"wrong ids archived: {archived_ids}"
            assert app._email_selected == set(), "selection not cleared after bulk action"

    print("mail_multiselect PASSED")


if __name__ == "__main__":
    asyncio.run(run())
