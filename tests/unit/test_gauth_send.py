"""Unit tests for google_tui.gauth's message-assembly write paths: CC/BCC on
send, draft creation, editable reply recipients, and untrash (ROADMAP P2
"Drafts + CC/BCC in compose" / "Undo for destructive mail actions"). The
Gmail service is a MagicMock — no network — and each test decodes the raw
RFC 822 body the code handed to the API to assert on the actual headers.
"""
import base64
import email as email_lib
from unittest.mock import MagicMock

from google_tui import gauth


def _decode_raw(raw: str):
    return email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))


def _sent_message(gmail: MagicMock) -> dict:
    """The body dict passed to messages().send."""
    return gmail.users().messages().send.call_args.kwargs["body"]


def _created_draft(gmail: MagicMock) -> dict:
    return gmail.users().drafts().create.call_args.kwargs["body"]


def test_send_message_includes_cc_and_bcc():
    gmail = MagicMock()
    gauth.send_message({"gmail": gmail}, to="a@b.com", subject="Hi", body="hello",
                       cc="c@d.com", bcc="e@f.com")
    msg = _decode_raw(_sent_message(gmail)["raw"])
    assert msg["to"] == "a@b.com"
    assert msg["cc"] == "c@d.com"
    assert msg["bcc"] == "e@f.com"
    assert msg["subject"] == "Hi"


def test_send_message_omits_empty_cc_bcc_headers():
    gmail = MagicMock()
    gauth.send_message({"gmail": gmail}, to="a@b.com", subject="Hi", body="hello")
    msg = _decode_raw(_sent_message(gmail)["raw"])
    assert msg["cc"] is None
    assert msg["bcc"] is None


def test_create_draft_uses_drafts_create_with_message_wrapper():
    gmail = MagicMock()
    gauth.create_draft({"gmail": gmail}, to="a@b.com", subject="Draft it",
                       body="wip", cc="c@d.com", thread_id="th9")
    body = _created_draft(gmail)
    assert body["message"]["threadId"] == "th9"
    msg = _decode_raw(body["message"]["raw"])
    assert msg["to"] == "a@b.com"
    assert msg["cc"] == "c@d.com"
    assert msg["subject"] == "Draft it"
    # send() must NOT have been called — a draft is saved, not sent.
    gmail.users().messages().send.assert_not_called()


def _gmail_with_thread_headers() -> MagicMock:
    gmail = MagicMock()
    gmail.users().threads().get().execute.return_value = {
        "messages": [{"payload": {"headers": [
            {"name": "From", "value": "orig@x.com"},
            {"name": "To", "value": "me@x.com"},
            {"name": "Cc", "value": "team@x.com"},
            {"name": "Subject", "value": "Hello"},
            {"name": "Message-ID", "value": "<mid-1>"},
        ]}}]
    }
    return gmail


def test_reply_to_derives_recipients_when_no_overrides():
    gmail = _gmail_with_thread_headers()
    gauth.reply_to({"gmail": gmail}, "th1", "my reply", reply_all=True)
    msg = _decode_raw(_sent_message(gmail)["raw"])
    assert msg["to"] == "orig@x.com"
    assert msg["cc"] == "me@x.com, team@x.com"
    assert msg["subject"] == "Re: Hello"
    assert msg["In-Reply-To"] == "<mid-1>"
    assert _sent_message(gmail)["threadId"] == "th1"


def test_reply_to_honors_explicit_overrides_including_cleared_cc():
    gmail = _gmail_with_thread_headers()
    # User edited To and deliberately cleared Cc ("" — not None): the derived
    # team@x.com must NOT come back.
    gauth.reply_to({"gmail": gmail}, "th1", "my reply", reply_all=True,
                   to="someone@else.com", cc="", subject="Custom")
    msg = _decode_raw(_sent_message(gmail)["raw"])
    assert msg["to"] == "someone@else.com"
    assert msg["cc"] is None
    assert msg["subject"] == "Custom"
    # Threading headers still derived from the original message.
    assert msg["In-Reply-To"] == "<mid-1>"


def test_untrash_thread_calls_threads_untrash():
    gmail = MagicMock()
    gauth.untrash_thread({"gmail": gmail}, "th7")
    gmail.users().threads().untrash.assert_called_once_with(userId="me", id="th7")
