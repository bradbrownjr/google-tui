"""Unit tests for google_tui.gauth's Gmail MIME-payload extraction helpers.
Pure functions over plain dicts shaped like the Gmail API's message.payload
— no network calls, no `svc` object needed.
"""
import base64

from google_tui import gauth


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def test_extract_body_simple_plain_text():
    payload = {"mimeType": "text/plain", "body": {"data": _b64("hello world")}}
    assert gauth._extract_body(payload) == "hello world"


def test_extract_body_prefers_plain_text_part_over_html():
    payload = {
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>hi</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64("plain hi")}},
        ]
    }
    assert gauth._extract_body(payload) == "plain hi"


def test_extract_body_recurses_into_nested_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("nested plain body")}},
                ],
            },
        ],
    }
    assert gauth._extract_body(payload) == "nested plain body"


def test_extract_body_no_matching_part_returns_empty():
    payload = {"parts": [{"mimeType": "image/png", "body": {"attachmentId": "abc"}}]}
    assert gauth._extract_body(payload) == ""


def test_extract_html_body_prefers_html_part():
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>rich</p>")}},
        ]
    }
    assert gauth._extract_html_body(payload) == "<p>rich</p>"


def test_extract_html_body_plain_text_only_message_returns_empty():
    payload = {"mimeType": "text/plain", "body": {"data": _b64("just text")}}
    assert gauth._extract_html_body(payload) == ""


def test_extract_html_body_recurses_into_nested_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
                    {"mimeType": "text/html", "body": {"data": _b64("<b>nested html</b>")}},
                ],
            },
        ],
    }
    assert gauth._extract_html_body(payload) == "<b>nested html</b>"
