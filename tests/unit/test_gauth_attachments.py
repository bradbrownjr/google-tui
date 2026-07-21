"""Unit tests for gauth's attachment handling (ROADMAP P2): walking a message
payload for attachment parts, building a multipart message with a file
attached on send, and the download round trip.
"""
import base64
import email as email_lib
from unittest.mock import MagicMock

from google_tui import gauth


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_walk_attachments_finds_named_parts():
    payload = {"parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("hi")}},  # body, no filename
        {"mimeType": "application/pdf", "filename": "report.pdf",
         "body": {"attachmentId": "att-1", "size": 2048}},
    ]}
    atts = gauth._walk_attachments(payload, "msg-9")
    assert len(atts) == 1
    a = atts[0]
    assert a["filename"] == "report.pdf"
    assert a["attachment_id"] == "att-1"
    assert a["message_id"] == "msg-9"
    assert a["size"] == 2048


def test_walk_attachments_recurses_nested_multipart():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("body")}},
        ]},
        {"mimeType": "image/png", "filename": "pic.png",
         "body": {"attachmentId": "att-2", "size": 10}},
    ]}
    atts = gauth._walk_attachments(payload, "m1")
    assert [a["filename"] for a in atts] == ["pic.png"]


def test_walk_attachments_none_returns_empty():
    assert gauth._walk_attachments({"mimeType": "text/plain", "body": {"data": _b64("x")}}, "m") == []


def test_build_raw_with_attachment_is_multipart(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("attached content")
    raw = gauth._build_raw("a@b.com", "Subj", "body text", attachments=[str(f)])
    msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg.is_multipart()
    parts = msg.get_payload()
    # First part is the text body; a later part is the file.
    assert parts[0].get_content_type() == "text/plain"
    filenames = [p.get_filename() for p in parts]
    assert "note.txt" in filenames
    att = next(p for p in parts if p.get_filename() == "note.txt")
    assert att.get_payload(decode=True) == b"attached content"


def test_build_raw_without_attachments_stays_plain():
    raw = gauth._build_raw("a@b.com", "Subj", "hi")
    msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert not msg.is_multipart()


def test_send_message_passes_attachment(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    gmail = MagicMock()
    gauth.send_message({"gmail": gmail}, to="a@b.com", subject="S", body="B",
                       attachments=[str(f)])
    raw = gmail.users().messages().send.call_args.kwargs["body"]["raw"]
    msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg.is_multipart()
    assert "doc.txt" in [p.get_filename() for p in msg.get_payload()]


def test_download_attachment_decodes_data():
    gmail = MagicMock()
    gmail.users().messages().attachments().get().execute.return_value = {"data": _b64("filebytes")}
    out = gauth.download_attachment({"gmail": gmail}, "m1", "att-1")
    assert out == b"filebytes"
