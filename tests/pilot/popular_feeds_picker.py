"""Pilot scenario: Settings -> News Feeds' "Browse popular feeds…" button
(ROADMAP: RSS subscription list, 2026-07-19) opens FeedPickerModal, a
filterable checklist over popular_feeds.POPULAR_FEEDS. Verifies the picker
starts pre-checked for anything already in Settings.feed_urls, that checking
a new box subscribes it (Settings.feed_urls + #settings-feed-list both
updated, background merge fetch kicked via the mocked fetchers.fetch_feed),
and that unchecking an already-subscribed box unsubscribes it -- a genuine
two-way toggle, unlike the assign-only LabelPickerModal it's cloned from.

Usage: python -m tests.pilot.popular_feeds_picker
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-feed-picker-")

from textual.widgets import SelectionList, TabbedContent  # noqa: E402

from google_tui.main import GoogleTUI, POPULAR_FEEDS  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

_ESPN_URL = POPULAR_FEEDS["Sports"][0]["url"]
_ARS_URL = POPULAR_FEEDS["Tech News"][0]["url"]
_KREBS_URL = POPULAR_FEEDS["Cybersecurity"][0]["url"]


async def run() -> None:
    app = GoogleTUI()
    # Pre-subscribe ESPN via the plain URL mechanism (as if the user had
    # typed it in manually) so this scenario can verify the picker both
    # starts pre-checked for it AND can unsubscribe it.
    app.settings.feed_urls = [_ESPN_URL]

    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(1)
            await pilot.pause()

            app.action_goto_tab_settings()
            await pilot.pause()
            app.query_one("#settings-tabs", TabbedContent).active = "settings-tab-feeds"
            await pilot.pause()

            # ESPN shows up in the plain feed list already (pre-subscribed).
            # #settings-feed-list is a DataTable now (ROADMAP P3); its row-key
            # -> url map is _feeds_by_cid.
            assert _ESPN_URL in app._feeds_by_cid.values(), app._feeds_by_cid

            await pilot.click("#settings-browse-feeds")
            await pilot.pause()

            modal = app.screen
            sel_list = modal.query_one("#feedpick-list", SelectionList)

            # Pre-checked for the already-subscribed feed, nothing else.
            assert _ESPN_URL in sel_list.selected, sel_list.selected
            assert _ARS_URL not in sel_list.selected, sel_list.selected

            # Filter narrows to just the Cybersecurity category (the combined
            # "Category — Title" label is what's matched against; unlike
            # "tech news", "cybersecurity" doesn't fuzzy-overlap any other
            # category's label well enough to clear the match threshold).
            search = modal.query_one("#feedpick-search")
            search.value = "cybersecurity"
            await pilot.pause()
            assert sel_list.option_count == len(POPULAR_FEEDS["Cybersecurity"]), sel_list.option_count

            # Reset filter, then drive the actual selection state directly
            # (SelectionList.select/deselect are the documented programmatic
            # API -- Apply reads sel_list.selected fresh, so this exercises
            # the real apply-time code path without needing to simulate
            # keyboard navigation to a specific row).
            search.value = ""
            await pilot.pause()
            sel_list.select(_ARS_URL)   # newly subscribe Ars Technica
            sel_list.deselect(_ESPN_URL)  # unsubscribe ESPN
            await pilot.pause()

            await pilot.click("#feedpick-apply")
            await pilot.pause()

            assert _ARS_URL in app.settings.feed_urls, app.settings.feed_urls
            assert _ESPN_URL not in app.settings.feed_urls, app.settings.feed_urls
            urls_shown = set(app._feeds_by_cid.values())
            assert _ARS_URL in urls_shown, urls_shown
            assert _ESPN_URL not in urls_shown, urls_shown

            # Manually-added feeds outside the curated table are untouched:
            # add one, reopen the picker, apply with no changes, confirm it
            # survives.
            custom_url = "https://example.com/custom-feed.xml"
            app.query_one("#settings-feed-url").value = custom_url
            await pilot.click("#settings-add-feed")
            await pilot.pause()
            assert custom_url in app.settings.feed_urls

            await pilot.click("#settings-browse-feeds")
            await pilot.pause()
            await pilot.click("#feedpick-apply")
            await pilot.pause()
            assert custom_url in app.settings.feed_urls, app.settings.feed_urls

    print("popular_feeds_picker PASSED")


if __name__ == "__main__":
    asyncio.run(run())
