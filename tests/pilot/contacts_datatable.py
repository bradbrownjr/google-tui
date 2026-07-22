"""Pilot scenario (ROADMAP P3): the Contacts tab renders as a DataTable, the
live search filters it, and Enter/Space on a row opens the detail modal.

Runs as its own process — see tests/isolate.py / AGENTS.md §6.

Usage: python -m tests.pilot.contacts_datatable
"""
import asyncio
from unittest.mock import patch

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-contacts-")

from google_tui import gauth  # noqa: E402
from google_tui.main import GoogleTUI, ContactModal  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

# Emoji/CJK on purpose — the width bug class the DataTable migration kills.
FAKE_CONTACTS = [
    {"resource_name": "people/c1", "name": "Roger 🚀 Wilco", "email": "roger@example.com"},
    {"resource_name": "people/c2", "name": "田中 太郎", "email": "tanaka@example.jp"},
    {"resource_name": "people/c3", "name": "María José", "email": "mj@example.es"},
    {"resource_name": "people/c4", "name": "", "email": "bare-address@example.com"},
]


async def run() -> None:
    app = GoogleTUI()
    patches = base_patches() + [
        patch.object(gauth, "list_contacts", return_value=FAKE_CONTACTS),
    ]
    with applied(patches):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(1)
            await pilot.pause()

            app.action_goto_tab_contacts()
            await asyncio.sleep(1)
            await pilot.pause()

            table = app.query_one("#contacts-list", DataTable)
            assert table.row_count == len(FAKE_CONTACTS), \
                f"expected {len(FAKE_CONTACTS)} rows, got {table.row_count}"
            labels = [c.label.plain for c in table.columns.values()]
            assert labels == ["Name", "Email"], f"unexpected columns {labels}"

            # Live fuzzy filter narrows the table (debounced Input.Changed).
            search = app.query_one("#contacts-search")
            search.focus()
            search.value = "tanaka"
            app._refresh_contacts_list()
            await pilot.pause()
            assert table.row_count == 1, f"filter expected 1 row, got {table.row_count}"
            search.value = ""
            app._refresh_contacts_list()
            await pilot.pause()
            assert table.row_count == len(FAKE_CONTACTS)

            # Enter on a row opens the ContactModal via on_data_table_row_selected.
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ContactModal), \
                f"Enter did not open ContactModal, on {type(app.screen).__name__}"
            app.pop_screen()
            await pilot.pause()

    print("contacts_datatable PASSED")


if __name__ == "__main__":
    asyncio.run(run())
