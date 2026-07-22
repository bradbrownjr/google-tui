"""Pilot scenario: notify() de-duplicates a burst of toasts and collapses the
Google-OAuth-revoked fan-out into a single actionable re-auth message.

A revoked/expired refresh_token surfaces once per data source on a single
refresh -- _live_refresh_thread's try/except blocks each format their own
"<Section> error: ... invalid_grant ..." toast (Refresh/Labels/Calendars/
Calendar/Drive). Before this fix, that flashed ~6 near-identical error toasts
by too fast to read. notify() now (a) rewrites any invalid_grant-signature
message to one clear re-auth message, and (b) suppresses repeats of an
identical toast within a short window, so the burst collapses to one.

Usage: python -m tests.pilot.toast_dedup_reauth
"""
import asyncio

from tests.isolate import isolate

isolate(prefix="google-tui-pilot-toastdedup-")

from google_tui.main import GoogleTUI  # noqa: E402
from tests.pilot.fakes import applied, base_patches  # noqa: E402

_INVALID_GRANT = (
    "('invalid_grant: Token has been expired or revoked.', "
    "{'error': 'invalid_grant', "
    "'error_description': 'Token has been expired or revoked.'})"
)


def _active_toasts(app):
    return list(app._notifications)


async def run() -> None:
    app = GoogleTUI()
    with applied(base_patches()):
        async with app.run_test(size=(140, 44)) as pilot:
            await asyncio.sleep(2)
            await pilot.pause()

            # Clear any startup toasts so the assertions below only see what
            # this scenario fires.
            app._notifications.clear()
            await pilot.pause()

            # Simulate the _live_refresh_thread fan-out: the same revoked
            # token, reported once per data source with a different prefix.
            for prefix in ("Refresh", "Labels", "Calendars", "Calendar", "Drive"):
                app.notify(f"{prefix} error: {_INVALID_GRANT}", severity="error")
            await pilot.pause()

            toasts = _active_toasts(app)
            assert len(toasts) == 1, f"expected 1 collapsed toast, got {len(toasts)}: {toasts}"
            msg = toasts[0].message
            assert "Re-authorize Google" in msg, msg
            assert "invalid_grant" not in msg, msg
            # Error toasts get the longer dwell time when no timeout is passed.
            assert toasts[0].timeout == app._NOTIFY_ERROR_TIMEOUT, toasts[0].timeout

            # A genuinely different message is NOT suppressed by the dedup.
            app.notify("No more messages to load.", severity="warning")
            await pilot.pause()
            assert len(_active_toasts(app)) == 2, _active_toasts(app)

            # An identical repeat of that warning IS suppressed within the window.
            app.notify("No more messages to load.", severity="warning")
            await pilot.pause()
            assert len(_active_toasts(app)) == 2, _active_toasts(app)

    print("toast_dedup_reauth PASSED")


if __name__ == "__main__":
    asyncio.run(run())
