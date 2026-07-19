"""Runs before any test module in this tree is imported (pytest imports
conftest.py first), so it's the one safe place to redirect platformdirs
before google_tui.cache/settings/remote_creds get imported anywhere in this
process by tests/unit/*. See tests/isolate.py's docstring for why this must
happen before import, not inside a fixture.

tests/pilot/* scenarios run in their OWN subprocess (see tests/isolate.py)
and call isolate() again themselves — this one only covers the in-process
tests/unit/* suite.
"""
from tests.isolate import isolate

isolate(prefix="google-tui-unit-test-")
