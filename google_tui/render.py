"""google_tui.render — protocol-agnostic document model + renderer (M1).

Shared core consumed by the Browser tab (M2: Web/Gopher/Gemini/Search), the
News tab (M3: RSS/Atom entries), and rich HTML email (M4). Everything here
is fetch-agnostic: parsers take already-fetched text and never do I/O of
their own (fetching stays in the M2/M3 consumers, run via
``self.run_worker(fn, thread=True)`` per this repo's convention — see
AGENTS.md).

Ported from ``bpq-apps/apps/htmlview.py`` (HTML nav/content link
separation) and ``bpq-apps/apps/gopher.py`` (Gopher menu parsing), with
three deliberate departures from the source material:

1. ``decode_html_entities`` decodes to real Unicode instead of stripping
   everything above ASCII 126 — that stripping existed only because the
   original ran on a packet-radio terminal; Textual handles Unicode fine.
2. The nav-heuristic that used to hardcode a specific domain now compares
   ``urlparse().netloc`` against the page's own ``base_url`` (a same-site
   check that works for any page, not just one).
3. ``<pre>``/``<code>`` blocks are extracted verbatim (entity-decoded but
   whitespace-preserved) as their own ``Block`` kind, pulled out of the HTML
   *before* any whitespace-collapsing regex runs — the source material had
   no such handling at all, so pasted code/log content used to get mangled.

The BBS-era print()/input() pagination loop, the `__EXIT__`/`__MAIN__`
sentinel strings, and the self-update mechanism are intentionally NOT
ported: ``DocumentView`` below owns real scrolling instead.

Settings -> General -> "ASCII-safe mode" (P2, 2026-07-15) partially reverses
departure #1 above, but only for the specific curly-quote/dash/ellipsis/
bullet/guillemet punctuation ``decode_html_entities`` itself introduces —
real content Unicode (accented names, non-Latin scripts, etc.) is left
alone; this is a punctuation fallback for mangling terminals, not the old
strip-everything-above-ASCII-126 behavior. Threaded through as a plain
``ascii_mode: bool`` parameter on every function in this module that
(transitively) calls ``decode_html_entities`` — this module stays I/O-free
and knows nothing about ``Settings``; callers (``main.py``, ``fetchers.py``)
read ``Settings.ascii_mode`` and pass the bool in.
"""
from __future__ import annotations

import html as _html_stdlib
import re
import urllib.parse as _urlparse_mod
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

# urllib.parse's urljoin() only resolves relative references for schemes it
# recognizes (uses_relative/uses_netloc) — "gopher" is already registered,
# but "gemini" isn't, so a bare urljoin("gemini://host/", "/foo") silently
# returns "/foo" unresolved instead of "gemini://host/foo". Register it once
# at import time so _resolve_url works for gemtext's relative "=>" links.
for _scheme in ("gemini",):
    if _scheme not in _urlparse_mod.uses_relative:
        _urlparse_mod.uses_relative.append(_scheme)
    if _scheme not in _urlparse_mod.uses_netloc:
        _urlparse_mod.uses_netloc.append(_scheme)

from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class RenderError(Exception):
    """Base class for this module's errors."""


class ParseError(RenderError):
    """Reserved for genuinely unusable input.

    Parsers in this module are deliberately lenient (malformed HTML/gopher/
    gemtext just produces a sparser Document) — this exists for a future
    caller that needs to distinguish "nothing usable came back" from
    "produced an empty-ish but valid Document."
    """


class FetchError(RenderError):
    """Reserved for a shared error hierarchy across fetch + render.

    Never raised by this module (render.py does zero I/O) — defined here so
    M2 (HTTP/Gopher/Gemini fetch) and M3 (feed fetch) can raise it and share
    a single ``except RenderError`` catch-all with parse errors.
    """


# --------------------------------------------------------------------------
# Document model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    number: int
    url: str
    text: str
    kind: str = "content"  # "content" | "nav"
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Block:
    kind: str  # "heading" | "paragraph" | "list_item" | "preformatted" | "quote" | "menu_item"
    text: str
    level: int = 0
    ordered: bool = False
    link: Link | None = None  # set when the whole block IS a link (gopher/gemtext menu items)
    caption: str | None = None  # e.g. gemtext ``` alt-text


@dataclass(frozen=True)
class GopherRef:
    host: str
    port: int
    item_type: str
    selector: str


@dataclass
class Document:
    title: str | None
    blocks: list[Block] = field(default_factory=list)
    # ALL links (content+nav), uniquely numbered in one flat namespace:
    # content links numbered first in document order, then nav links
    # continuing the same sequence.
    links: list[Link] = field(default_factory=list)
    source_url: str = ""


# --------------------------------------------------------------------------
# Entity decoding
# --------------------------------------------------------------------------

# Historically hand-picked entities from the source material, now mapped to
# their real Unicode characters (fix #1) instead of ASCII approximations.
_ENTITY_MAP = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&mdash;": "—",
    "&ndash;": "–",
    "&hellip;": "…",
    "&copy;": "©",
    "&reg;": "®",
    "&trade;": "™",
    "&bull;": "•",
    "&middot;": "·",
    "&laquo;": "«",
    "&raquo;": "»",
    "&ldquo;": "“",
    "&rdquo;": "”",
    "&lsquo;": "‘",
    "&rsquo;": "’",
    "&deg;": "°",
    "&plusmn;": "±",
    "&times;": "×",
    "&divide;": "÷",
    "&frac12;": "½",
    "&frac14;": "¼",
    "&frac34;": "¾",
}

_NUMERIC_ENTITY_RE = re.compile(r"&#(\d+);")
_HEX_ENTITY_RE = re.compile(r"&#x([0-9a-fA-F]+);")

# Settings -> General -> "ASCII-safe mode": plain-ASCII equivalents for the
# punctuation this module otherwise decodes to real Unicode (fix #1's whole
# point, normally) — applied as a second pass, after the real character has
# already been produced, so this stays a pure substitution table rather than
# a second _ENTITY_MAP to keep in sync.
_ASCII_PUNCTUATION = {
    "‘": "'", "’": "'",   # ‘ ’
    "“": '"', "”": '"',   # “ ”
    "—": "-", "–": "-",   # — –
    "…": "...",                # …
    "•": "*", "·": "*",   # • ·
    "«": '"', "»": '"',   # « »
}


def decode_html_entities(text: str, ascii_mode: bool = False) -> str:
    """Decode HTML entities to proper Unicode text.

    Ported from ``htmlview.py``'s ``decode_html_entities`` (named-entity
    table + numeric/hex entity decoding), minus the ASCII-only stripping
    pass that made sense on a packet-radio terminal but has no place here
    (fix #1 — see module docstring).

    ``ascii_mode=True`` (Settings -> General -> "ASCII-safe mode") re-adds a
    narrow, deliberate version of that stripping: only the specific curly-
    quote/dash/ellipsis/bullet/guillemet punctuation this function itself
    introduces gets swapped for plain ASCII, everything else stays real
    Unicode. This module stays I/O-free/protocol-agnostic — the caller
    passes a plain bool, not a ``Settings`` object (see AGENTS.md).
    """
    for entity, replacement in _ENTITY_MAP.items():
        text = text.replace(entity, replacement)

    def _decode_numeric(match: re.Match) -> str:
        try:
            return chr(int(match.group(1)))
        except (ValueError, OverflowError):
            return ""

    def _decode_hex(match: re.Match) -> str:
        try:
            return chr(int(match.group(1), 16))
        except (ValueError, OverflowError):
            return ""

    text = _NUMERIC_ENTITY_RE.sub(_decode_numeric, text)
    text = _HEX_ENTITY_RE.sub(_decode_hex, text)
    # Safety net for named entities outside the explicit table above
    # (there are hundreds in the HTML spec; this catches the long tail).
    text = _html_stdlib.unescape(text)
    if ascii_mode:
        for glyph, repl in _ASCII_PUNCTUATION.items():
            text = text.replace(glyph, repl)
    return text


# --------------------------------------------------------------------------
# URL resolution
# --------------------------------------------------------------------------

_ABSOLUTE_PREFIXES = ("http://", "https://", "gopher://", "gemini://", "mailto:")


def _resolve_url(url: str, base_url: str) -> str:
    """Resolve a possibly-relative URL against ``base_url``.

    Ported from ``htmlview.py``'s ``HTMLViewer._resolve_url``.
    """
    if not url:
        return url
    if url.startswith(_ABSOLUTE_PREFIXES):
        return url
    if not base_url:
        return url
    return urljoin(base_url, url)


# --------------------------------------------------------------------------
# HTML noise stripping
# --------------------------------------------------------------------------


def _strip_noise(html: str) -> str:
    """Strip script/style/comments and known boilerplate (nav widgets,
    WordPress cruft, share/social buttons, comment sections, etc.).

    Ported from the noise-stripping cascade at the top of ``htmlview.py``'s
    ``HTMLParser.parse`` (roughly lines 265-354), condensed where several
    near-identical class-name patterns collapsed into one loop.
    """
    # Drop <head> entirely: <title>, <meta>, <link> etc. have no business
    # leaking into body text (the original relied on a dedup step in its
    # interactive viewer layer to paper over this — since that layer isn't
    # ported, the real fix is to just not extract head content as body text).
    html = re.sub(r"<head[^>]*>.*?</head>", "", html, flags=re.DOTALL | re.IGNORECASE)

    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<noscript[^>]*>.*?</noscript>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

    html = re.sub(r"<aside[^>]*>.*?</aside>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<div[^>]*id="[^"]*sidebar[^"]*"[^>]*>.*?</div>', "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<div[^>]*id="[^"]*wpcom[^"]*"[^>]*>.*?</div>', "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<div[^>]*id="[^"]*footer[^"]*"[^>]*>.*?</div>', "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<div[^>]*id="comments"[^>]*>.*?</div>', "", html, flags=re.DOTALL | re.IGNORECASE)

    _class_junk = (
        "sidebar", "widget", "post-navigation", "entry-footer", "meta-nav",
        "nav-previous", "nav-next", "author", "by-author", "related",
        "suggested", "share", "social", "tags", "wp-", "site-footer",
        "entry-meta", "post-meta", "sharedaddy", "sd-", "wpcom", "reblog",
        "jetpack", "subscribe", "newsletter", "comments?", "comment-form",
    )
    for cls in _class_junk:
        html = re.sub(
            rf'<div[^>]*class="[^"]*{cls}[^"]*"[^>]*>.*?</div>', "",
            html, flags=re.DOTALL | re.IGNORECASE,
        )
    html = re.sub(r'<div[^>]*class="[^"]*email[^"]*signup[^"]*"[^>]*>.*?</div>', "", html, flags=re.DOTALL | re.IGNORECASE)

    html = re.sub(r'<footer[^>]*class="[^"]*post[^"]*"[^>]*>.*?</footer>', "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<footer[^>]*>.*?</footer>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<section[^>]*class="[^"]*comments[^"]*"[^>]*>.*?</section>', "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<form[^>]*>.*?</form>", "", html, flags=re.DOTALL | re.IGNORECASE)

    html = re.sub(r'<p[^>]*>\s*(?:Tags?|Categories?):[^<]*(?:<[^>]+>[^<]*)*</p>', "", html, flags=re.DOTALL | re.IGNORECASE)

    html = re.sub(r"Like Loading\.\.\.", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<h2[^>]*>\s*Menu\s*</h2>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<div[^>]*>\s*<h2[^>]*>\s*Menu\s*</h2>.*?</div>", "", html, flags=re.DOTALL | re.IGNORECASE)

    html = re.sub(r"<li[^>]*>\s*[-–—]?\s*</li>", "", html, flags=re.DOTALL | re.IGNORECASE)

    html = re.sub(r'<a[^>]+class="[^"]*skip-link[^"]*"[^>]*>.*?</a>', "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"Skip to content", "", html, flags=re.IGNORECASE)
    html = re.sub(r"open primary menu", "", html, flags=re.IGNORECASE)

    html = re.sub(r"<a[^>]*>(facebook|twitter|instagram|linkedin|youtube|pinterest)</a>", "", html, flags=re.IGNORECASE)
    html = re.sub(
        r"<a[^>]*>\s*(facebook|twitter|instagram|linkedin|youtube|pinterest)\s*\[?\d+\]?\s*</a>",
        "", html, flags=re.IGNORECASE,
    )

    html = re.sub(r'<button[^>]*class=["\'][^"\']*dropbtn[^"\']*["\'][^>]*>.*?</button>', "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<div[^>]*class=["\'][^"\']*dropdown[^"\']*["\'][^>]*>\s*</div>', "", html, flags=re.DOTALL | re.IGNORECASE)

    return html


# --------------------------------------------------------------------------
# Balanced tag extraction
# --------------------------------------------------------------------------


def _extract_balanced_tag(html: str, start_pos: int, tag_name: str, max_search: int = 300_000) -> str | None:
    """Extract a tag's full content (including its own open/close tags),
    respecting nested tags of the same name.

    Ported from ``htmlview.py``'s ``HTMLParser._extract_balanced_tag``. The
    original's search cap was an undocumented magic number (50000); this
    version makes it a named, documented parameter, raised to 300_000
    (fix #3).

    Args:
        html: Full HTML text to search within.
        start_pos: Index of (or before) the opening ``<tag_name`` to extract.
        tag_name: Tag name, e.g. ``"div"``.
        max_search: Maximum number of characters past ``start_pos`` to scan
            before giving up on finding a balanced close tag.
    """
    open_tag = f"<{tag_name}"
    close_tag = f"</{tag_name}>"

    tag_end = html.find(">", start_pos)
    if tag_end == -1:
        return None

    pos = tag_end + 1
    depth = 1
    search_limit = min(len(html), start_pos + max_search)
    html_lower = html.lower()
    open_lower = open_tag.lower()
    close_lower = close_tag.lower()

    while pos < search_limit and depth > 0:
        next_open = html_lower.find(open_lower, pos)
        next_close = html_lower.find(close_lower, pos)

        if next_close == -1:
            return None  # Malformed HTML

        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + len(open_tag)
        else:
            depth -= 1
            pos = next_close + len(close_tag)

    if depth == 0:
        return html[start_pos:pos]
    return None


# --------------------------------------------------------------------------
# Nav / content separation
# --------------------------------------------------------------------------

_NAV_WORDS = {"home", "about", "contact", "members", "join", "login", "register"}
_TOP_LINK_EXCLUDE = {"click here", "read more", "continue reading", "learn more", "more"}


def _detect_link_cluster(html: str, nav_threshold: int) -> tuple[list[str], str]:
    """Fallback heuristic: a dense cluster of links at the very start of the
    document is probably a nav menu.

    Ported from ``htmlview.py``'s ``HTMLParser._detect_link_cluster``.
    """
    link_pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    links_with_pos = [
        (m.start(), m.group(0))
        for m in re.finditer(link_pattern, html, flags=re.DOTALL | re.IGNORECASE)
    ]

    if len(links_with_pos) < nav_threshold:
        return [], html

    scan_limit = len(html) // 4
    early_links = [l for l in links_with_pos if l[0] < scan_limit]

    if len(early_links) >= nav_threshold:
        first_pos = early_links[0][0]
        last_idx = min(nav_threshold - 1, len(early_links) - 1)
        last_pos = early_links[last_idx][0]
        region = html[first_pos:last_pos + len(early_links[-1][1])]

        text_only = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", region)).strip()
        avg_text_per_link = len(text_only) / len(early_links) if early_links else 999

        if avg_text_per_link < 50:
            nav_end = last_pos + len(early_links[-1][1]) + 100
            return [html[:nav_end]], html[nav_end:]

    return [], html


def _separate_nav_content(html: str, base_url: str = "", nav_threshold: int = 5) -> tuple[str, str]:
    """Split ``html`` into ``(content_html, nav_html)``.

    Ported from ``htmlview.py``'s ``HTMLParser._separate_nav_content``,
    cascading through:
      1. explicit ``<nav>`` tags (if present, trusted exclusively)
      2. ``<header>`` tags with enough links to look like nav
      3. CSS dropdown-menu wrapper divs
      4. ``class``/``id`` nav/menu/sidebar/header/container keyword + link
         density match
      5. a same-site or common-nav-word short-link scan near the top of the
         document (fix #2: replaces the original's hardcoded domain with a
         ``urlparse().netloc`` comparison against ``base_url``)
      6. a tight 2-8 link cluster near the top of the document
      7. a dense-link-cluster fallback (``_detect_link_cluster``)

    Unlike the original (which, in the explicit-``<nav>``-tag branch,
    returned the *unmodified* full HTML as content — relying on a separate
    href-based dedup pass elsewhere to avoid double-numbering), every branch
    here actually removes what it classifies as nav from the returned
    content_html, so nav/content links can't collide.
    """
    html_for_nav = re.sub(r"<(article|main)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)

    nav_parts: list[str] = []
    content_html = html_for_nav

    # 1. Explicit <nav> tags.
    nav_matches = re.findall(r"<nav[^>]*>.*?</nav>", html_for_nav, flags=re.DOTALL | re.IGNORECASE)
    if nav_matches:
        content_html = html
        for match in nav_matches:
            content_html = content_html.replace(match, "", 1)
        return content_html, "\n".join(nav_matches)

    # 2. <header> tags dense with links.
    header_matches = re.findall(r"<header[^>]*>.*?</header>", html, flags=re.DOTALL | re.IGNORECASE)
    for match in header_matches:
        link_count = len(re.findall(r"<a[^>]+href=", match, re.IGNORECASE))
        if link_count >= nav_threshold:
            nav_parts.append(match)
            content_html = content_html.replace(match, "")

    # 3. CSS dropdown menu wrappers.
    dropdown_wrapper_pattern = r'<div[^>]+class=["\'][^"\']*dropdown[^"\']*["\'][^>]*>'
    dropdown_parts: list[str] = []
    for match in re.finditer(dropdown_wrapper_pattern, content_html, flags=re.IGNORECASE):
        div_content = _extract_balanced_tag(content_html, match.start(), "div")
        if div_content and (
            "dropdown-content" in div_content.lower()
            or len(re.findall(r"<a[^>]+href=", div_content, re.IGNORECASE)) >= 2
        ):
            dropdown_parts.append(div_content)
    for part in dropdown_parts:
        if part in content_html:
            content_html = content_html.replace(part, "", 1)
    nav_parts.extend(dropdown_parts)

    # 4. class/id nav keyword + link density.
    nav_class_keywords = r"(?:nav|menu|sidebar|header|container)"
    nav_div_pattern = r'<div[^>]+(?:class|id)=["\'][^"\']*' + nav_class_keywords + r'[^"\']*["\'][^>]*>'
    keyword_nav_parts: list[str] = []
    for match in re.finditer(nav_div_pattern, content_html, flags=re.IGNORECASE):
        div_content = _extract_balanced_tag(content_html, match.start(), "div")
        if not div_content:
            continue
        link_count = len(re.findall(r"<a[^>]+href=", div_content, re.IGNORECASE))
        text_only = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", div_content)).strip()
        if link_count >= 3 and (len(text_only) < link_count * 60 or link_count >= nav_threshold):
            keyword_nav_parts.append(div_content)
    for part in keyword_nav_parts:
        content_html = content_html.replace(part, "", 1)
    nav_parts.extend(keyword_nav_parts)

    # 5. Same-site (or common-nav-word) short links near the top of the doc.
    base_netloc = urlparse(base_url).netloc if base_url else ""
    simple_nav_parts: list[str] = []
    for match in re.finditer(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{2,15})</a>',
        content_html[:4000], flags=re.IGNORECASE,
    ):
        href, text = match.group(1), match.group(2).strip()
        same_site = bool(base_netloc) and urlparse(href).netloc == base_netloc
        if text.lower() in _NAV_WORDS or (len(text) < 12 and (href.startswith("/") or same_site)):
            simple_nav_parts.append(match.group(0))
    for part in simple_nav_parts:
        if part in content_html:
            content_html = content_html.replace(part, "", 1)
    nav_parts.extend(simple_nav_parts)

    # 6. A tight cluster of 2-8 short links near the top of the document.
    top_links: list[tuple[str, str, str]] = []
    for match in re.finditer(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{3,30})</a>',
        content_html[:3000], flags=re.IGNORECASE,
    ):
        href, text = match.group(1), match.group(2).strip()
        if len(text) < 30 and text.lower() not in _TOP_LINK_EXCLUDE:
            top_links.append((match.group(0), href, text))
    if 2 <= len(top_links) <= 8:
        for link_html, _href, _text in top_links:
            nav_parts.append(link_html)
            content_html = content_html.replace(link_html, "", 1)

    # 7. Dense-link-cluster fallback.
    if not nav_parts:
        cluster_parts, content_html = _detect_link_cluster(content_html, nav_threshold)
        nav_parts.extend(cluster_parts)

    return content_html, "\n".join(nav_parts)


# --------------------------------------------------------------------------
# Nav link extraction
# --------------------------------------------------------------------------

_SOCIAL_TEXT_RE = re.compile(
    r"^(facebook|twitter|instagram|linkedin|youtube|tiktok|snapchat|pinterest|github)$",
    re.IGNORECASE,
)
_PAGINATION_TEXT_RE = re.compile(
    r"^(?:\d+|next|prev|previous|last|first|load more|older|newer|\.\.\.|more)$",
    re.IGNORECASE,
)
_NAV_LINK_CAP = 75


def _extract_nav_links(nav_html: str, base_url: str, start_number: int, ascii_mode: bool = False) -> list[Link]:
    """Extract, dedup, and number links from a nav section.

    Ported from ``htmlview.py``'s ``HTMLParser._extract_nav_links``
    (pagination/social-icon filtering, href dedup, cap at 75).
    """
    links: list[Link] = []
    seen_hrefs: set[str] = set()
    n = start_number - 1

    for match in re.finditer(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        nav_html, flags=re.DOTALL | re.IGNORECASE,
    ):
        href = match.group(1)
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()

        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        text = decode_html_entities(text, ascii_mode)
        text = re.sub(r"\s+", " ", text).strip()

        if _SOCIAL_TEXT_RE.match(text):
            continue
        if _PAGINATION_TEXT_RE.match(text):
            continue
        if href in seen_hrefs:
            continue

        if text and len(text) < 100:
            n += 1
            links.append(Link(number=n, url=_resolve_url(href, base_url), text=text, kind="nav"))
            seen_hrefs.add(href)
            if len(links) >= _NAV_LINK_CAP:
                break

    return links


# --------------------------------------------------------------------------
# Title extraction
# --------------------------------------------------------------------------


def _extract_title(html: str, ascii_mode: bool = False) -> str | None:
    """Extract a page title: ``<title>`` first, falling back to the first
    ``<h1>``/``<h2>``/``<h3>``.

    Ported from ``htmlview.py``'s ``HTMLViewer._extract_title`` (the
    terminal-width-based truncation is dropped — that's a display concern
    for ``DocumentView``/its consumers, not something a parse step should
    bake in).
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.DOTALL | re.IGNORECASE)
    if match:
        title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        title = decode_html_entities(title, ascii_mode)
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            return title

    for tag in ("h1", "h2", "h3"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, flags=re.DOTALL | re.IGNORECASE)
        if match:
            title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            title = decode_html_entities(title, ascii_mode)
            title = re.sub(r"\s+", " ", title).strip()
            if title and len(title) < 100:
                return title

    return None


# --------------------------------------------------------------------------
# HTML -> blocks
# --------------------------------------------------------------------------

_JUNK_LINK_TEXT_RES = (
    re.compile(r"^[\d.,]+$"),
    re.compile(r"^(\d+)\s*(reviews?|stars?|votes?|ratings?)$", re.IGNORECASE),
)

# Sentinel-token family markers used to splice block-level elements (lists,
# headings, quotes, preformatted text) back into their correct document
# position after they've been pulled out of the HTML stream. \x00 can't
# appear in real HTML text, so these tokens can't collide with content.
_TOKEN_RE = re.compile(r"\x00(PRE|H|Q|LI)(\d+)\x00")


def _html_to_blocks(content_html: str, base_url: str, start_number: int, ascii_mode: bool = False) -> tuple[list[Block], list[Link]]:
    """Convert content HTML into ``(blocks, links)``.

    Functional re-implementation of ``htmlview.py``'s
    ``HTMLParser._html_to_text``/``_clean_text``, restructured to produce
    ``Block`` objects (headings, list items, quotes, preformatted, and
    paragraphs) instead of a flat wrapped-text stream, with inline ``[N]``
    link markers baked into text exactly as the original did.

    Per fix #4, ``<pre>``/``<code>`` content is pulled out into opaque
    placeholder tokens *before* any whitespace-collapsing regex runs, so it
    survives with its original whitespace intact — the source material had
    no ``<pre>`` handling at all, so this is new work, not a port.
    """
    links: list[Link] = []
    counter = [start_number - 1]

    # 1. Pull out <pre> (and any leftover standalone <code>) blocks FIRST,
    #    before any whitespace collapsing, entity-decoding them but leaving
    #    whitespace untouched.
    pre_store: list[str] = []

    def _stash_pre(match: re.Match) -> str:
        inner = re.sub(r"<[^>]+>", "", match.group(1))
        inner = decode_html_entities(inner, ascii_mode)
        pre_store.append(inner)
        return f"\x00PRE{len(pre_store) - 1}\x00"

    content_html = re.sub(r"<pre[^>]*>(.*?)</pre>", _stash_pre, content_html, flags=re.DOTALL | re.IGNORECASE)
    content_html = re.sub(r"<code[^>]*>(.*?)</code>", _stash_pre, content_html, flags=re.DOTALL | re.IGNORECASE)

    # 2. Inline links -> "text [N]" markers, tracking Link objects.
    def _replace_link(match: re.Match) -> str:
        href = match.group(1)
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()

        if not href or href.startswith("#") or href.startswith("javascript:"):
            return text

        text = decode_html_entities(text, ascii_mode)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""

        # Skip junk links: pure numbers, review/rating counts (ported as-is
        # from the source material's e-commerce-site filtering).
        if any(p.match(text) for p in _JUNK_LINK_TEXT_RES):
            return text
        if len(text) <= 2 and text.isdigit():
            return text

        counter[0] += 1
        n = counter[0]
        links.append(Link(number=n, url=_resolve_url(href, base_url), text=text, kind="content"))
        return f"{text} [{n}]"

    content_html = re.sub(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        _replace_link, content_html, flags=re.DOTALL | re.IGNORECASE,
    )

    # 3. Ordered/unordered lists -> LI placeholder tokens (one per <li>).
    li_store: list[tuple[str, bool]] = []

    def _stash_li(text: str, ordered: bool) -> str:
        li_store.append((text, ordered))
        return f"\x00LI{len(li_store) - 1}\x00"

    def _replace_ol_block(match: re.Match) -> str:
        def _replace_ol_item(im: re.Match) -> str:
            inner = re.sub(r"<[^>]+>", " ", im.group(1))
            return "\n" + _stash_li(inner, True) + "\n"
        return "\n" + re.sub(r"<li[^>]*>(.*?)</li>", _replace_ol_item, match.group(1), flags=re.DOTALL | re.IGNORECASE) + "\n"

    content_html = re.sub(r"<ol[^>]*>(.*?)</ol>", _replace_ol_block, content_html, flags=re.DOTALL | re.IGNORECASE)

    def _replace_ul_block(match: re.Match) -> str:
        def _replace_ul_item(im: re.Match) -> str:
            inner = re.sub(r"<[^>]+>", " ", im.group(1))
            return "\n" + _stash_li(inner, False) + "\n"
        return "\n" + re.sub(r"<li[^>]*>(.*?)</li>", _replace_ul_item, match.group(1), flags=re.DOTALL | re.IGNORECASE) + "\n"

    content_html = re.sub(r"<ul[^>]*>(.*?)</ul>", _replace_ul_block, content_html, flags=re.DOTALL | re.IGNORECASE)

    # 4. Headings -> H placeholder tokens (so they become their own block
    #    instead of merging into a surrounding paragraph).
    heading_store: list[tuple[int, str]] = []

    def _stash_heading(match: re.Match) -> str:
        level = int(match.group(1))
        inner = re.sub(r"<[^>]+>", " ", match.group(2))
        heading_store.append((level, inner))
        return f"\n\x00H{len(heading_store) - 1}\x00\n"

    content_html = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", _stash_heading, content_html, flags=re.DOTALL | re.IGNORECASE)

    # 5. Blockquotes -> Q placeholder tokens.
    quote_store: list[str] = []

    def _stash_quote(match: re.Match) -> str:
        inner = re.sub(r"<[^>]+>", " ", match.group(1))
        quote_store.append(inner)
        return f"\n\x00Q{len(quote_store) - 1}\x00\n"

    content_html = re.sub(r"<blockquote[^>]*>(.*?)</blockquote>", _stash_quote, content_html, flags=re.DOTALL | re.IGNORECASE)

    # 6. Paragraph-break boundaries (same source patterns as the original).
    content_html = re.sub(r"<br\s*/?>\s*<br\s*/?>", "\n\n", content_html, flags=re.IGNORECASE)
    content_html = re.sub(r"</(p|div|article|section)>", "\n\n", content_html, flags=re.IGNORECASE)
    content_html = re.sub(r"<(p|div|article|section)[^>]*>", "\n\n", content_html, flags=re.IGNORECASE)
    content_html = re.sub(r"</(tr)>", "\n", content_html, flags=re.IGNORECASE)
    content_html = re.sub(r"<(tr)[^>]*>", "\n", content_html, flags=re.IGNORECASE)
    content_html = re.sub(r"<br\s*/?>", "\n", content_html, flags=re.IGNORECASE)

    # 7. Strip any remaining tags. Everything left in content_html at this
    #    point is either plain text or one of our opaque \x00-delimited
    #    tokens (which contain no '<'/'>' so this can't touch them).
    content_html = re.sub(r"<[^>]+>", "", content_html)

    # 8. Split on tokens (re.split with a capturing group keeps them,
    #    interleaved with the surrounding text, in document order) and
    #    build the final block list.
    blocks: list[Block] = []
    parts = _TOKEN_RE.split(content_html)

    def _flush_paragraphs(text: str) -> None:
        text = decode_html_entities(text, ascii_mode)
        for para in re.split(r"\n\s*\n", text):
            collapsed = re.sub(r"\s+", " ", para).strip()
            if collapsed:
                blocks.append(Block(kind="paragraph", text=collapsed))

    i = 0
    while i < len(parts):
        _flush_paragraphs(parts[i])
        i += 1
        if i >= len(parts):
            break
        kind, idx_str = parts[i], parts[i + 1]
        idx = int(idx_str)
        i += 2
        if kind == "PRE":
            blocks.append(Block(kind="preformatted", text=pre_store[idx]))
        elif kind == "H":
            level, htext = heading_store[idx]
            htext = re.sub(r"\s+", " ", decode_html_entities(htext, ascii_mode)).strip()
            if htext:
                blocks.append(Block(kind="heading", text=htext, level=level))
        elif kind == "Q":
            qtext = re.sub(r"\s+", " ", decode_html_entities(quote_store[idx], ascii_mode)).strip()
            if qtext:
                blocks.append(Block(kind="quote", text=qtext))
        elif kind == "LI":
            litext, ordered = li_store[idx]
            litext = re.sub(r"\s+", " ", decode_html_entities(litext, ascii_mode)).strip()
            if litext:
                blocks.append(Block(kind="list_item", text=litext, ordered=ordered))

    return blocks, links


# --------------------------------------------------------------------------
# Public parsers
# --------------------------------------------------------------------------


def parse_html(html: str, base_url: str = "", ascii_mode: bool = False) -> Document:
    """Parse an HTML page into a protocol-agnostic ``Document``.

    ``ascii_mode`` (Settings -> General -> "ASCII-safe mode") is threaded
    straight through to every ``decode_html_entities`` call underneath this
    (title, nav links, body blocks) — see that function's docstring.
    """
    title = _extract_title(html, ascii_mode)
    stripped = _strip_noise(html)
    content_html, nav_html = _separate_nav_content(stripped, base_url)
    blocks, content_links = _html_to_blocks(content_html, base_url, start_number=1, ascii_mode=ascii_mode)
    nav_links = _extract_nav_links(nav_html, base_url, start_number=len(content_links) + 1, ascii_mode=ascii_mode)
    return Document(title=title, blocks=blocks, links=content_links + nav_links, source_url=base_url)


# Ported from bpq-apps/apps/gopher.py's GopherClient.ITEM_TYPES.
ITEM_TYPES = {
    "0": "TXT", "1": "DIR", "2": "CSO", "3": "ERR", "4": "BHX", "5": "BIN",
    "6": "UUE", "7": "SRH", "8": "TEL", "9": "BIN", "+": "MIR", "g": "GIF",
    "I": "IMG", "p": "PNG", "T": "TN3", "h": "HTM", "i": "INF", "s": "SND",
    ":": "BMP", ";": "MOV", "d": "DOC",
}


def parse_gopher_url(url: str) -> GopherRef:
    """Parse a ``gopher://`` URL into its components.

    Ported from ``gopher.py``'s ``GopherClient.parse_gopher_url``.
    """
    if not url.startswith("gopher://"):
        url = "gopher://" + url

    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 70
    path = parsed.path or "/"

    if len(path) > 1:
        item_type = path[1]
        selector = path[2:] if len(path) > 2 else ""
    else:
        item_type = "1"
        selector = ""

    return GopherRef(host=host, port=port, item_type=item_type, selector=selector)


def parse_gopher_menu(content: str, base_url: str = "", title: str | None = None) -> Document:
    """Parse a tab-delimited Gopher menu into a ``Document``.

    Ported from ``gopher.py``'s ``GopherClient.parse_gopher_menu``. ``'i'``
    (informational) lines become plain paragraph blocks; every selectable
    item becomes a ``menu_item`` block whose ``.link.url`` is a synthesized
    ``gopher://host:port/{type}{selector}`` URL, with ``Link.meta
    ["gopher_type"]`` set and a human-readable type tag (from ``ITEM_TYPES``)
    folded into the block text.
    """
    blocks: list[Block] = []
    links: list[Link] = []
    n = 0

    for raw_line in content.split("\n"):
        line = raw_line.rstrip("\r")
        if not line or line == ".":
            continue

        if "\t" in line:
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            type_and_display = parts[0]
            if not type_and_display:
                continue
            item_type = type_and_display[0]
            display = type_and_display[1:]
            selector = parts[1]
            host = parts[2]
            try:
                port = int(parts[3].strip())
            except ValueError:
                port = 70

            if item_type == "i":
                blocks.append(Block(kind="paragraph", text=display))
                continue

            n += 1
            url = f"gopher://{host}:{port}/{item_type}{selector}"
            type_label = ITEM_TYPES.get(item_type, "UNK")
            link = Link(number=n, url=url, text=display, kind="content", meta={"gopher_type": item_type})
            links.append(link)
            blocks.append(Block(kind="menu_item", text=f"[{n}] {type_label}  {display}", link=link))
        elif line.startswith("i"):
            blocks.append(Block(kind="paragraph", text=line[1:]))

    return Document(title=title, blocks=blocks, links=links, source_url=base_url)


def parse_gemtext(content: str, base_url: str = "", title: str | None = None) -> Document:
    """Parse a gemtext (``text/gemini``) document into a ``Document``.

    Implemented directly from the gemtext spec (no source material to port
    — gemini:// isn't in bpq-apps): ``=>`` link lines (optionally followed
    by whitespace-separated link text), ``#``/``##``/``###`` heading levels
    1-3, ``* `` list items, ``> `` quotes, and a ``` ``` ``` fence toggling a
    preformatted block (verbatim between fences; text after the opening
    fence captured as a caption/alt-text). Everything else — including
    blank lines — becomes its own paragraph block: gemtext does NOT reflow
    text across lines, so this is spec-faithful, not a bug.

    Title falls back to the first level-1 (``#``) heading's text if the
    caller didn't supply one.
    """
    blocks: list[Block] = []
    links: list[Link] = []
    n = 0
    in_pre = False
    pre_lines: list[str] = []
    pre_caption: str | None = None
    detected_title = title

    for raw_line in content.split("\n"):
        line = raw_line.rstrip("\r")

        if line.startswith("```"):
            if in_pre:
                blocks.append(Block(kind="preformatted", text="\n".join(pre_lines), caption=pre_caption))
                pre_lines = []
                pre_caption = None
                in_pre = False
            else:
                in_pre = True
                pre_caption = line[3:].strip() or None
            continue

        if in_pre:
            pre_lines.append(line)
            continue

        if line.startswith("=>"):
            rest = line[2:].strip()
            if not rest:
                continue
            parts = rest.split(None, 1)
            url = parts[0]
            link_text = parts[1].strip() if len(parts) > 1 else url
            n += 1
            link = Link(number=n, url=_resolve_url(url, base_url), text=link_text, kind="content")
            links.append(link)
            blocks.append(Block(kind="menu_item", text=f"[{n}] {link_text}", link=link))
            continue

        if line.startswith("###"):
            blocks.append(Block(kind="heading", text=line[3:].strip(), level=3))
            continue
        if line.startswith("##"):
            blocks.append(Block(kind="heading", text=line[2:].strip(), level=2))
            continue
        if line.startswith("#"):
            text = line[1:].strip()
            blocks.append(Block(kind="heading", text=text, level=1))
            if detected_title is None:
                detected_title = text
            continue

        if line.startswith("* "):
            blocks.append(Block(kind="list_item", text=line[2:].strip()))
            continue

        if line.startswith(">"):
            blocks.append(Block(kind="quote", text=line[1:].strip()))
            continue

        blocks.append(Block(kind="paragraph", text=line))

    if in_pre and pre_lines:
        # Unterminated fence at EOF: flush what we have rather than drop it.
        blocks.append(Block(kind="preformatted", text="\n".join(pre_lines), caption=pre_caption))

    return Document(title=detected_title, blocks=blocks, links=links, source_url=base_url)


_HTML_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")


def parse_feed_entry(title: str, html_or_text: str, base_url: str = "", ascii_mode: bool = False) -> Document:
    """Parse a single RSS/Atom entry body into a ``Document``.

    Sniffs for HTML tags; if any are found, routes through ``parse_html``.
    Otherwise wraps each non-blank line as its own paragraph block. Never
    imports ``feedparser`` — feed fetching/parsing-the-feed-itself stays
    M3's job; this only handles a single entry's already-extracted body.
    """
    if _HTML_TAG_RE.search(html_or_text):
        doc = parse_html(html_or_text, base_url=base_url, ascii_mode=ascii_mode)
        doc.title = title or doc.title
        return doc

    blocks = [Block(kind="paragraph", text=line.strip()) for line in html_or_text.split("\n") if line.strip()]
    return Document(title=title, blocks=blocks, source_url=base_url)


# --------------------------------------------------------------------------
# Textual widget
# --------------------------------------------------------------------------

_INLINE_LINK_RE = re.compile(r"\[\d+\]")

# Distinct color for link text/markers, layered ON TOP of the existing
# "dim" marker styling below rather than replacing it — so the fallback
# path (a marker we can't confidently match back to its anchor text) still
# looks the same as before this was added, and only the positively-matched
# spans get the stronger, more "clickable-looking" style. No underline —
# it hurt readability more than it helped signal "clickable". Stays
# visually distinct from #doc-title's bold "$accent" styling via color alone.
_LINK_STYLE = "bright_cyan"


class DocumentView(VerticalScroll):
    """Renders any ``Document``; owns its own scrolling (no manual
    pagination — that's the BBS-era print()/input() pattern this module
    explicitly does not port).

    Reusable across the Browser (M2), News (M3), and rich-HTML-email (M4)
    consumers, so it carries its own ``DEFAULT_CSS`` rather than living in
    this app's single big App-level ``CSS`` string (see ``main.py``).
    """

    can_focus = True

    DEFAULT_CSS = """
    DocumentView {
        height: 1fr;
    }
    DocumentView #doc-nav {
        display: none;
        color: $text-muted;
        border-bottom: solid $panel-darken-2;
        padding-bottom: 1;
        margin-bottom: 1;
    }
    DocumentView #doc-nav.-visible {
        display: block;
    }
    DocumentView #doc-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    DocumentView #doc-body {
        height: auto;
    }
    """

    document: reactive[Document | None] = reactive(None)

    class LinkActivated(Message):
        """Posted when the user selects a numbered link (digits + Enter)."""

        def __init__(self, link: Link) -> None:
            self.link = link
            super().__init__()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._num_buffer = ""

    def compose(self) -> ComposeResult:
        yield Static(id="doc-nav")
        yield Static(id="doc-title")
        yield Static(id="doc-body")

    def watch_document(self, document: Document | None) -> None:
        nav_widget = self.query_one("#doc-nav", Static)
        title_widget = self.query_one("#doc-title", Static)
        body_widget = self.query_one("#doc-body", Static)

        self._num_buffer = ""

        if document is None:
            nav_widget.update("")
            nav_widget.remove_class("-visible")
            title_widget.update("")
            body_widget.update("")
            return

        title_widget.update(document.title or "(untitled)")

        nav_links = [l for l in document.links if l.kind == "nav"]
        if nav_links:
            nav_widget.update(self._render_nav(nav_links))
            nav_widget.add_class("-visible")
        else:
            nav_widget.update("")
            nav_widget.remove_class("-visible")

        content_links = [l for l in document.links if l.kind == "content"]
        body_widget.update(
            Group(*self._render_blocks(document.blocks, content_links)) if document.blocks else ""
        )
        self.scroll_home(animate=False)

    # -- rendering helpers --------------------------------------------------

    def _render_nav(self, nav_links: list[Link]) -> Text:
        text = Text("  ".join(f"[{l.number}] {l.text}" for l in nav_links))
        for m in _INLINE_LINK_RE.finditer(text.plain):
            text.stylize("dim", m.start(), m.end())
        # Nav links are formatted "[N] text" (marker BEFORE the anchor text
        # here, unlike inline content links below) — style the whole
        # "[N] text" span so the link's label reads as a link, not just its
        # bracketed number.
        for l in nav_links:
            span = f"[{l.number}] {l.text}"
            idx = text.plain.find(span)
            if idx != -1:
                text.stylize(_LINK_STYLE, idx, idx + len(span))
        return text

    def _render_blocks(self, blocks: list[Block], content_links: list[Link]) -> list[Text]:
        rendered: list[Text] = []
        for block in blocks:
            rendered.append(self._render_block(block, content_links))
            rendered.append(Text(""))
        if rendered:
            rendered.pop()  # drop the trailing spacer
        return rendered

    def _render_block(self, block: Block, content_links: list[Link]) -> Text:
        if block.kind == "preformatted":
            plain = "\n".join("    " + line for line in block.text.split("\n"))
            text = Text(plain, no_wrap=True)
            return text

        if block.kind == "heading":
            plain = "#" * max(block.level, 1) + " " + block.text
            text = Text(plain)
            text.stylize("bold")
            self._stylize_links(text, content_links)
            return text

        if block.kind == "list_item":
            marker = f"{max(block.level, 1)}." if block.ordered else "-"
            plain = f"  {marker} {block.text}"
            text = Text(plain)
            self._stylize_links(text, content_links)
            return text

        if block.kind == "quote":
            plain = f"> {block.text}"
            text = Text(plain)
            text.stylize("dim")
            return text

        # "paragraph" and "menu_item" both render as plain styled text with
        # inline [N] markers dimmed.
        text = Text(block.text)
        if block.link is not None:
            # Gopher/Gemini menu items (see parse_gopher_menu/parse_gemtext):
            # the WHOLE block is the link -- block.text is already exactly
            # "[N] label", bracket first, unlike the "label [N]" ordering
            # _stylize_links' substring search looks for below. No need to
            # search for a span here: style the entire line as a link.
            text.stylize(_LINK_STYLE)
        else:
            self._stylize_links(text, content_links)
        return text

    def _stylize_links(self, text: Text, content_links: list[Link]) -> None:
        for m in _INLINE_LINK_RE.finditer(text.plain):
            text.stylize("dim", m.start(), m.end())
        # Inline content links are baked into block text as "text [N]" (see
        # _html_to_blocks' _replace_link) — find that exact span per link and
        # layer the stronger link style over it so the anchor text itself
        # (not just the bracketed number) reads as a link. A block only ever
        # contains a handful of a document's links, so scanning the whole
        # list per block is cheap; a link whose formatted span isn't found
        # (block text was reshaped somewhere unexpected) just keeps the
        # plain "dim" marker from the pass above instead of erroring.
        for l in content_links:
            span = f"{l.text} [{l.number}]"
            idx = text.plain.find(span)
            if idx != -1:
                text.stylize(_LINK_STYLE, idx, idx + len(span))

    # -- link activation ------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if event.character is not None and event.character.isdigit():
            self._num_buffer += event.character
            event.stop()
        elif event.key == "enter":
            if self._num_buffer:
                self._activate_link(int(self._num_buffer))
                self._num_buffer = ""
                event.stop()
        elif event.key == "escape":
            if self._num_buffer:
                self._num_buffer = ""
                event.stop()

    def _activate_link(self, number: int) -> None:
        if self.document is None:
            return
        for link in self.document.links:
            if link.number == number:
                self.post_message(self.LinkActivated(link))
                return

    # -- instant paging (see ROADMAP/CHANGELOG for the perf investigation) -

    # Textual's default Widget.action_page_up/down/scroll_home/scroll_end
    # animate the scroll (see widget.py's scroll_page_up/down: default
    # speed=50 "lines per second" when neither speed nor duration is given;
    # scroll_home/scroll_end instead default to a flat 1.0s duration
    # regardless of distance). Profiled with a fabricated ~2000-block
    # document: PageDown consistently cost ~0.68s per press (every single
    # press, not just the first) and Home/End cost ~1.0s each, REGARDLESS
    # of total document size (300 vs. 2000 vs. 5000 blocks all measured the
    # same ~0.68s/~1.0s) -- exactly what you'd expect from a fixed scroll
    # SPEED over a roughly-constant per-page distance (viewport height), not
    # a cost proportional to content length. The actual block-rendering work
    # (_render_blocks) is cheap even at 5000 blocks (~40ms, done once when
    # .document is set, not per scroll). Overriding these four actions to
    # scroll instantly restores the "jump immediately" behaviour expected of
    # a pager/document reader instead of Textual's default smooth-glide,
    # which is a fine default for small on-screen nudges but reads as "hung"
    # for a multi-second jump to the end of a long page.
    def action_page_up(self) -> None:
        self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.scroll_page_down(animate=False)

    def action_scroll_home(self) -> None:
        self.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self.scroll_end(animate=False)
