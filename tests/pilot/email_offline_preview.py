"""Pilot scenario: the inline email preview pane (highlight a thread with
"p" preview enabled) serves a full body from the persistent thread_body
cache when offline, instead of falling back to a snippet-only message --
even when that body was never fetched in the *current* session (the
in-memory `_thread_full_cache` is empty after a restart, but the on-disk
cache written by an earlier ThreadModal open, or by a prior preview, is
still there). Regression coverage for the gap fixed alongside ROADMAP P4's
"Cache email bodies for offline reading" item, 2026-07-19.

Usage: python -m tests.pilot.email_offline_preview
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-email-offline-")

from google_tui.main import GoogleTUI  # noqa: E402
from textual.widgets import DataTable  # noqa: E402
from google_tui.render import DocumentView  # noqa: E402
from tests.pilot.fakes import applied, base_patches, FAKE_THREADS  # noqa: E402

FAKE_THREADS_WITH_HISTORY = [dict(FAKE_THREADS[0], historyId="h1")]

FULL_BODY_TEXT = "Are we still on for hiking Saturday morning? Full body text."


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()

            # Seed state the way a real prior session would have left it:
            # the summary carries the historyId Gmail reported, and a
            # matching thread_body row is already on disk (as if written by
            # an earlier ThreadModal open or preview) -- but nothing is in
            # this session's in-memory _thread_full_cache yet.
            app._threads_cache = {"th1": FAKE_THREADS_WITH_HISTORY[0]}
            app._cache.put("thread_body", "th1", {
                "historyId": "h1",
                "msgs": [{
                    "id": "m1", "from": "Priya Rao <priya.rao@example.com>",
                    "date": "Thu, 16 Jul 2026 09:00:00 +0000", "subject": "Weekend plans?",
                    "body": FULL_BODY_TEXT, "html_body": "", "label_ids": ["INBOX"],
                }],
            })
            app._thread_full_cache.clear()
            app._online = False

            app.action_goto_tab_mail()
            await pilot.pause()
            if not app._email_preview_visible:
                await pilot.press("p")
                await pilot.pause()
            assert app.query_one("#email-list", DataTable).row_count, \
                "email list is empty, nothing to select"
            # Enabling the preview ('p') already previews the row-0 thread; the
            # DataTable cursor starts there (no "down" needed).
            await asyncio.sleep(0.6)  # debounce (0.25s) + worker thread round trip
            await pilot.pause()

            doc = app.query_one("#email-preview-doc", DocumentView)
            assert doc.document is not None
            text = "\n".join(b.text for b in doc.document.blocks)
            assert FULL_BODY_TEXT in text, (
                f"expected the cached full body, got: {text!r}")

    print("email_offline_preview PASSED")


if __name__ == "__main__":
    asyncio.run(run())
