"""Unit tests for google_tui.render's protocol-agnostic Document parsers.
Pure functions, no Textual app, no I/O — plain in-process pytest.
"""
from google_tui import render


def test_parse_html_extracts_title_and_links():
    html = """
    <html><head><title>Example Page</title></head>
    <body>
        <h1>Welcome</h1>
        <p>Some intro text with a <a href="/about">link</a> in it.</p>
    </body></html>
    """
    doc = render.parse_html(html, base_url="https://example.com/")
    assert doc.title == "Example Page"
    assert any(b.kind == "heading" and "Welcome" in b.text for b in doc.blocks)
    assert any("intro text" in b.text for b in doc.blocks)
    assert len(doc.links) >= 1
    link = doc.links[0]
    assert link.url == "https://example.com/about"
    assert link.text == "link"


def test_parse_html_empty_input_does_not_raise():
    doc = render.parse_html("", base_url="https://example.com/")
    assert doc.blocks == []
    assert doc.links == []


def test_parse_gopher_menu_builds_numbered_links():
    menu = (
        "iWelcome to the server\t\t\t\r\n"
        "1Sample dir\t/sample\tgopher.example.org\t70\r\n"
        "0Readme file\t/readme.txt\tgopher.example.org\t70\r\n"
        ".\r\n"
    )
    doc = render.parse_gopher_menu(menu, base_url="gopher://gopher.example.org")
    info_blocks = [b for b in doc.blocks if b.kind == "paragraph"]
    menu_blocks = [b for b in doc.blocks if b.kind == "menu_item"]
    assert any("Welcome to the server" in b.text for b in info_blocks)
    assert len(menu_blocks) == 2
    assert len(doc.links) == 2
    assert doc.links[0].number == 1
    assert doc.links[0].url == "gopher://gopher.example.org:70/1/sample"
    assert doc.links[1].url == "gopher://gopher.example.org:70/0/readme.txt"


def test_parse_gopher_menu_skips_blank_and_dot_lines():
    menu = "\n.\n1Item\t/x\thost\t70\n"
    doc = render.parse_gopher_menu(menu)
    assert len(doc.links) == 1


def test_parse_gemtext_headings_links_and_lists():
    content = (
        "# Title Heading\n"
        "Some paragraph text.\n"
        "=> https://example.com/page A link with text\n"
        "=> gemini://example.com/other\n"
        "* first item\n"
        "* second item\n"
        "> a quoted line\n"
    )
    doc = render.parse_gemtext(content)
    assert doc.title == "Title Heading"
    headings = [b for b in doc.blocks if b.kind == "heading"]
    assert headings and headings[0].level == 1
    list_items = [b for b in doc.blocks if b.kind == "list_item"]
    assert len(list_items) == 2
    quotes = [b for b in doc.blocks if b.kind == "quote"]
    assert len(quotes) == 1
    assert len(doc.links) == 2
    assert doc.links[0].url == "https://example.com/page"
    assert doc.links[0].text == "A link with text"
    # second link line supplied no text -> falls back to something non-empty
    assert doc.links[1].url == "gemini://example.com/other"


def test_parse_gemtext_preformatted_block_is_verbatim():
    content = "```caption text\ncode line 1\n  indented code line 2\n```\n"
    doc = render.parse_gemtext(content)
    pre = [b for b in doc.blocks if b.kind == "preformatted"]
    assert len(pre) == 1
    assert pre[0].caption == "caption text"
    assert pre[0].text == "code line 1\n  indented code line 2"


def test_parse_gemtext_explicit_title_wins_over_heading():
    content = "# Heading Text\nbody\n"
    doc = render.parse_gemtext(content, title="Explicit Title")
    assert doc.title == "Explicit Title"


def test_decode_html_entities_basic():
    assert render.decode_html_entities("Tom &amp; Jerry") == "Tom & Jerry"
    assert render.decode_html_entities("&lt;tag&gt;") == "<tag>"
    assert render.decode_html_entities("caf&#233;") == "café"
