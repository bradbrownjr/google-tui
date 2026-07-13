"""google_tui.fetchers — blocking I/O fetchers for the Browser tab (M2).

HTTP(S), Gopher, and Gemini fetch, each returning a ``render.Document``.
``render.py`` itself does zero I/O (see its module docstring); this is
where the actual network calls live, mirroring the existing per-concern
module boundary (``gauth.py`` = Google API, ``ask.py`` = AI/search,
``cache.py`` = persistence, ``render.py`` = parsing).

Every ``fetch_*`` here is blocking and is meant to be called via
``self.run_worker(fn, thread=True)`` from ``main.py``, same convention as
``_live_refresh_thread`` — see AGENTS.md's fetch/apply-split NOTE.

No new third-party dependency: ``requests`` is already used by ``ask.py``;
gopher/gemini use only the stdlib (``socket``/``ssl``/``hashlib``).
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import socket
import ssl
from urllib.parse import urljoin, urlparse

import feedparser
import requests

from . import render

DEFAULT_USER_AGENT = "google-tui/0.1 (+https://github.com/bradbrownjr/google-tui)"
GEMINI_DEFAULT_PORT = 1965
GOPHER_DEFAULT_PORT = 70


class BrowserFetchError(Exception):
    """User-facing fetch error — caught by main.py and shown via notify()."""


class GeminiInputRequired(Exception):
    """Raised on a Gemini 1x status: the server wants a line of user input
    appended to the URL (as a query string) before retrying the request.
    """

    def __init__(self, url: str, meta: str, sensitive: bool):
        self.url = url
        self.meta = meta
        self.sensitive = sensitive
        super().__init__(f"Gemini input required ({url}): {meta}")


class GeminiRedirectConfirm(Exception):
    """Raised on a Gemini 3x status pointing at a different host/scheme —
    auto-following is only safe same-host/same-scheme; anything else needs
    the user's explicit OK.
    """

    def __init__(self, from_url: str, to_url: str, hop: int):
        self.from_url = from_url
        self.to_url = to_url
        self.hop = hop
        super().__init__(f"Gemini cross-host redirect: {from_url} -> {to_url}")


# ---------------------------------------------------------------------------
# TOFU (trust-on-first-use) certificate pinning for Gemini
# ---------------------------------------------------------------------------


class GeminiTofuStore:
    """Wraps ``cache.Cache`` (category ``"gemini_cert"``, key
    ``f"{host}:{port}"``) so the trust check reads as one call-site. First
    connection to a host:port pins its cert fingerprint; every later
    connection must match or the fetch is refused.
    """

    def __init__(self, cache) -> None:
        self._cache = cache

    def check_and_pin(self, host: str, port: int, fingerprint: str) -> None:
        if self._cache is None:
            return  # no cache object yet (shouldn't normally happen) — skip pinning
        key = f"{host}:{port}"
        existing = self._cache.get("gemini_cert", key)
        if existing is None:
            self._cache.put("gemini_cert", key, {
                "fingerprint": fingerprint,
                "first_seen": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
            return
        if existing.get("fingerprint") != fingerprint:
            raise BrowserFetchError(
                f"Gemini certificate for {host}:{port} changed since it was "
                f"first trusted (TOFU mismatch) — refusing to connect. If "
                f"this is expected (e.g. the server rotated certs), clear "
                f"the local cache in Settings."
            )


# ---------------------------------------------------------------------------
# HTTP(S)
# ---------------------------------------------------------------------------


def _looks_like_html(text: str) -> bool:
    head = text[:512].lower()
    return "<html" in head or "<!doctype html" in head


def fetch_http(url: str, timeout: int = 20) -> render.Document:
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        raise BrowserFetchError(f"HTTP fetch failed: {e}") from e

    if resp.status_code >= 400:
        raise BrowserFetchError(f"HTTP {resp.status_code} for {url}")

    content_type = resp.headers.get("Content-Type", "")
    mime = content_type.split(";")[0].strip().lower()

    if mime == "text/html" or (not mime and _looks_like_html(resp.text)):
        return render.parse_html(resp.text, base_url=resp.url)
    if mime.startswith("text/") or not mime:
        blocks = [render.Block(kind="paragraph", text=line)
                  for line in resp.text.split("\n") if line.strip()]
        return render.Document(title=url, blocks=blocks, source_url=resp.url)
    raise BrowserFetchError(f"Unsupported content type: {mime}")


# ---------------------------------------------------------------------------
# Gopher
# ---------------------------------------------------------------------------


def fetch_gopher(url: str, timeout: int = 15) -> render.Document:
    ref = render.parse_gopher_url(url)

    # The common "h"-type web-link convention: a menu item whose selector is
    # "URL:<actual http(s) url>", meant to be opened in a real web browser,
    # not fetched as a gopher resource.
    if ref.item_type == "h" and ref.selector.upper().startswith("URL:"):
        target = ref.selector.split(":", 1)[1]
        raise BrowserFetchError(f"This is a web link, not a gopher resource — open it directly: {target}")

    try:
        with socket.create_connection((ref.host, ref.port), timeout=timeout) as sock:
            sock.sendall((ref.selector + "\r\n").encode("utf-8", errors="replace"))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as e:
        raise BrowserFetchError(f"Gopher fetch failed: {e}") from e

    text = b"".join(chunks).decode("utf-8", errors="replace")

    if ref.item_type == "1":
        return render.parse_gopher_menu(text, base_url=url)

    if ref.item_type == "0":
        lines = text.split("\n")
        # Strip a lone "." terminator line, if present (gopher text items
        # are conventionally dot-terminated like SMTP/NNTP bodies).
        while lines and lines[-1].rstrip("\r") in ("", "."):
            lines.pop()
        blocks = [render.Block(kind="paragraph", text=line) for line in lines if line.strip()]
        return render.Document(title=url, blocks=blocks, source_url=url)

    type_label = render.ITEM_TYPES.get(ref.item_type, ref.item_type)
    raise BrowserFetchError(f"Gopher item type '{ref.item_type}' ({type_label}) is not previewable")


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

_MAX_GEMINI_REDIRECT_HOPS = 5


def _make_gemini_ssl_context() -> ssl.SSLContext:
    # Gemini servers overwhelmingly use self-signed certs (that's the norm
    # for the protocol, trust is TOFU-based instead of CA-based) — a default
    # context with hostname/CA verification would fail against real servers,
    # so this deliberately does NOT use ssl.create_default_context().
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def fetch_gemini(url: str, tofu: GeminiTofuStore, timeout: int = 15, _hop: int = 0) -> render.Document:
    if _hop > _MAX_GEMINI_REDIRECT_HOPS:
        raise BrowserFetchError("Too many gemini redirects")

    if len(url.encode("utf-8")) > 1024:
        raise BrowserFetchError("Gemini request URL exceeds the 1024-byte spec limit")

    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise BrowserFetchError(f"Invalid gemini URL: {url}")
    port = parsed.port or GEMINI_DEFAULT_PORT

    ctx = _make_gemini_ssl_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw_sock:
            with ctx.wrap_socket(raw_sock, server_hostname=host) as sock:
                der_cert = sock.getpeercert(binary_form=True)
                if not der_cert:
                    raise BrowserFetchError(f"No certificate presented by {host}:{port}")
                fingerprint = hashlib.sha256(der_cert).hexdigest()
                tofu.check_and_pin(host, port, fingerprint)

                sock.sendall((url + "\r\n").encode("utf-8"))
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
    except ssl.SSLError as e:
        raise BrowserFetchError(f"TLS error connecting to {host}:{port}: {e}") from e
    except OSError as e:
        raise BrowserFetchError(f"Gemini fetch failed: {e}") from e

    raw = b"".join(chunks)
    header_end = raw.find(b"\r\n")
    if header_end == -1:
        raise BrowserFetchError("Malformed gemini response (no header line)")
    header = raw[:header_end].decode("utf-8", errors="replace")
    body = raw[header_end + 2:]

    head_parts = header.split(" ", 1)
    status = head_parts[0].strip()
    meta = head_parts[1].strip() if len(head_parts) > 1 else ""

    if len(status) != 2 or not status.isdigit():
        raise BrowserFetchError(f"Malformed gemini status line: {header!r}")

    category = status[0]

    if category == "1":
        raise GeminiInputRequired(url, meta, sensitive=(status == "11"))

    if category == "2":
        mime = meta or "text/gemini"
        charset = "utf-8"
        if ";" in mime:
            mime, _, params = mime.partition(";")
            mime = mime.strip()
            for p in params.split(";"):
                p = p.strip()
                if p.lower().startswith("charset="):
                    charset = p.split("=", 1)[1].strip().strip('"')
        try:
            text = body.decode(charset, errors="replace")
        except (LookupError, ValueError):
            text = body.decode("utf-8", errors="replace")
        if mime == "text/gemini":
            return render.parse_gemtext(text, base_url=url)
        if mime.startswith("text/"):
            blocks = [render.Block(kind="paragraph", text=line)
                      for line in text.split("\n") if line.strip()]
            return render.Document(title=url, blocks=blocks, source_url=url)
        raise BrowserFetchError(f"Unsupported gemini content type: {mime}")

    if category == "3":
        new_url = urljoin(url, meta)
        new_parsed = urlparse(new_url)
        same_host = new_parsed.hostname == host and new_parsed.scheme == parsed.scheme
        if same_host:
            return fetch_gemini(new_url, tofu, timeout=timeout, _hop=_hop + 1)
        raise GeminiRedirectConfirm(url, new_url, _hop)

    if category in ("4", "5"):
        raise BrowserFetchError(meta or f"Gemini error status {status}")

    if category == "6":
        raise BrowserFetchError("Client certificates aren't supported yet")

    raise BrowserFetchError(f"Unknown gemini status: {status}")


# ---------------------------------------------------------------------------
# Feeds (RSS/Atom) — News tab (M3)
# ---------------------------------------------------------------------------


def fetch_feed(url: str, timeout: int = 15) -> list[dict]:
    """Fetch and parse an RSS/Atom feed, returning a list of plain dicts.

    Deliberately returns dicts, not a custom dataclass — matches ``gauth.py``'s
    convention of list-of-dict return values, which makes caching trivial
    (``Cache.put_many`` stores dicts directly, see the ``feed_entry`` category
    in ``main.py``). ``render.py`` stays fetch-agnostic per its module design
    (see AGENTS.md) — this module owns the network call, same as
    ``fetch_http``/``fetch_gopher``/``fetch_gemini`` above.

    The HTTP fetch itself goes through ``requests`` (already a dependency,
    already used by ``fetch_http``) rather than handing ``feedparser.parse()``
    a bare URL, so this function gets the same timeout/User-Agent control as
    every other fetcher in this module instead of relying on feedparser's own
    (less configurable) URL-fetching path.

    Each returned dict: ``id`` (stable — ``entry.id`` if present, else
    ``entry.link``, else a synthetic fallback), ``title``, ``link``,
    ``summary`` (the entry body — ``content`` if the feed provides full
    content, else ``summary``/description; may be HTML or plain text, sniffed
    by ``render.parse_feed_entry`` later), ``published`` (ISO-8601 UTC string
    derived from feedparser's normalized ``*_parsed`` struct_time when
    available — this makes newest-first sorting a plain string sort in
    ``main.py``, instead of every caller having to cope with the many raw
    date formats real-world feeds use), ``feed_title`` and ``feed_url`` (the
    parsed feed's own title and the subscribed URL, so a combined
    multi-feed view can show provenance and a feed can be located again for
    removal).
    """
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    except requests.RequestException as e:
        raise BrowserFetchError(f"Feed fetch failed: {e}") from e

    if resp.status_code >= 400:
        raise BrowserFetchError(f"HTTP {resp.status_code} for {url}")

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        reason = parsed.get("bozo_exception", "unknown parse error")
        raise BrowserFetchError(f"Feed parse failed for {url}: {reason}")

    feed_title = parsed.feed.get("title") or url

    entries: list[dict] = []
    for i, e in enumerate(parsed.entries):
        if e.get("content"):
            body = e["content"][0].get("value", "")
        else:
            body = e.get("summary", "")

        struct = e.get("published_parsed") or e.get("updated_parsed")
        if struct:
            published = dt.datetime.fromtimestamp(
                calendar.timegm(struct), tz=dt.timezone.utc).isoformat()
        else:
            published = e.get("published") or e.get("updated") or ""

        entries.append({
            "id": e.get("id") or e.get("link") or f"{url}#{i}",
            "title": e.get("title") or "(untitled)",
            "link": e.get("link", ""),
            "summary": body,
            "published": published,
            "feed_title": feed_title,
            "feed_url": url,
        })
    return entries
