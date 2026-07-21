"""Pilot scenario: snooze a thread from the list (ROADMAP P2) and resurface
it. Snooze removes INBOX + records a remind-at; _resurface_due_snoozes re-adds
INBOX once the time has passed.

Usage: python -m tests.pilot.mail_snooze
"""
import asyncio
import datetime as dt
from unittest.mock import MagicMock, patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-snooze-")

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI, SnoozeModal  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

FAKE_THREADS = [
    {"threadId": "th1", "subject": "Weekend plans?", "from": "priya@example.com",
     "date": "Thu, 16 Jul 2026 09:00:00 +0000", "count": 1, "unread": True,
     "snippet": "hiking?", "labelIds": ["INBOX"]},
]


async def run() -> None:
    app = GoogleTUI()
    modify = MagicMock(return_value={})
    patches = [p for p in base_patches() if p.attribute != "list_threads"] + [
        patch.object(gauth, "list_threads", return_value=(FAKE_THREADS, None)),
        patch.object(gauth, "modify_labels", modify),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            app.action_goto_tab_mail()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()

            # --- Snooze: 'z' opens the modal, a preset removes INBOX + records ---
            await pilot.press("z")
            await asyncio.sleep(0.2)
            await pilot.pause()
            assert isinstance(app.screen, SnoozeModal), f"snooze modal not open: {app.screen!r}"
            await pilot.click("#sn-3h")
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert modify.called, "snooze didn't call modify_labels"
            assert modify.call_args.kwargs.get("remove") == ["INBOX"], \
                f"snooze should remove INBOX: {modify.call_args}"
            assert "th1" in app.settings.snoozed, "remind-at not persisted"

            # --- Resurface: a past remind-at re-adds INBOX and clears the store ---
            modify.reset_mock()
            past = (dt.datetime.now(app._snooze_tz()) - dt.timedelta(hours=1)).isoformat()
            app.settings.snoozed["th1"] = past
            worker = app.run_worker(app._resurface_due_snoozes, thread=True)
            await worker.wait()
            await pilot.pause()
            assert modify.called, "resurface didn't call modify_labels"
            assert modify.call_args.kwargs.get("add") == ["INBOX"], \
                f"resurface should add INBOX: {modify.call_args}"
            assert app.settings.snoozed == {}, f"store not cleared: {app.settings.snoozed}"

    print("mail_snooze PASSED")


if __name__ == "__main__":
    asyncio.run(run())
