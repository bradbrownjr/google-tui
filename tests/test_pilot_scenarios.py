"""Runs each tests/pilot/ scenario as its own subprocess and asserts a clean
exit. Each scenario instantiates a real GoogleTUI() under Textual's run_test
pilot driver, so they can't share a process with each other or with the
tests/unit/ suite -- see AGENTS.md §6 / tests/isolate.py's docstring for the
real DuplicateIds crash that motivated the "own process per GoogleTUI()
scenario" rule. A thin subprocess wrapper here is what makes that rule
compatible with a single `pytest` invocation covering everything.
"""
import subprocess
import sys

import pytest

SCENARIOS = [
    "tests.pilot.startup_smoke",
    "tests.pilot.email_reply_modal",
    "tests.pilot.event_task_markdown",
    "tests.pilot.email_offline_preview",
    "tests.pilot.dashboard_external_cards",
    "tests.pilot.popular_feeds_picker",
    "tests.pilot.drive_google_regression",
    "tests.pilot.drive_markdown_preview",
    "tests.pilot.drive_remote_source_switch",
    "tests.pilot.browser_sftp_redirect",
]


@pytest.mark.parametrize("module", SCENARIOS)
def test_pilot_scenario(module):
    result = subprocess.run(
        [sys.executable, "-m", module],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{module} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
