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


def test_parse_markdown_headings_lists_quotes_and_links():
    content = (
        "# Title Heading\n"
        "Some paragraph text.\n"
        "- first item\n"
        "- second item\n"
        "1. step one\n"
        "> a quoted line\n"
        "See [the site](https://example.com/page) for more.\n"
    )
    doc = render.parse_markdown(content)
    assert doc.title == "Title Heading"
    headings = [b for b in doc.blocks if b.kind == "heading"]
    assert headings and headings[0].level == 1
    unordered = [b for b in doc.blocks if b.kind == "list_item" and not b.ordered]
    assert len(unordered) == 2
    ordered = [b for b in doc.blocks if b.kind == "list_item" and b.ordered]
    assert len(ordered) == 1
    quotes = [b for b in doc.blocks if b.kind == "quote"]
    assert len(quotes) == 1
    assert len(doc.links) == 1
    assert doc.links[0].url == "https://example.com/page"
    assert doc.links[0].text == "the site"
    link_para = next(b for b in doc.blocks if "the site" in b.text)
    assert "[1]" in link_para.text


def test_parse_markdown_fenced_code_block_is_verbatim():
    content = "```python\ndef f():\n    return 1\n```\n"
    doc = render.parse_markdown(content)
    pre = [b for b in doc.blocks if b.kind == "preformatted"]
    assert len(pre) == 1
    assert pre[0].caption == "python"
    assert pre[0].text == "def f():\n    return 1"


def test_parse_markdown_explicit_title_wins_over_heading():
    content = "# Heading Text\nbody\n"
    doc = render.parse_markdown(content, title="Explicit Title")
    assert doc.title == "Explicit Title"


def test_parse_markdown_strips_bold_markers():
    doc = render.parse_markdown("This is **bold** and __also bold__.")
    assert doc.blocks[0].text == "This is bold and also bold."


def test_looks_like_markdown_true_for_heading_alone():
    assert render._looks_like_markdown("# A Heading\nSome text below it.")


def test_looks_like_markdown_true_for_fenced_code_pair():
    assert render._looks_like_markdown("Here is code:\n```\nx = 1\n```\n")


def test_looks_like_markdown_true_for_inline_link():
    assert render._looks_like_markdown("Check out [this page](https://example.com/x) for details.")


def test_looks_like_markdown_false_for_single_fence_marker():
    # An unterminated/lone ``` shouldn't count as a real fenced-code PAIR.
    assert not render._looks_like_markdown("Some text with a stray ``` in it.")


def test_looks_like_markdown_false_for_plain_email_with_stray_emphasis():
    text = "Hey, just a quick note -- can you send that *file* over? Thanks, R"
    assert not render._looks_like_markdown(text)


def test_looks_like_markdown_false_for_quoted_reply_alone():
    # A plain-text email reply chain is the single most common shape of
    # "not Markdown" text this sniffer sees -- must not trigger alone.
    text = "> On Tuesday, Priya wrote:\n> Are we still on for lunch?\n> See you then.\n"
    assert not render._looks_like_markdown(text)


def test_looks_like_markdown_true_for_quote_plus_bold_combo():
    text = "> quoted line\nThis part is **important** though.\n"
    assert render._looks_like_markdown(text)


def test_looks_like_markdown_false_for_multiple_list_items_alone():
    # List markers alone are WEAK (see _looks_like_markdown's docstring) --
    # a plain shopping list ("- eggs\n- milk\n- bread") is a very ordinary
    # thing to type in an email without "meaning" Markdown, so it must not
    # be enough by itself.
    text = "Shopping list:\n- eggs\n- milk\n- bread\n"
    assert not render._looks_like_markdown(text)


def test_looks_like_markdown_true_for_list_plus_bold_combo():
    text = "Shopping list:\n- eggs\n- **milk**\n- bread\n"
    assert render._looks_like_markdown(text)


def test_looks_like_markdown_false_for_single_list_item():
    text = "Note: - this one thing needs doing\n"
    assert not render._looks_like_markdown(text)


def test_parse_feed_entry_routes_markdown_when_sniffed():
    content = "# Release Notes\n- Fixed a bug\n- Added a feature\n"
    doc = render.parse_feed_entry("", content)
    headings = [b for b in doc.blocks if b.kind == "heading"]
    assert headings
    list_items = [b for b in doc.blocks if b.kind == "list_item"]
    assert len(list_items) == 2


def test_parse_feed_entry_plain_text_stays_unrendered_paragraphs():
    content = "Hey, just a quick note -- can you send that *file* over? Thanks, R"
    doc = render.parse_feed_entry("", content)
    assert len(doc.blocks) == 1
    assert doc.blocks[0].kind == "paragraph"
    assert doc.blocks[0].text == content
