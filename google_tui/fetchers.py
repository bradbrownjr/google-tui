"""google_tui.fetchers — blocking I/O fetchers for the Browser tab (M2),
the News tab's feed fetch (M3), and the Navigation tab's Routes API call (M6).

HTTP(S), Gopher, and Gemini fetch each return a ``render.Document``;
``compute_route`` (Navigation) returns a plain ``RouteResult`` dataclass
instead, since there's nothing to hyperlink-navigate in a turn-by-turn step
list. ``render.py`` itself does zero I/O (see its module docstring); this is
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
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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


def fetch_http(url: str, timeout: int = 20, ascii_mode: bool = False) -> render.Document:
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
        return render.parse_html(resp.text, base_url=resp.url, ascii_mode=ascii_mode)
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


# ---------------------------------------------------------------------------
# Web search — Browser tab Search mode (replaces the defunct
# ``hermes web search`` shell-out, see ROADMAP/CHANGELOG). Three backends
# (Google Programmable Search / DuckDuckGo HTML / SearXNG), each turned into
# a ``render.Document`` with real numbered ``[N]`` links so Search results
# navigate exactly like Gopher/Gemini menus and HTTP page links do.
# ---------------------------------------------------------------------------

# A browser-like User-Agent is required here: DuckDuckGo's HTML endpoint
# (unlike its JSON/instant-answer API) 403s requests carrying a generic/
# empty or clearly-non-browser User-Agent — confirmed empirically against
# this app's default ``DEFAULT_USER_AGENT``.
_DDG_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_DDG_A_RE = re.compile(r'<a\s+([^>]*\bclass="result__a"[^>]*)>(.*?)</a>', re.S)
_DDG_SNIPPET_RE = re.compile(
    r'\bclass="result__snippet"[^>]*>(.*?)</(?:a|div)>', re.S)
_HREF_RE = re.compile(r'href="([^"]*)"')


def _strip_tags(html: str, ascii_mode: bool = False) -> str:
    return render.decode_html_entities(_TAG_RE.sub("", html), ascii_mode).strip()


def _unwrap_ddg_redirect(href: str) -> str:
    """DuckDuckGo's HTML results wrap outbound links in a redirector
    (``//duckduckgo.com/l/?uddg=<url-encoded-target>&rut=...``) — unwrap it
    so numbered links go straight to the real target.
    """
    if not href:
        return href
    candidate = "https:" + href if href.startswith("//") else href
    parsed = urlparse(candidate)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg")
        if target:
            return unquote(target[0])
    return href


def _search_results_to_document(query: str, results: list[tuple[str, str, str]]) -> render.Document:
    """Shared helper: turn a flat list of ``(title, url, snippet)`` tuples
    into a ``Document`` with real numbered ``[N]`` links — matching how
    every other Browser mode (Gopher/Gemini menus, HTTP page links) already
    supports digit + ``Enter`` navigation.
    """
    if not results:
        return render.Document(
            title=f"Search: {query}",
            blocks=[render.Block(kind="paragraph", text="(no results)")],
            links=[],
        )

    blocks: list[render.Block] = []
    links: list[render.Link] = []
    for i, (title, url, snippet) in enumerate(results, start=1):
        label = title or url
        links.append(render.Link(number=i, url=url, text=label, kind="content"))
        blocks.append(render.Block(kind="paragraph", text=f"[{i}] {label}"))
        if snippet:
            blocks.append(render.Block(kind="paragraph", text=f"    {snippet}"))

    return render.Document(title=f"Search: {query}", blocks=blocks, links=links)


def search_google_cse(query: str, api_key: str, cse_id: str, timeout: int = 15) -> render.Document:
    """Google Programmable Search (Custom Search JSON API). Needs an API
    key + Search Engine ID ("cx") — see SETUP.md for how to create both.
    """
    resp = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": api_key, "cx": cse_id, "q": query},
        timeout=timeout,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    results = [(item.get("title", ""), item.get("link", ""), item.get("snippet", ""))
               for item in items]
    return _search_results_to_document(query, results)


def search_duckduckgo(query: str, timeout: int = 15, ascii_mode: bool = False) -> render.Document:
    """DuckDuckGo's non-JS HTML results page. No API key needed — this is
    the no-config-needed baseline every other search path falls back to.
    """
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": _DDG_USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return _search_results_to_document(query, [])

    titles_urls: list[tuple[str, str]] = []
    for attrs, inner in _DDG_A_RE.findall(resp.text):
        href_m = _HREF_RE.search(attrs)
        href = _unwrap_ddg_redirect(href_m.group(1)) if href_m else ""
        title = _strip_tags(inner, ascii_mode)
        if href:
            titles_urls.append((title, href))

    snippets = [_strip_tags(s, ascii_mode) for s in _DDG_SNIPPET_RE.findall(resp.text)]

    results = []
    for i, (title, url) in enumerate(titles_urls):
        snippet = snippets[i] if i < len(snippets) else ""
        results.append((title, url, snippet))

    return _search_results_to_document(query, results)


def search_searxng(query: str, base_url: str, timeout: int = 15, ascii_mode: bool = False) -> render.Document:
    """SearXNG instance search. Tries JSON output first (most instances
    support ``format=json``); some public instances disable it, in which
    case this falls back to fetching the plain HTML results page and
    routing it through the existing ``render.parse_html`` rather than
    writing a second bespoke parser.
    """
    url = f"{base_url.rstrip('/')}/search"
    try:
        resp = requests.get(
            url, params={"q": query, "format": "json"},
            headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [(r.get("title", ""), r.get("url", ""), r.get("content", ""))
                   for r in data.get("results", [])]
        return _search_results_to_document(query, results)
    except Exception:
        resp = requests.get(
            url, params={"q": query},
            headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=timeout,
        )
        resp.raise_for_status()
        return render.parse_html(resp.text, base_url=resp.url, ascii_mode=ascii_mode)


def run_search(query: str, settings, timeout: int = 15) -> render.Document:
    """Dispatch to the configured search provider, with DuckDuckGo as the
    reliable no-config-needed fallback for every path — see AGENTS.md /
    the task brief for the exact fallback chain. This is what replaces the
    old, now-broken ``hermes web search`` shell-out.

    Reads ``settings.ascii_mode`` (P2, 2026-07-15 "ASCII-safe mode") itself
    and threads it into every backend, rather than the caller passing it
    separately — every other knob this function reads (provider, API keys)
    already comes off the same ``settings`` object.
    """
    provider = settings.search_provider
    ascii_mode = getattr(settings, "ascii_mode", False)

    if provider == "google":
        if settings.google_cse_api_key and settings.google_cse_id:
            try:
                return search_google_cse(query, settings.google_cse_api_key,
                                          settings.google_cse_id, timeout=timeout)
            except Exception:
                return search_duckduckgo(query, timeout=timeout, ascii_mode=ascii_mode)
        return search_duckduckgo(query, timeout=timeout, ascii_mode=ascii_mode)

    if provider == "searxng":
        if settings.searxng_url:
            try:
                return search_searxng(query, settings.searxng_url, timeout=timeout, ascii_mode=ascii_mode)
            except Exception:
                return search_duckduckgo(query, timeout=timeout, ascii_mode=ascii_mode)
        return search_duckduckgo(query, timeout=timeout, ascii_mode=ascii_mode)

    return search_duckduckgo(query, timeout=timeout, ascii_mode=ascii_mode)


# ---------------------------------------------------------------------------
# Routes API (Navigation tab, P1 M6) — driving directions.
#
# Unlike this module's other fetchers (query-param API keys via
# `requests.get`), the Routes API needs a JSON POST body plus
# `X-Goog-Api-Key`/`X-Goog-FieldMask` headers. There's no fallback provider
# for driving directions, so unlike `run_search`'s silent DuckDuckGo
# fallback, every failure mode here raises `BrowserFetchError` with a
# user-facing message instead of degrading quietly.
# ---------------------------------------------------------------------------

ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ROUTES_FIELD_MASK = (
    "routes.distanceMeters,routes.duration,routes.localizedValues,"
    "routes.legs.steps.navigationInstruction,routes.legs.steps.distanceMeters,"
    "routes.legs.steps.staticDuration,routes.legs.steps.localizedValues"
)


@dataclass
class RouteStep:
    instruction: str        # navigationInstruction.instructions
    distance_text: str      # step localizedValues.distance.text, e.g. "0.3 mi"
    duration_text: str      # step localizedValues.staticDuration.text, e.g. "1 min"


@dataclass
class RouteResult:
    origin: str
    destination: str
    distance_meters: int
    duration_seconds: int   # parsed from routes[0].duration ("772s" -> 772)
    distance_text: str      # route-level localizedValues.distance.text
    duration_text: str      # route-level localizedValues.duration.text (traffic-aware)
    steps: list[RouteStep]


def compute_route(origin: str, destination: str, api_key: str,
                   units: str = "IMPERIAL", timeout: int = 15, ascii_mode: bool = False) -> RouteResult:
    """POST to the Routes API's computeRoutes endpoint and return a flat,
    already-parsed ``RouteResult``. Returns a plain dataclass, not a
    ``render.Document`` — there's nothing to hyperlink-navigate in a step
    list, unlike Browser/Search's ``Document``.
    """
    if not api_key:
        raise BrowserFetchError("No Routes API key set — add one in Settings -> Navigation.")
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "routeModifiers": {"avoidTolls": False, "avoidHighways": False, "avoidFerries": False},
        "languageCode": "en-US",
        "units": units,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ROUTES_FIELD_MASK,
    }
    try:
        resp = requests.post(ROUTES_API_URL, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise BrowserFetchError(f"Route request failed: {e}") from e
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text
        hint = ""
        if resp.status_code in (401, 403):
            hint = (" (check your Routes API key in Settings -> Navigation, and that "
                     "Cloud Billing is linked — see SETUP.md §6)")
        raise BrowserFetchError(f"Routes API error: {msg}{hint}")
    try:
        data = resp.json()
    except ValueError as e:
        raise BrowserFetchError(f"Routes API returned invalid JSON: {e}") from e
    routes = data.get("routes") or []
    if not routes:
        raise BrowserFetchError(f"No route found from {origin!r} to {destination!r} — check the addresses.")
    route = routes[0]
    duration_str = route.get("duration", "0s")
    stripped = duration_str.rstrip("s")
    duration_seconds = int(stripped) if stripped.isdigit() else 0
    steps: list[RouteStep] = []
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            instr = step.get("navigationInstruction", {}).get("instructions", "")
            instr = render.decode_html_entities(instr, ascii_mode)
            lv = step.get("localizedValues", {})
            steps.append(RouteStep(
                instruction=instr,
                distance_text=lv.get("distance", {}).get("text", ""),
                duration_text=lv.get("staticDuration", {}).get("text", ""),
            ))
    route_lv = route.get("localizedValues", {})
    return RouteResult(
        origin=origin, destination=destination,
        distance_meters=route.get("distanceMeters", 0),
        duration_seconds=duration_seconds,
        distance_text=route_lv.get("distance", {}).get("text", ""),
        duration_text=route_lv.get("duration", {}).get("text", ""),
        steps=steps,
    )
