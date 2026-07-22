"""Pilot scenario (ROADMAP P3): the News tab renders as a DataTable with real
Date/Feed/Title columns, the live search filters it, and Enter/Space opens the
entry modal.

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.news_datatable
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-news-")

from google_tui.main import GoogleTUI, NewsEntryModal  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

# Emoji/CJK + a literal "[Feed]" name that would break Label markup parsing.
ENTRIES = [
    {"id": "e1", "title": "🚀 Rocket launch succeeds", "link": "http://x/1",
     "summary": "s1", "published": "2026-07-21T10:00:00Z", "feed_title": "Tech [beta]"},
    {"id": "e2", "title": "会議の議事録が公開", "link": "http://x/2",
     "summary": "s2", "published": "2026-07-20T09:00:00Z", "feed_title": "日本ニュース"},
    {"id": "e3", "title": "Markets rally on news", "link": "http://x/3",
     "summary": "s3", "published": "2026-07-19T08:00:00Z", "feed_title": "Finance"},
]


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(1)
            await pilot.pause()

            app.action_goto_tab_news()
            await pilot.pause()
            app._apply_news_data(list(ENTRIES))
            await pilot.pause()
            await pilot.pause()

            table = app.query_one("#news-list", DataTable)
            assert table.row_count == len(ENTRIES), \
                f"expected {len(ENTRIES)} rows, got {table.row_count}"
            labels = [c.label.plain for c in table.columns.values()]
            assert labels == ["Date", "Feed", "Title"], f"unexpected columns {labels}"

            # Live filter narrows the table.
            app.query_one("#news-search").value = "rally"
            app._refresh_news_list()
            await pilot.pause()
            assert table.row_count == 1, f"filter expected 1 row, got {table.row_count}"
            app.query_one("#news-search").value = ""
            app._refresh_news_list()
            await pilot.pause()
            assert table.row_count == len(ENTRIES)

            # Enter opens the entry modal via on_data_table_row_selected.
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NewsEntryModal), \
                f"Enter did not open NewsEntryModal, on {type(app.screen).__name__}"
            app.pop_screen()
            await pilot.pause()

    print("news_datatable PASSED")


if __name__ == "__main__":
    asyncio.run(run())
