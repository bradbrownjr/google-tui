"""Pilot scenario (ROADMAP P3): the Email list as a DataTable — Space expands
a thread into one real row per message (and collapses it back), and 'x'
multi-select tints the row's cells (no per-row CSS class).

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.mail_datatable_expand
"""
import asyncio
from unittest.mock import patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-mailexpand-")

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

THREADS = [
    {"threadId": "th1", "subject": "🚀 Deploy went out", "from": "Roger <roger@example.com>",
     "date": "Mon, 20 Jul 2026 09:00:00 +0000", "count": 3, "unread": True,
     "snippet": "please review", "labelIds": ["INBOX", "STARRED"]},
    {"threadId": "th2", "subject": "会議の議事録", "from": "田中 <tanaka@example.jp>",
     "date": "Mon, 20 Jul 2026 08:00:00 +0000", "count": 1, "unread": False,
     "snippet": "詳細はこちら", "labelIds": ["INBOX"]},
]
FULL = [
    {"id": "m1", "from": "Roger <roger@example.com>", "body": "first message body"},
    {"id": "m2", "from": "田中 <tanaka@example.jp>", "body": "second reply"},
    {"id": "m3", "from": "Roger <roger@example.com>", "body": "third reply"},
]


def _keys(table):
    return [rk.value for rk in table.rows]


async def run() -> None:
    app = GoogleTUI()
    patches = [p for p in base_patches() if p.attribute not in ("list_threads", "get_thread")] + [
        patch.object(gauth, "list_threads", return_value=(THREADS, None)),
        patch.object(gauth, "get_thread", return_value=FULL),
    ]
    with applied(patches):
        async with app.run_test(size=(150, 44)) as pilot:
            await asyncio.sleep(1.5)
            app.action_goto_tab_mail()
            await pilot.pause()
            table = app.query_one("#email-list", DataTable)
            assert _keys(table) == ["t-th1", "t-th2"], _keys(table)

            # Space on the 3-message thread (row 0) → reveal 3 message rows.
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("space")
            await asyncio.sleep(0.5)  # get_thread runs on a worker thread
            await pilot.pause()
            assert _keys(table) == ["t-th1", "t-th1::m0", "t-th1::m1", "t-th1::m2", "t-th2"], _keys(table)

            # Space again → collapse back to just the summaries.
            table.move_cursor(row=table.get_row_index("t-th1"))
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            assert _keys(table) == ["t-th1", "t-th2"], _keys(table)

            # 'x' multi-select tints the row's cells (checks _email_sel_style is
            # applied) and records the thread; 'x' again clears both.
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert app._email_selected == {"th1"}, app._email_selected
            styled = table.get_cell_at((table.get_row_index("t-th1"), 2))  # Subject cell
            assert "on " in str(styled.style or ""), f"row not tinted: {styled.style!r}"
            # 'x' advanced the cursor (Gmail-style) — move back to un-check th1.
            table.move_cursor(row=table.get_row_index("t-th1"))
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert app._email_selected == set(), app._email_selected
            cleared = table.get_cell_at((table.get_row_index("t-th1"), 2))
            assert "on " not in str(cleared.style or ""), f"tint not cleared: {cleared.style!r}"

    print("mail_datatable_expand PASSED")


if __name__ == "__main__":
    asyncio.run(run())
