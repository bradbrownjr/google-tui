# AGENTS.md — google-tui

Multi-pane terminal TUI for Brad's Google Workspace (Gmail, Calendar, Tasks,
Drive) plus a "Hermes Ask" pane. Built with [Textual](https://textual.textualize.io/).
Python 3.13, package layout under `/home/bradb/google-tui/google_tui/`.

This file is the single source of truth for a future session to continue work
WITHOUT prior chat context. Read it top-to-bottom before touching code.

---

## 1. What the app does

Eight full-width **tabs** live in the blue bar (this IS the styled `Tabs`
bar of the outer `TabbedContent#main-tabs`, not a separate status widget):
**Mail**, **Calendar**, **Drive**, **Browser**, **News**, **Navigation**,
**Settings**, **Contacts**. The Mail tab holds four **panes**: Email,
Events, Tasks, Hermes. Tabs and panes are deliberately different concepts
with different key prefixes (`Ctrl+#` for tabs, `Alt+#` for panes) — see §2.

```
┌[Mail¹]  Calendar²  Drive³  Browser⁴  News⁵  Navigation⁶  Settings⁷  Contacts⁸┐  ← blue bar,
├─ EMAIL (widened) ────────────┐ ┌─ EVENTS ─────────────────────┤    active tab
│ ▸ Frank Krizan                │ │ ▸ 07/13 Tick/Flea Appt       │    has an
│   Fwd: [DigiPi] …             │ │ ▸ 07/15 OHD Water Testing    │    accent-
│                                │ ├─ TASKS ──────────────────────┤    colored
│                                │ │ [ ] Buy cat food             │    background
│                                │ │ [x] Pay electric bill        │
│                                │ ├─ HERMES ASK ─────────────────┤
│                                │ │ > ask a question, Enter      │
└────────────────────────────────┘ └───────────────────────────────┘
  [help bar: contextual row above a static global-shortcuts row]
```

App startup is **cache-first**: whatever was cached from the last run (see
§1a) is applied to the UI instantly, then a background thread reconnects to
Google and refreshes it. `Header.sub_title` shows `Connecting…` →
`Synced HH:MM` or `Offline (cached HH:MM)`. `LoadingModal` only appears on a
genuine first run with an empty cache — the initial live fetch (mail +
calendar + drive) commonly takes ~20s (see the NOTE on `list_threads` below),
so on every run after the first, the app is interactive immediately instead
of blocking on that.

## 1a. Local cache, offline mode, encryption-at-rest

- **`google_tui/cache.py`** — `Cache`, a SQLite (`cache_items(category, key,
  payload, updated_at)`) key/value store, one row per cached object,
  optionally Fernet-encrypted per row. Categories: `thread_summary`,
  `thread_body` (unused so far — bodies aren't cached, only summaries),
  `event`, `task`, `tasklist`, `cal_month` (key `YYYY-MM`), `cal_week` (key
  = the Monday's ISO date), `drive_listing` (key = folder id, only `root`
  is ever fetched today), `drive_file_meta`, `drive_file_text` (both keyed
  by file id, populated lazily — only after a live Drive preview actually
  succeeds for that file, never pre-fetched for a whole folder),
  `gemini_cert` (key `f"{host}:{port}"`, Browser tab's Gemini TOFU pinning,
  P1 M2), `feed_entry` (key = the entry's stable id — `entry.id` or
  `entry.link` — News tab, P1 M3).
  **Design intent**: small "browse" rows (summaries/listings) are cheap to
  bulk-decrypt on every list population; large "content" rows (Drive text)
  are decrypted one at a time, only when opened. This is what makes
  encryption not cost a "potato laptop" anything proportional to total
  cache size — see the module docstring.
- **`google_tui/settings.py`** — `Settings` dataclass persisted as
  **plaintext** JSON at `platformdirs.user_config_dir("google-tui")/
  settings.json` (`encrypt_at_rest`, `key_method`, `kdf_salt`, `canary`).
  Must stay plaintext: the app needs to know the key method before it can
  derive or verify any key.
- **Key methods** (`Settings.key_method`): `"keyfile"` — a random Fernet key
  at `.../cache.key`, chmod 0600, no prompt ever. `"passphrase"` — a key
  derived via scrypt from a passphrase typed at launch (`UnlockModal`,
  mode="unlock"); verified against a stored `canary` (a Fernet-encrypted
  known string) so a wrong passphrase is caught before it's trusted, not
  after decrypting garbage. The passphrase itself is NEVER written to disk;
  only `kdf_salt` and `canary` are.
- **Turning encryption on/off, or switching key method, always
  `Cache.clear_all()`s immediately** (no re-encryption/migration code) and
  tells the user to restart. This is a deliberate simplification — see
  ROADMAP.
- **Offline behavior is intentionally narrow**: `self._online: bool` is set
  by `_apply_live_refresh` after each connect attempt. Reply/Reply All/
  Forward/toggle-task check `self._require_online()` first and just
  `notify(..., severity="warning")` instead of attempting the call — there
  is NO queue-for-later/sync-when-reconnected mechanism. Drive preview reads
  from cache instead of `gauth` when offline. This is "browse cached data
  read-only while offline," not a sync engine.

Mail-tab panes:
- **Email** (left, full height): threaded Gmail list, lightbar. `Enter`
  opens the full thread (`ThreadModal`); `Space` expands/collapses the
  highlighted row IN PLACE (mutates just that one `ListItem`'s `Label` text
  — see `_toggle_thread_expand` — to append the thread's snippet, and a
  `(N messages)` note if `count > 1`; does NOT open `ThreadModal`, and does
  NOT `ListView.clear()`/repopulate). `l` focuses `Select#email-label-select`
  and opens its dropdown (`action_focus_label_select`). `r`/`a`/`f` reply /
  reply-all / forward (compose modal). Unread threads prefixed with a
  bullet `•`. `self._threads_cache: dict[str, dict]` (threadId -> thread
  dict) backs the expand lookup, populated everywhere `_apply_email_list`/
  `_apply_mail_data_async` populate the list; `self._expanded_thread_ids:
  set[str]` tracks which threads are currently expanded and naturally
  resets whenever the list is torn down and repopulated (no persistence).
- **Events** (right top, renamed from "Calendar" to avoid clashing with the
  Calendar tab): next ~3 weeks of events, lightbar, `Enter`/`Space` → detail.
- **Tasks** (right middle): all Google Task lists combined, lightbar.
  `Space` toggles complete (live), `Enter` shows details/subtasks.
- **Hermes Ask** (right bottom): type question, `Enter`. General questions
  answered by the Nous LLM (`tencent/hy3:free`) with live Google context
  injected; action-style questions delegate to the full Hermes agent.

Other tabs:
- **Calendar tab**: nested `TabbedContent#cal-tabs` (Month/Week), unrelated
  to the outer tab bar. Month = `DataTable#cal-grid` with multi-line rows
  (day number + up to 2 events + `+N more`); `Enter`/click on a day opens
  `DayEventsModal`. Week = `DataTable#cal-week-grid`, 24 hour rows x 7 day
  columns, an event's summary is written into every hour row it spans (a
  text-cell approximation of a Gantt block — DataTable is a grid of cells,
  not a pixel canvas). `[`/`]` page the month, or the week when the Week
  sub-tab is active.
- **Drive tab**: `ListView#drive-list` (left) + live preview pane (right).
  Preview updates on `ListView.Highlighted` (cursor movement), not just
  `Selected` — metadata (who/what/where/when) always shown; text preview
  only for `_is_previewable()` mime types. "Up" always reloads root, not the
  true parent folder (pre-existing simplification, not fixed by the tab
  redesign — see §7). Offline: reads `drive_file_meta`/`drive_file_text`
  from cache instead of `gauth`; shows "not available offline" for a file
  that was never viewed while online.
  - `gauth.get_file_metadata(svc, file_id)` — added for the preview's
    who/what/where/when: `fields="id,name,mimeType,size,owners,
    modifiedTime,createdTime,parents,webViewLink"`.
- **Browser tab** (`Ctrl+4`, P1 M2): address bar (`Input#browser-url`) + a
  mode badge (`Static#browser-mode`: WEB/GOPHER/GEMINI/SEARCH) + a
  `render.DocumentView` (`#browser-doc`) rendering whatever came back. A
  "new tab page" row of starter-destination buttons (`Horizontal
  #browser-bookmarks`, between `#browser-bar` and `#browser-doc`) — module-
  level `_BROWSER_BOOKMARKS` list (Google/Wikipedia/Gopherpedia/Gemini
  Protocol, one per non-search protocol) — is visible until the first
  successful page load or search of the session, then gets `.hidden`'d
  permanently (`self._browser_started: bool`, flipped inside
  `_browser_apply_document`) and never reappears; clicking a bookmark
  (`on_button_pressed`'s `browser-bookmark-<i>` branch) navigates exactly
  like typed address-bar input. Address-bar submission is classified by
  `_classify_address()` (omnibox heuristic: explicit
  `http(s)://`/`gopher://`/`gemini://` wins; a single dotted-word-with-no-
  space gets `https://` prepended; everything else, including any text
  containing a space, is a web search via `fetchers.run_search` — see
  below). Fetching lives in `google_tui/fetchers.py`
  (`fetch_http`/`fetch_gopher`/`fetch_gemini`/`run_search` and its three
  backends), never in `render.py` (which stays I/O-free) or `main.py`
  directly — every `fetch_*`/search call is blocking and run via
  `self.run_worker(fn, thread=True, exclusive=True, group="browser-fetch")`,
  same fetch/apply split as the rest of the app. History is an in-memory
  `list[BrowserHistoryEntry]` (already-fetched `Document`s, not just URLs —
  Back/Forward never re-fetches) — session-lifetime only, no SQLite cache
  category for page content. `Alt+Left/Right` are back/forward (not `[`/`]`)
  when the Browser tab is active; `Tab`/`Shift+Tab` toggle focus between the
  address bar and the page. Gemini's TOFU cert pinning uses a new `Cache`
  category (`"gemini_cert"`, key `f"{host}:{port}"`) via
  `fetchers.GeminiTofuStore`; Gemini status 1x (input) and cross-host 3x
  (redirect) responses raise
  `fetchers.GeminiInputRequired`/`GeminiRedirectConfirm`, each handled by a
  small modal (`GeminiInputModal`/`ConfirmModal`) that resumes navigation
  through `_browser_navigate` on confirm. Never gated by
  `self._require_online()` — that flag tracks Google reachability
  specifically, unrelated to arbitrary web/gopher/gemini/search fetches.
  Search mode: `fetchers.run_search(query, settings)` dispatches to
  `search_google_cse`/`search_duckduckgo`/`search_searxng` per
  `Settings.search_provider` (default `"google"`), with DuckDuckGo (no API
  key needed) as the fallback for every path — see CHANGELOG `[2026-07-14]`
  for the exact fallback chain. This replaced the old `ask.google_search`
  shell-out to `hermes web search`, which stopped working once that
  subcommand disappeared from the installed `hermes` CLI (see the removed
  P3 ROADMAP item, now in CHANGELOG). Every search result becomes a real
  numbered `render.Link` via `fetchers._search_results_to_document`, so
  Search results get the same digit + `Enter` navigation as Gopher/Gemini
  menus and HTTP page links.
- **News tab** (`Ctrl+5`, P1 M3): `ListView#news-list`, the same lightbar
  pattern as the Email pane, showing entries from EVERY subscribed feed
  combined (like the Tasks pane combines all Google tasklists), sorted
  newest-first by `published` (an ISO-8601 UTC string `fetchers.fetch_feed`
  derives from feedparser's normalized `*_parsed` struct_time, so sorting is
  a plain string comparison — raw feed date formats vary too much to sort
  directly). Each row: `MM/DD  [Feed Title] Entry Title` (both truncated,
  same style as `_append_email_items`). Fetching is `fetchers.fetch_feed(url)`
  (new; uses `feedparser`, HTTP done via `requests` like `fetch_http` for
  consistent timeout/User-Agent handling, not feedparser's own URL-fetch
  path) — returns plain dicts (`id`, `title`, `link`, `summary`, `published`,
  `feed_title`, `feed_url`), matching `gauth.py`'s list-of-dict convention.
  `Enter`/`Space` opens `NewsEntryModal` (modeled on `EventModal`/`TaskModal`
  — pushed WITHOUT a callback, since unlike `ThreadModal` there's no
  follow-up action to relay back), which parses the entry body via M1's
  `render.parse_feed_entry(title, summary, base_url=link)` into a `Document`
  shown in a `render.DocumentView`. Item ids use `_mk_id("n", entry["id"])`;
  since a feed entry's real id is very often a URL, `_mk_id`'s sanitizing is
  lossy in that direction, so `self._news_by_cid: dict[str, dict]` (cid ->
  entry dict, rebuilt on every apply) is the lookup, not a `cid[2:]` slice
  like the Email/Tasks/Events lists use. Cached under a new `Cache` category
  `"feed_entry"` (keyed by entry id), fetched/applied via
  `_fetch_news_data`/`_write_news_cache`/`_apply_news_data` exactly like the
  other data sources (see §8, §2's `ListView.clear()` NOTE — `_apply_news_data`
  uses the same generation-counter + awaited-`run_worker` pattern as
  `_apply_mail_data`/`_apply_drive_files`, since it can be applied more than
  once per session: cache load, live refresh, AND every add/remove in
  Settings). Each subscribed feed is fetched in its own try/except inside
  `_fetch_news_data` so one broken feed URL doesn't take down the others —
  but, deliberately, a feed failure does NOT flip `self._online`/the
  Synced-Offline header the way a Gmail/Calendar/Drive failure does: that
  flag is specifically about Google reachability (§1a), and feed URLs are
  unrelated third-party sites. Row/meta `Label`/`Static` widgets built from
  feed content are constructed with `markup=False` — feed titles are
  untrusted external text and Textual's `Content.from_markup()` (what
  `Label`/`Static` route through by default) silently swallows anything
  that looks like `[a tag]`, including a plain `"[Feed Title]"` with no
  malicious intent; `rich.markup.escape()` does NOT reliably fix this
  (confirmed empirically — its tag-detection regex doesn't even touch a
  bracketed phrase containing a space, and `Content.from_markup()` still ate
  it), so `markup=False` is the correct fix, not escaping.
- **Navigation tab** (`Ctrl+6`, P1 M6): driving directions via the Google
  Routes API (`POST https://routes.googleapis.com/directions/v2:
  computeRoutes`). Two `Input`s (`#nav-origin`/`#nav-destination`, free-text
  addresses — the Routes API geocodes these itself, no Places API/exact-
  coordinates needed) + a `Button#nav-go`; `Enter` in either input or the
  button both call `_nav_go`. Fetching is `fetchers.compute_route(origin,
  destination, api_key)`, unlike this app's other fetchers (query-param
  `requests.get`) because the Routes API needs a JSON POST body plus
  mandatory `X-Goog-Api-Key`/`X-Goog-FieldMask` headers; returns a plain
  `fetchers.RouteResult` dataclass (`distance_text`/`duration_text`/
  `steps: list[RouteStep]`), NOT a `render.Document` — there's nothing to
  hyperlink-navigate in a turn-by-turn step list. Units/language/travel
  mode are hardcoded (`IMPERIAL`/`en-US`/`DRIVE`) rather than Settings
  fields — a v1 simplification. Every failure (missing key, HTTP error,
  no route found) raises `fetchers.BrowserFetchError` — reused from the
  Browser tab despite the name, per its own docstring ("caught by main.py
  and shown via notify()") — and, unlike `run_search`'s silent DuckDuckGo
  fallback, there's no fallback provider for driving directions, so every
  failure surfaces as a `notify(severity="error")` instead of degrading
  quietly. `RichLog#nav-log` (`markup=False`, read-only sequential text, no
  per-row action)
  shows the numbered step list; `Static#nav-summary` shows the route
  total. `Button#nav-export` writes the current itinerary to a plain-text
  file via module-level `_export_itinerary` (runs synchronously on the
  main thread — a small local write, no worker needed) at
  `platformdirs.user_documents_dir()/google-tui/route_<origin>_to_
  <destination>_<timestamp>.txt`; `self._nav_last_result: fetchers.
  RouteResult | None` (new `__init__` attribute) backs Export, and is
  `None` until a route has actually been computed this session (Export
  before then just notifies a warning). Fetch/apply split
  (`_nav_fetch_thread`/`_nav_apply_result`/`_nav_apply_error`) follows the
  Browser tab's `_browser_fetch_thread`/`_browser_apply_document` pattern
  exactly: `run_worker(fn, thread=True, exclusive=True, group="nav-fetch")`
  + `call_from_thread` back to the main thread for all widget mutation.
  Configured in a new Settings sub-tab (`settings-tab-navigation`,
  `Input#settings-routes-key` + `Button#settings-save-routes`, backing
  `Settings.routes_api_key`) — see the Settings tab entry below.
- **Settings tab** (`Ctrl+7`): nested `TabbedContent#settings-tabs` (mirrors
  the Calendar tab's `#cal-tabs` Month/Week pattern), five sub-tabs,
  `Alt+Left/Right` cycles between them while the Settings tab is active
  (`_cycle_settings_tab`, modeled on `_cycle_tab`, targets
  `SETTINGS_TAB_ORDER = ["settings-tab-general", "settings-tab-ai",
  "settings-tab-feeds", "settings-tab-search", "settings-tab-navigation"]`
  instead of `TAB_ORDER`).
  Each sub-tab's content is wrapped in its own `VerticalScroll` (independent
  scrolling per section, not one giant outer scroll around the whole
  `TabbedContent`):
  - `TabPane#settings-tab-general`: `Button#settings-reauth-google` (in-app
    Google OAuth re-authorization — see below) + `Switch#settings-encrypt-switch`
    (encrypt-at-rest on/off) + `RadioSet#settings-key-method` (passphrase
    vs. keyfile, hidden via `.hidden` CSS class when encryption is off) + a
    "Clear local cache now" button + a `Static` showing the cache file's
    path/size (see §1a for the encryption model this drives).
  - `TabPane#settings-tab-ai`: `RadioSet#settings-ai-provider` (AI provider
    for the Hermes Ask pane) + `Input#settings-nous-key` /
    `Button#settings-save-nous-key`.
  - `TabPane#settings-tab-feeds`: a News-feed subscription manager
    (`ListView#settings-feed-list` + `Input#settings-feed-url` +
    `Button#settings-add-feed` + `Button#settings-remove-feed`) that edits
    `Settings.feed_urls` directly (append/remove + `save_settings`) and kicks
    a one-off background fetch (`_fetch_and_merge_one_feed`, `thread=True`,
    group `"news-fetch-one"`) for a newly-added feed so the News tab isn't
    empty for it until the next full refresh.
  - `TabPane#settings-tab-search` (Browser tab search provider, added
    2026-07-14): `RadioSet#settings-search-provider`
    (`rb-search-google`/`rb-search-duckduckgo`/`rb-search-searxng`, backing
    `Settings.search_provider`) + two conditionally-`.hidden` groups
    (`#settings-google-group`: `Input#settings-google-cse-key` +
    `Input#settings-google-cse-id`; `#settings-searxng-group`:
    `Input#settings-searxng-url`) that `on_radio_set_changed`'s
    `settings-search-provider` branch shows/hides based on the current
    selection (both can be hidden at once, when DuckDuckGo is selected) +
    `Button#settings-save-search`. See the Browser tab entry above and
    CHANGELOG `[2026-07-14]` for the search backends this configures.
  - `TabPane#settings-tab-navigation` (Navigation tab's Routes API key,
    P1 M6): `Input#settings-routes-key` (password-masked) +
    `Button#settings-save-routes`, backing `Settings.routes_api_key`. A
    `Static` note points at SETUP.md §6 (Cloud Billing must be linked for
    the Routes API — it's part of paid Google Maps Platform, unlike the
    Workspace APIs the rest of this app uses).
- **Contacts tab** (`Ctrl+8`, P1 M5): `Input#contacts-search` + `Button
  #contacts-compose-new` + `Button#contacts-refresh` in a `Horizontal
  #contacts-bar`, above `ListView#contacts-list` (lightbar, same pattern as
  Email/News/Tasks). Backed by a new `gauth.list_contacts(svc)` (People API
  `people.connections().list(resourceName="people/me", personFields=
  "names,emailAddresses,phoneNumbers", pageSize=1000)`, paginated via
  `pageToken`, returns `{resource_name, name, email, phone}` dicts) through
  a new `"people"` service added to `gauth.services()`. Deliberately does
  NOT call `otherContacts.list` (Gmail-derived auto-contacts) — needs a
  separate `contacts.other.readonly` scope not requested by this project.
  Requires the `contacts.readonly` scope (added to `SETUP.md` §7's scope
  list); a token minted before that scope existed gets a 403 from
  `list_contacts`, caught in `_contacts_fetch_thread` and surfaced as an
  actionable `notify(severity="error")` ("re-run the OAuth flow... see
  SETUP.md §7") instead of crashing the tab — this WILL fire against a
  pre-existing token until it's re-minted. Fetched LAZILY: only on the
  Contacts tab's first activation (`self._contacts_fetch_started` guard in
  `on_tabbed_content_tab_activated`), not on every startup/`Ctrl+R` like
  mail/calendar/drive/news — contacts change far less often, and a full
  fetch is one `connections.list` call (not Gmail's N-sequential-calls
  pattern), so eager fetching wasn't worth the extra startup latency. Also
  triggerable manually via `Button#contacts-refresh`. Cached offline in a
  new `Cache` category `"contact"` (keyed by `resource_name`) per §1a/§8's
  pattern. `Input#contacts-search`'s `Input.Changed` re-filters
  `self._contacts_cache` client-side via `rapidfuzz.fuzz.partial_ratio`
  against `"name email"` (module-level `_fuzzy_filter_contacts` helper) —
  never re-queries Google per keystroke. `Enter`/`Space` on a highlighted
  contact opens `ContactModal` (name/email/phone + "Compose Email", which
  dismisses `("compose", email)` and is relayed via
  `_on_contact_modal_result` → `_open_compose_new(email)`, same
  `push_screen(..., callback)` + `call_after_refresh` deferral pattern as
  every other modal-result relay in this app — see the push_screen timing
  NOTE below). `Button#contacts-compose-new` opens a blank compose with no
  prefill. New `rapidfuzz` dependency (`pyproject.toml`) — same helper also
  powers Compose's To-field autocomplete, see the ComposeModal note below.
- **In-app Google re-authorization** (`Button#settings-reauth-google` in
  Settings → General, and `Button#onboarding-reauth-google` in
  `OnboardingWizardModal` when `"google"` is a diagnosed problem — see
  `_diagnose_setup`). Replaces the old process of writing and running a
  one-off OAuth script by hand (SETUP.md §7) for the two cases that used
  to require it: the routine **7-day token expiry** (Testing-status Google
  Cloud apps — SETUP.md §4) and adding a new scope to an existing token
  (e.g. `contacts.readonly` for P1 M5).

  **Deliberately NOT** `InstalledAppFlow.run_local_server()` (spawn a local
  HTTP server, auto-open a system browser, block until the redirect hits
  it) — this app commonly runs on a **headless VM** or an underpowered
  laptop with no X11/Wayland compositor, where there's no browser to open,
  and even opening the URL on a different device (a phone) could never
  reach a server listening on the headless machine's own `localhost`.
  Instead a manual copy-URL/paste-code flow, entirely inside
  `GoogleReauthModal` (`main.py`):
  1. `gauth.build_reauth_flow(scopes=None)` — reads `client_id`/
     `client_secret`/`token_uri` out of the EXISTING `TOKEN_PATH` (so the
     user never re-supplies their downloaded OAuth client JSON — only a
     genuinely first-ever setup, no `TOKEN_PATH` at all, still needs
     SETUP.md's manual walkthrough; this raises a clear error pointing at
     it instead of trying to build a "paste your client_secret.json into
     the TUI" flow, judged not worth the complexity for a once-ever step),
     builds an `InstalledAppFlow`, and sets `flow.redirect_uri =
     "http://localhost"` — a placeholder Google's "Desktop app" OAuth
     client type always accepts without pre-registration (RFC 8252
     loopback exception); nothing ever actually listens there. Local/no
     network — safe on the main thread, called from `GoogleReauthModal.
     __init__` directly (not a worker).
  2. `gauth.reauth_authorization_url(flow)` — `flow.authorization_url
     (access_type="offline", prompt="consent")`. Both kwargs are REQUIRED:
     without them, a RE-consent (the normal case here — the user already
     granted access once before) very often does NOT come back with a
     `refresh_token` at all, since Google only issues one on a truly
     first-ever consent by default, and a re-auth's whole point is
     replacing a dead one. Also local/no-network, safe on the main thread.
     The modal shows this URL as PLAIN text (`Static(..., markup=False)`
     — see the widget-level NOTE below for why not a `[link=...]` markup
     tag), with instructions: open it on ANY device with ANY browser, sign
     in, and expect the resulting redirect to FAIL to load ("can't reach
     this page") since nothing listens at `http://localhost` — that's
     correct, not a bug; the query string it failed to load is what gets
     pasted back.
  3. User pastes either the full failed-redirect URL or just its `code=`
     value into `Input#reauth-code-input`; `gauth.complete_reauth(flow,
     pasted)` sniffs which (`"://" in pasted`) and calls `flow.fetch_token
     (authorization_response=pasted)` or `flow.fetch_token(code=pasted)`
     accordingly, then writes `flow.credentials.to_json()` to `TOKEN_PATH`.
     THIS step hits the network (the actual token exchange with Google) —
     runs on a worker thread (`run_worker(..., thread=True)`), same rule as
     every other gauth call. Must reuse the SAME `flow` object step 1
     created (it carries the OAuth `state` from step 2, needed to validate
     a full pasted URL) — `GoogleReauthModal` holds it as `self.flow`
     across both steps; don't rebuild a fresh flow for step 3.
  Module-level `os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")`
  in `gauth.py`: required because the placeholder redirect is `http://` not
  `https://` — oauthlib refuses to parse a pasted redirect URL against an
  insecure scheme otherwise. Safe here since the loopback leg is never
  actually connected to; only the real exchange with Google's (https) token
  endpoint matters, and that's unaffected by this flag.
  New `google-auth-oauthlib` dependency (`pyproject.toml`) — imported
  lazily inside `build_reauth_flow()`, not at `gauth.py` module level,
  since nothing else in this app needs it.
  **Widget-level NOTE**: the auth-URL `Static` is `markup=False` — Textual's
  own markup parser (`Content.from_markup`, not Rich's) throws a
  `MarkupError` on `://` inside a `[link=...]` tag value (confirmed
  empirically; same family of gotcha as the News tab's bracketed-feed-title
  issue elsewhere in this file). Plain text is the correct fix here too,
  not escaping — most terminals auto-linkify bare URLs in plain output on
  their own, and native mouse-drag selection works on it either way, which
  covers "copy or click" without fighting the markup parser for a cosmetic
  OSC-8 hyperlink tag.
  On success (`GoogleReauthModal` dismisses `"reauthorized"`),
  `GoogleTUI._apply_google_reauth_success` does NOT ask for a restart like
  the encrypt-at-rest settings do (§7) — it rebuilds `self.svc = gauth.
  services()` and kicks `_live_refresh_thread` immediately, since re-auth
  touches neither the cache nor the encryption key. If triggered from
  `OnboardingWizardModal`, `GoogleReauthModal` is pushed ON TOP of it
  (stacked modals); success dismisses BOTH (`isinstance(self.screen,
  OnboardingWizardModal)` check) via the same `push_screen(..., callback)`
  timing the rest of this app's modal-result relays use.
  **Known limitation, not fixed**: still assumes SOME browser, on SOME
  device, can reach the public internet to complete Google's consent
  screen — obviously true in practice, just noting this app's own machine
  never needs to be that device, which is the entire point of this design
  versus `run_local_server()`.

## 2. Key bindings

| Key | Action |
|-----|--------|
| `Ctrl+1..8` | switch **tab** (Mail / Calendar / Drive / Browser / News / Navigation / Settings / Contacts) |
| `Ctrl+Left/Right` | cycle tabs — the reliable fallback for `Ctrl+1..8` (see caveat below) |
| `Alt+1..4` | jump to a Mail **pane** (Email / Events / Tasks / Hermes); switches to the Mail tab first if needed |
| `Alt+Left/Right/Up/Down` | move to the adjacent Mail pane (see `PANE_ADJACENCY` below) on the Mail tab; back/forward through session history on the Browser tab; cycle Settings sub-tabs (General/AI Provider/News Feeds/Search/Navigation) on the Settings tab (`Alt+Up/Down` still only does Mail-pane adjacency — no vertical cycling defined for Settings) |
| `Tab` / `Shift+Tab` | cycle Mail panes (no-op outside the Mail tab) |
| `l` | focus + open `Select#email-label-select`'s dropdown (Email pane only — no-op elsewhere) |
| `r` `a` `f` | reply / reply-all / forward (Email pane) — blocked with a warning notify while offline |
| `Space` | contextual (`action_context_space`): expand/collapse the highlighted row in place (Email — see `_toggle_thread_expand`, NOT `ThreadModal`), toggle complete (Tasks — blocked while offline), event detail (Events); no-op elsewhere |
| `Enter` | open selected item's detail (`ListView.Selected` / `DataTable.CellSelected`) |
| `[` `]` | previous / next month, or week if the Week sub-tab is active (Calendar tab only — no-op on other tabs) |
| `Ctrl+R` | reconnect / refresh all data (same code path as the background sync on startup) |
| `Ctrl+P` | command palette (Textual's own default binding, not declared in `BINDINGS`) |
| `Ctrl+H` | `HelpModal` — full reference, grouped by tab |
| `Ctrl+Q` / `Esc` | quit / close modal |

**Tab number display:** the confirmed design is "always show, dimmed" —
`_tab_label()` appends a `[dim]` superscript digit to each tab title, and
`_pane_title_row()` renders a two-`Label` row (title `width: 1fr`, number
`width: auto`, both styled) for Mail panes. This is NOT hide-until-modifier-
held: Textual 8.2.8's `events.py` has only one keyboard event class (`Key`,
press-only) — there is no key-release event and no exposed Kitty-protocol
modifier tracking, so "numbers appear only while Ctrl/Alt is held" cannot be
implemented in this Textual version. Don't attempt to "fix" this later
without re-checking whether Textual has since added key-release support.

**`Ctrl+1..8` terminal caveat:** most terminals (and browser-based terminals
especially — Chrome/Firefox/Edge reserve `Ctrl+1..8` for switching *browser*
tabs, intercepting the keystroke before it ever reaches the terminal) don't
reliably transmit `Ctrl+<digit>` at all; only terminals with `modifyOtherKeys`
or the Kitty keyboard protocol enabled do (confirmed via
`ANSI_SEQUENCES_KEYS` in this Textual version — the sequences exist and are
mapped, but most terminals never send them). `Ctrl+Left/Right` (`Ctrl+Arrow`)
is universally well-supported and is the reliable path — `Ctrl+1..8` is kept
for terminals that do support it, but don't assume it works everywhere, and
don't "fix" it by touching the bindings — there's nothing to fix in this
app's code; it's what the terminal transmits.

**`PANE_ADJACENCY`** (replaces an older `active ± 1` / `active ± 2`
arithmetic scheme that assumed a symmetric 2x2 grid): Email spans the full
left column; Events/Tasks/Hermes stack in the right column. This is an
explicit `{pane: {direction: pane}}` map, not arithmetic — see `main.py`
near `PANE_ADJACENCY`. If you add a 5th Mail pane, update this map, not a
formula.

NOTE on Textual selection model: `ListView.Highlighted` (capital H) is the
cursor index setter; `ListView.highlighted_child` (read-only) is the selected
ListItem. A `ListView` only has a `highlighted_child` after the cursor has
moved via key/message (e.g. `pilot.press("down")`), not by setting the
attribute directly. This matters for tests — see §6.

NOTE on `TabbedContent`: there are THREE `TabbedContent` widgets in the DOM
(`#main-tabs` outer, `#cal-tabs` nested inside the Calendar tab,
`#settings-tabs` nested inside the Settings tab). A bare
`self.query_one(TabbedContent)` raises `TooManyMatches` — always query by ID
(`self._main_tabs()` helper, or `self.query_one("#cal-tabs", TabbedContent)`,
or `self.query_one("#settings-tabs", TabbedContent)`).
`on_tabbed_content_tab_activated` must check `event.tabbed_content.id` before
acting, since all three post the same `TabbedContent.TabActivated` message —
the existing guard (`if event.tabbed_content.id != "main-tabs": return`)
already correctly no-ops for `#settings-tabs` sub-tab activation the same
way it already did for `#cal-tabs`; no new branch was needed when
`#settings-tabs` was added.

NOTE on `TabPane`/`Tab` titles: pass a **markup string** (e.g.
`"Mail [dim]¹[/dim]"`), not a `rich.text.Text` object. Textual 8.2.8's
`Widget.render_str()` always routes through `Content.from_markup()` unless
the input is already a Textual `Content` instance — a Rich `Text` object hits
`Content.from_markup()` too and blows up (`AttributeError: 'Text' object has
no attribute 'translate'`) instead of being passed through.

NOTE on overriding a Textual-internal `_on_xxx` handler (e.g. `Header.
_on_click`): a bare no-op override in a subclass does **NOT** suppress the
base class's handler. `MessagePump._get_dispatch_methods()` walks the
FULL MRO and, for naming-convention handlers (`_on_click` etc., as opposed
to `@on`-decorated ones), invokes the method from **every** class in the
MRO that defines one — there's no dedup, unlike the decorated-handler path.
Confirmed empirically: `class GtHeader(Header): def _on_click(self): pass`
still let `Header._on_click`'s `toggle_class("-tall")` run right after it,
because both `GtHeader._on_click` and `Header._on_click` get dispatched for
the same click. The actual fix is `event.prevent_default()` — its docstring
says exactly "prevent handlers in any base classes from being called", and
`_get_dispatch_methods` checks `message._no_default_action` at the top of
each MRO-loop iteration and `break`s before reaching the base class's
handler. `main.py`'s `GtHeader` (disables `Header`'s click-to-grow-3-rows
behavior) uses this pattern; if you ever override another built-in
`_on_xxx` handler, use `event.prevent_default()`, not a no-op body, and
verify with a live pilot click (see the `pilot.click` offset gotcha in §6)
rather than trusting it by inspection.

NOTE on `App.query_one`/`App.query` and screens: they resolve against
`self.screen`, i.e. the CURRENTLY ACTIVE (top-of-stack) screen — not the base
app screen. Cost real debugging time once already: a worker callback tried to
`self.query_one("#email-list")` while `LoadingModal` was still on top of the
stack and got `NoMatches("... on Screen(id='_default')")` even though
`#email-list` obviously exists — it exists on the base screen, which wasn't
current. Fix: dismiss any modal FIRST, then query/populate widgets. Any
future modal shown during startup (or any worker that might run while a
modal is up) needs this same ordering.

NOTE on the startup/refresh worker (`_start_after_unlock` → `_load_from_cache`
→ `_live_refresh_thread` → `_apply_live_refresh`): Gmail/Calendar/Drive calls
are blocking synchronous httplib2 calls, not asyncio-native — an `async def`
worker with no real `await` inside doesn't yield control back to the loop,
so it can't paint anything (like `LoadingModal`) before it finishes. That's
why **every** gauth-touching worker runs via `run_worker(fn, thread=True)` (a
real OS thread) and never as a plain `async def`. Textual widgets are NOT
thread-safe (`App.call_from_thread`'s own docstring says so) — every
gauth-touching method is split into a `_fetch_*` half (pure data, safe to
call from the worker thread — also safe to call `Cache` methods from there,
they're lock-guarded) and an `_apply_*` half (widget mutation, must run via
`self.call_from_thread(...)` back on the main thread). If you add a 6th data
source, follow this same fetch/apply split; don't call `gauth.*` and mutate
a widget in the same method if that method might ever run off the main
thread.

**`async def` + blocking I/O is the bug that made this app feel broken.** An
`async def` worker does NOT get its own thread: it runs ON the event loop, so
a blocking `gauth.*` / `requests` call inside one freezes the entire UI —
keystrokes, repaints, everything — until the network answers. Four workers
were written that way and all four have been converted to `thread=True`
(2026-07-14): `refresh_all` → `_refresh_all_thread` (post-send/post-task-toggle
refresh), `_hermes_worker` → `_hermes_thread` (LLM round-trip), `_drive_preview`
→ `_drive_preview_thread`, and the inline `gauth.set_task_status()` call in
`action_toggle_task`. If you add a worker that touches the network, it takes
`thread=True`. There is no exception to this.

Two hot handlers are also **debounced**, because they fire on every keypress
and each one did real work per key: `_drive_on_highlight` (a Drive preview is a
metadata round-trip *plus* a file download — arrowing through a folder fired one
per row) and the Contacts search box (fuzzy-matches the whole address book and
rebuilds every row). Both restart a timer instead of stacking work; Drive
previews are additionally memoised per session in `_drive_preview_cache`.

Populate `ListView`s with `extend(items)`, never `append()` in a loop —
`append` mounts one widget per call (mount + layout + repaint each), so an
80-row inbox paid 80 separate mount cycles. `extend` batches them into one.

`gauth.list_threads()` used to issue one sequential `threads().get()` per
thread — ~160 round-trips at `max_results=80`, measured at **~20 seconds**. It
now issues them through Gmail's HTTP **batch** endpoint (`new_batch_http_request`,
50 sub-requests per call), so the same fetch is 2 round-trips. Order is
preserved and a failed sub-request is skipped rather than sinking the list.
Keep any new per-item Google fetch batched the same way. (Pagination is still
tracked as a P2 ROADMAP item; that's a separate concern from call count.)

NOTE on `push_screen(screen, callback)` timing: the callback fires **before**
the screen is actually popped (confirmed by reading `Screen.dismiss` in this
Textual version: it calls the result callback, THEN `self.app.pop_screen()`)
— NOT after, like you'd assume. A callback that does `self.query_one(...)`
immediately hits the same "wrong screen" `NoMatches` described above.
`_on_startup_unlock_result` and `_on_settings_passphrase_result` both defer
their actual work one step via `self.call_after_refresh(...)` for exactly
this reason. Do the same for any new modal-with-callback flow.

NOTE on `ListView.clear()`: it returns an `AwaitRemove` — removal is NOT
synchronous, and for a bulk removal (dozens of items) it can take LONGER
than a single `call_after_refresh` cycle to actually finish. This only bit
us once mail/drive data started being applied TWICE per session (cache load,
then live refresh, both with the same item IDs): a fire-and-forget
`clear()` + `call_after_refresh(populate)` pattern raised `DuplicateIds`
intermittently, because the second populate's items were inserted before
the first populate's identically-IDed items had actually been removed.
Fixed in `_apply_mail_data_async`/`_apply_drive_files_async` by `await`ing
`clear()` properly inside a `run_worker(..., exclusive=True, group=...)`
coroutine, plus a generation counter (`_mail_apply_gen`/`_drive_apply_gen`)
as a second safety net so a stale, superseded populate is a no-op instead of
racing. If you add another category that gets applied more than once per
session, use this same pattern — don't go back to bare `.clear()` +
`call_after_refresh`.

NOTE: `ModalScreen.Dismissed` does **not exist** in this Textual version
(`hasattr(ModalScreen, "Dismissed")` is `False`) — `on_dismiss(self, event:
ModalScreen.Dismissed)` in `GoogleTUI` type-checks fine only because
`from __future__ import annotations` makes it a string, never evaluated.
This means `on_dismiss` is very likely **dead code that Textual never
calls** in this version (there's no message class for it to dispatch on).
It was NOT touched this round (out of scope), but if `ThreadModal`'s
Reply/Reply All/Forward buttons ever seem to silently do nothing, this is
almost certainly why — the fix would be routing that result through
`push_screen(..., callback)` instead (mind the callback-timing NOTE above).

## 3. File map

```
/home/bradb/google-tui/
├── pyproject.toml              # package metadata + console_scripts entry
├── README.md                  # user-facing keys/layout/setup
├── AGENTS.md                  # THIS file
├── ROADMAP.md
├── CHANGELOG.md
├── SETUP.md                   # Google Cloud Console walkthrough
├── assets/
│   └── screenshot.png         # README hero image (P1 M7) — regenerate via scripts/generate_screenshot.py
├── scripts/
│   └── generate_screenshot.py # regenerates assets/screenshot.png — see §6
├── google_tui/
│   ├── __init__.py            # exports main, gauth, ask
│   ├── __main__.py            # `python -m google_tui` → GoogleTUI().run()
│   ├── gauth.py               # Google auth + Gmail/Cal/Tasks/Drive/Contacts helpers
│   ├── ask.py                 # Hermes Ask (LLM) providers (Browser search moved to fetchers.py)
│   ├── render.py              # protocol-agnostic Document/Block/Link model + DocumentView (P1 M1)
│   ├── fetchers.py            # HTTP/Gopher/Gemini fetch + web search (Browser, P1 M2) + feed fetch (News, P1 M3) + Routes API (Navigation, P1 M6)
│   ├── setup_instructions.py  # shared Google-account/AI-provider onboarding text
│   ├── cache.py               # SQLite local cache, optional per-row Fernet encryption
│   ├── settings.py            # plaintext Settings dataclass (settings.json)
│   └── main.py                # Textual app: tabs, panes, modals, CSS, bindings
└── .venv/                     # Python 3.13 venv (system-site-packages)
```

`cache.py` / `settings.py`: see §1a for the full design (categories, key
methods, canary verification). `CACHE_DB_PATH` = `platformdirs.
user_cache_dir("google-tui")/cache.db`; `KEY_FILE_PATH` and `SETTINGS_PATH`
= `platformdirs.user_config_dir("google-tui")/{cache.key,settings.json}`.

`gauth.py`:
- `services()` — returns cached `{gmail, calendar, tasks, drive}` via
  `Credentials.from_authorized_user_file(~/.hermes/google_token.json)` + builds
  the four `googleapiclient` resources. Refreshes a worker copy so the API
  client isn't shared across worker threads.
- `list_threads(svc, max_results, q)` — Gmail threads, formats `metadata`
  then `full` per thread for snippet/body/headers. Unread via `UNREAD` label.
  Each returned dict also carries `"snippet"` (Gmail message resources
  include a top-level `snippet` regardless of `format`, so this is free —
  no extra API call); backs the Email pane's Space-to-expand inline preview
  (`main.py`'s `_toggle_thread_expand`).
- `list_events(svc, days)` — Calendar `events.list` over next `days` days.
- `events_between(svc, start, end)` — generic date-range `events.list`;
  `month_events(svc, year, month)` and the Calendar tab's week grid both call
  this rather than duplicating the API-call shape.
- `list_tasklists(svc)` / `list_tasks(svc, list_id, show_completed)` — task
  lists and one list's items (caller tags each item with `_list`).
- `list_drive(svc, folder_id)` — Drive `files.list` in `folder_id` (or root).
- `get_file_metadata(svc, file_id)` — `files.get` with an expanded `fields`
  string (`owners`, `createdTime`, `modifiedTime`, `parents`, ...); backs the
  Drive tab's who/what/where/when preview panel.
- `read_drive_text(svc, file_id)` — returns `(name, mime, text)`; Google-native
  files exported via `files.export` (Docs→text/plain, Sheets→text/csv,
  Slides→text/plain), others fetched as bytes then decoded best-effort. Its
  `files().get(...)` call used the wrong keyword (`file_id=`) — the Google API
  discovery-generated method needs `fileId=` (camelCase). This is a real API
  parameter name, not a Python convention; grep for `file_id=` vs `fileId=`
  if a Drive call ever throws "unexpected keyword argument".
- `list_contacts(svc)` (P1 M5) — People API `people.connections().list`
  against `resourceName="people/me"`, paginated via `pageToken`, returns
  `{resource_name, name, email, phone}` dicts (first value of each
  possibly-multi-valued field, `""` if absent). Requires the
  `contacts.readonly` scope — raises on a token that doesn't have it; the
  caller (`main.py`'s `_contacts_fetch_thread`) catches and surfaces this.
- `GOOGLE_SCOPES` — the canonical scope list this app requests, kept in
  sync with SETUP.md §7 by convention (not by code — it's plain text there).
- `build_reauth_flow(scopes=None)` / `reauth_authorization_url(flow)` /
  `complete_reauth(flow, response_or_code)` — the three-step in-app Google
  OAuth re-authorization flow (headless-safe: manual copy-URL/paste-code,
  not `run_local_server()`); see the "In-app Google re-authorization" entry
  in §1 for the full design.
- `reply_to(...)`, `forward(...)`, `send_message(...)` (plain new-message
  send, backs `ComposeModal`'s `mode == "new"`, P1 M5), `set_task_status(...)`
  — MUTATING helpers.

`ask.py`:
- `ask_llm(question, ctx)` — POSTs to Nous inference endpoint
  (`https://inference-api.nousresearch.com/v1/chat/completions`,
  model `tencent/hy3:free`) using `NOUS_API_KEY` from `~/.hermes/config.yaml`.
  `ctx` is a prebuilt "Google snapshot" string.
- `needs_agent(q)` — keyword heuristic; if True, question is delegated to the
  full Hermes agent via `subprocess` shelling `hermes "<question>" --print`.
- `ask_hermes_agent(q)` — runs `hermes` and returns stdout.
- `build_ctx()` — pulls live threads/events/tasks into a compact text block
  for LLM context.
- `google_search(q)` (the old `hermes web search` shell-out) was REMOVED
  2026-07-14 — the Browser tab's Search mode now goes through
  `fetchers.run_search` instead (see the Browser tab entry in §1 and
  CHANGELOG). Don't recreate a search function here; `fetchers.py` owns all
  Browser-tab network I/O, search included.

`main.py`:
- `GoogleTUI(App)` — main screen. Holds `self.svc`, `self.settings`,
  `self._cache` (`Cache | None`, built once the encryption key is resolved),
  `self._online`, `self._tasks_cache`, `self._events_cache` (Mail-tab
  upcoming events), `self._threads_cache` (threadId -> thread dict,
  populated everywhere `_apply_email_list`/`_apply_mail_data_async` are —
  backs the Email pane's Space-to-expand lookup),
  `self._expanded_thread_ids` (`set[str]`, which threads are currently
  shown expanded; resets naturally on every list repopulate, no
  persistence), `self._cal_by_day` / `self._cal_week_cells`
  (Calendar-tab month/week grids), `self._drive_files` (Drive tab) — all
  populated so modal/preview reads don't need a fresh network round trip.
- `LoadingModal` — pushed only on a genuine first run (empty cache), by
  `_start_after_unlock`; dismissed by `_apply_live_refresh`.
- `UnlockModal` — passphrase entry, "unlock" (startup) and "create"
  (Settings tab) modes; see §1a.
- Every gauth-touching operation is split `_fetch_*` (pure data, thread-safe,
  also writes to `self._cache` when called from the live-refresh path) /
  `_apply_*` (widget mutation, main-thread only) — see the NOTEs on the
  startup/refresh worker above. `refresh_all()` (used after a task toggle)
  and `_live_refresh_thread` (startup + `Ctrl+R`) both call `_fetch_mail_data()`
  then `_write_mail_cache(...)` then `_apply_mail_data(...)`; same
  fetch/apply pattern for `_build_cal_month`/`_build_cal_week`/`_drive_load`.
  `_apply_mail_data`/`_apply_drive_files` can each now run TWICE per session
  (cache load, then live refresh) — see the `ListView.clear()` NOTE above for
  why they're `run_worker(..., exclusive=True)`-wrapped async methods with a
  generation counter, not plain synchronous clear+append.
- `_load_from_cache()` — reads every category via `Cache.get_all`/`get` and
  feeds the SAME `_apply_*` methods the live path uses; returns whether
  anything was found (decides whether `LoadingModal` is needed).
- Modals (all subclass `ModalScreen`): `OnboardingWizardModal` (forced
  first-run guidance), `GoogleReauthModal` (in-app OAuth re-auth, see the
  Google re-authorization entry below — can be pushed standalone or
  stacked on top of `OnboardingWizardModal`), `LoadingModal`, `UnlockModal`,
  `ThreadModal`, `ComposeModal`, `EventModal`, `TaskModal`, `DayEventsModal`
  (Calendar day/hour-slot overflow), `NewsEntryModal` (News tab, P1 M3),
  `ContactModal` (Contacts tab detail, P1 M5), `HelpModal` (`Ctrl+H`).
  `CalendarModal`/`DriveModal`/`DriveFileModal`/`SearchModal` from the
  pre-tab-redesign version are GONE — their content is inline in the
  Calendar/Drive/Search `TabPane`s now; do not recreate them as modals.
- `ThreadModal` (P1 M4 rewrite): no longer a single `RichLog#thread-body`.
  Each message is rendered through `render.parse_feed_entry` (HTML-sniffing
  — routes through `render.parse_html` when `gauth.get_thread`'s new
  `"html_body"` key is non-empty, else the plain-paragraph fallback) into a
  `Document`, mounted as its own `DocumentView` (`classes="thread-msg-doc"`,
  height forced to `"auto"` since `DocumentView`'s own `DEFAULT_CSS` sets
  `height: 1fr`, wrong when several are stacked in one `VerticalScroll`),
  preceded by a small From/Date `Static` header — all stacked in
  `#thread-messages`, oldest-first (unchanged order). NOT one merged
  `Document` per thread — see the P1 M4 CHANGELOG entry for why. `_apply_thread`
  is `async` (unlike this app's other `call_from_thread` targets) because it
  must `await container.mount(...)` before setting `.document =` on each
  `DocumentView` — a bare fire-and-forget `.mount()` races `watch_document`'s
  `query_one` calls on children that aren't mounted yet.
- `ComposeModal` (P1 M5 extension): gained `mode == "new"` (blank compose,
  `thread_id=None`, optional `to=` prefill param) alongside the existing
  `reply`/`reply_all`/`forward` modes — sent via a new `gauth.send_message`
  rather than `gauth.reply_to`/`forward`. `#c-to` has a live fuzzy-match
  suggestion `ListView#c-to-suggestions` (hidden when empty) sourced from
  `self.app._contacts_cache`, matching only the fragment after the last
  comma (so a partially-typed multi-recipient list still autocompletes);
  selecting a suggestion appends/replaces that fragment and re-focuses the
  input. No-ops silently (empty suggestion list) if contacts were never
  fetched — never errors.
- `_mk_id(prefix, raw)` — MODULE-LEVEL helper (NOT a method) that sanitizes a
  Google id (or, for News, a feed entry id — often a URL) into a valid
  Textual widget CSS id (`t-…`, `e-…`, `k-…`, `d-…`, `n-…`, `sf-…`).
  MUST stay module-level: do not re-indent it into the class body, and do not
  name any method `_id` (collides with Textual's internal `DOMNode._id`).
- Module-level helpers: `_fmt_date(s)`, `_mk_id`, `_feed_list_item(url)`
  (News-feed subscription row, stashes the raw URL as a `.feed_url`
  attribute since `_mk_id` can't be reversed for a URL-shaped id),
  `_tab_label(text, num)`, `_event_day(e)`, `_is_previewable(mime)`,
  `_email_collapsed_line(th)` (the one-line collapsed row format shared by
  `_append_email_items` and `_toggle_thread_expand`'s collapse path).
- `GtHeader(Header)` — module-level class (not a method), disables
  Textual's built-in click-to-grow-3-rows `Header` behavior via
  `event.prevent_default()` in an overridden `_on_click`; see the MRO-
  dispatch NOTE in §2 for why a naive no-op override doesn't work. Used in
  `compose()` in place of a bare `Header()`.

## 4. Auth & secrets

- Token: `~/.hermes/google_token.json` (OAuth, has `refresh_token` + Gmail/
  Calendar/Drive/Tasks scopes). `google_client_secret.json` was NOT found —
  token is long-lived.
- Why a custom wrapper: the bundled `google-workspace` skill's
  `scripts/google_api.py` does NOT implement the `tasks` service and Drive has
  no `list` subcommand. So this project talks to the Google APIs directly via
  `google-api-python-client` using the already-valid token.
- Nous key: read from `~/.hermes/config.yaml` (`keys.nous_api_key`) by
  `ask.py`. If missing, `ask_llm` raises a clear error.
- The skill's `gws` CLI and `scripts/google_api.py` are NOT used by this app.

## 5. How to run

```bash
cd /home/bradb/google-tui
. .venv/bin/activate            # optional — launcher does this for you
google-tui                     # works from ANY shell (see §7)
```

`google-tui` launcher: `/home/bradb/.local/bin/google-tui` (on PATH),
shell script that `exec`s `/home/bradb/google-tui/.venv/bin/python -m google_tui`.
If the project folder moves, update the `VENV=` path in that launcher.

**Keeping venv deps in sync across machines**: `pip install -e .` (README
Setup) only installs what `pyproject.toml` lists AT THAT MOMENT — a later
`git pull` that adds a new dependency (e.g. `feedparser` for the News tab,
P1 M3) does NOT retroactively install it, and the next launch crashes with
`ModuleNotFoundError` instead of failing informatively. Fixed with a tracked
`hooks/post-merge` hook (activate once per clone: `git config
core.hooksPath hooks`, also in the README) that re-runs `pip install -e .`
automatically whenever a pull touches `pyproject.toml`. If a machine's
`google-tui` fails on import with a missing module right after a pull, check
whether `core.hooksPath` is set there before assuming something else broke.

## 6. Testing without a TTY

Textual needs a real terminal, so headless tests use Textual's `run_test`
driver with a `pilot`. Pattern that works:

```python
async with app.run_test(size=(140, 44)) as pilot:
    await asyncio.sleep(2)                          # let workers populate
    app.action_goto_pane_email()
    await pilot.pause()
    await pilot.press("down")                       # move cursor → highlighted_child set
    await pilot.pause()
    await pilot.press("r")                          # reply → ComposeModal opens
    await pilot.pause()
    assert isinstance(app.screen, ModalScreen)
```

Use `app.save_screenshot(path)` at any point inside `run_test` to export an
SVG snapshot of the current render — the closest substitute for eyeballing a
live TTY app when you can't attach one. `pip install cairosvg` (into the
project `.venv`) to convert those to PNG for visual review.

**Regenerating `assets/screenshot.png`** (the README hero image, P1 M7):
run `scripts/generate_screenshot.py` — it's a self-contained version of this
exact pattern (fabricated dataset, `gauth.get_credentials`/`services`/every
`list_*`/`get_thread` call mocked so it needs no real token and makes zero
live API calls, `run_test` pilot, `save_screenshot` → `cairosvg`) that writes
straight to `assets/screenshot.png`:
```bash
.venv/bin/pip install cairosvg   # one-time, not a project dependency (see script docstring)
.venv/bin/python scripts/generate_screenshot.py
```
Re-run this whenever the Mail tab's visual design changes enough to make the
current screenshot look stale (new tab, changed pane layout, color scheme,
etc.) — not on every commit, and always eyeball the result before
committing (a Textual layout regression can still "succeed" while looking
wrong). The dataset lives in the script itself (module-level `FAKE_*`
constants) — edit those directly if you want different sample content;
don't reach for real account data, the whole point is that this asset is
safe to regenerate and share without ever touching a live token.

Gotchas that cost time before:
- Mock `ask.ask_llm`, `ask.ask_hermes_agent`,
  `fetchers.search_google_cse`/`search_duckduckgo`/`search_searxng` (or
  `fetchers.run_search` directly), and `gauth.reply_to`/`forward`/
  `send_message`/`set_task_status` in tests to avoid network + real email
  sends. Also mock `gauth.list_contacts` if the test's token doesn't have
  the `contacts.readonly` scope yet — otherwise the Contacts tab's fetch
  raises and the test only exercises the error-notify path. ALWAYS mock
  `gauth.build_reauth_flow`/`reauth_authorization_url`/`complete_reauth`
  too before clicking `#settings-reauth-google` or `#onboarding-reauth-google`
  — the real `complete_reauth` blocks on an actual network round trip to
  Google, which will hang any test/pilot run waiting on a fabricated code
  that was never really issued.
- Do NOT assert on `ListView.highlighted`/`highlighted_child` after setting the
  attribute directly (read-only setters differ between versions). Drive selection
  through key presses instead.
- There are three `TabbedContent`s in the DOM (`#main-tabs`, `#cal-tabs`,
  `#settings-tabs`) — use `app.query_one("#main-tabs")`, never a bare type
  query (see §2).
- `app.query_one`/`app.query` resolve against `self.screen` (see the NOTE in
  §2) — a pushed `ModalScreen` (e.g. `ThreadModal`) means widgets like
  `#thread-messages` must be reached via `app.screen.query_one(...)`, not
  `app.query_one(...)`, while that modal is on top of the stack.
- `pilot.click(SomeWidget)` with no `offset` clicks the widget's top-left
  corner, not its visual center — cost real debugging time once already on
  `GtHeader`/`Header`: the default offset landed on `HeaderIcon` (docked
  left, width 8), whose own `on_click` calls `event.stop()` and opens the
  command palette, so the click never even reached `Header`'s own handler —
  a test that "passed" for the wrong reason. Pass an explicit `offset=`
  (e.g. `(50, 0)`) to land on the part of the widget actually being tested.
- The Hermes Ask answer takes ~1s to stream into the RichLog; sleep 2s before
  asserting log line count.
- Run each `GoogleTUI()` test scenario in its OWN process (`python
  scenario_x.py`, not multiple `async with app.run_test()` blocks chained in
  one `asyncio.run(...)`). Chaining full app instances in one process left a
  background `thread=True` worker from a prior instance still in flight when
  the next instance mounted, and it reproducibly caused a `DuplicateIds`
  crash unrelated to the actual scenario being tested. Wipe
  `platformdirs.user_cache_dir("google-tui")` / `user_config_dir("google-tui")`
  between scenarios that need a clean cache (`shutil.rmtree(..., ignore_errors=True)`).
- To prime a cache for a "warm start" test, actually run a cold-start
  scenario first (real live data) rather than hand-crafting cache rows —
  the payload shapes (Gmail thread dict, Calendar event dict, etc.) are
  exactly what `gauth.*` returns, easy to get subtly wrong by hand.

## 7. Known caveats / open items

- **Contacts tab needs a token re-mint.** `gauth.list_contacts` (P1 M5)
  requires the `contacts.readonly` scope, added to `SETUP.md` §7's scope
  list when this shipped. Brad's live `~/.hermes/google_token.json` was
  minted before that — the Contacts tab will show the graceful
  "re-run the OAuth flow" error notify until it's regenerated with the new
  scope (a manual step, see SETUP.md §7 — a session can't do this itself,
  it needs a real browser consent flow).
- NO send confirmation: `ComposeModal` send fires `gauth.reply_to`/`forward`
  immediately. Not tested against the live API (would actually send mail).
  Recommended next step: add a confirmation step before send (see ROADMAP).
- Threads: only first 80 shown (metadata+full per thread). No threading UI
  beyond one level; no pagination beyond that.
- Calendar month grid (`#cal-grid`): `on_data_table_cell_selected` reads the
  day number off `event.value` (the first line of the multi-line cell text),
  not `event.coordinate` + a separate `get_cell_at` lookup — simpler and
  avoids the `update_cell`-with-integer-indices `CellDoesNotExist` trap the
  old modal-era code had to work around.
- Week view (`#cal-week-grid`) is **hour granularity**, not 30/15-minute — an
  event's summary is written into every hour row it spans, so sub-hour timing
  isn't visually precise. Documented follow-up in ROADMAP, not fixed here.
- Drive "up" always reloads root, not the true parent folder — a
  simplification carried over unchanged from the pre-tab-redesign DriveModal
  (fixing real parent-stack tracking is a separate, unrequested task).
- Drive preview is text-only for `_is_previewable()` mime types; images and
  other binaries show metadata + "no text preview" (no download-to-`less` or
  in-terminal image rendering — that would need `textual-image`, not
  currently a dependency).
- **FIXED 2026-07-14** (was open since M2): the Browser tab's Search mode
  used to shell out to `hermes web search`, a subcommand that no longer
  exists in the installed `hermes` CLI — it degraded to an empty-links
  Document instead of crashing, but was effectively dead. Replaced with
  real search backends (`fetchers.run_search` → Google Custom Search /
  DuckDuckGo / SearXNG, configurable in Settings' new Search sub-tab,
  DuckDuckGo as the no-config fallback). See the Browser tab entry in §1
  and CHANGELOG `[2026-07-14]` for the full design.
- LLM model is hardcoded `tencent/hy3:free`; change in `ask.py` only.
- Tab numbers are always-shown-dimmed, not hide-until-modifier-held — see the
  NOTE in §2 for why (no key-release event in Textual 8.2.8).
- Offline mode is READ-ONLY browsing of cached data, not a sync engine: no
  queued mutations, no automatic retry beyond `Ctrl+R`/next launch. See §1a.
- Changing the encrypt-at-rest switch or key method takes effect on the
  NEXT launch, not live — the running session keeps using whatever `Cache`
  object it already built. The cache is cleared immediately so stale rows
  under the old scheme can't linger, but a "restart to apply" notify is the
  only feedback; there's no in-session cache-object hot-swap. A genuine
  live-swap (rebuild `self._cache` with the new key without restarting)
  is a reasonable follow-up if the restart step proves annoying in practice.
- `on_dismiss(self, event: ModalScreen.Dismissed)` is almost certainly dead
  code in this Textual version — see the NOTE in §2. Not fixed this round
  (out of scope), but worth knowing before assuming Reply/Forward-from-
  ThreadModal works.
- News tab (P1 M3): removing a feed in Settings drops it from `Settings.
  feed_urls` and re-renders `#news-list` with that feed's entries filtered
  out, but does NOT delete its old rows from the `feed_entry` cache
  category — they just stop being fetched/shown until a full `Cache.
  clear_all()`. Harmless (a few stale SQLite rows), consistent with how
  Drive/mail data isn't actively pruned either, but worth knowing if a
  cache-size audit ever looks suspiciously large after a lot of
  add/remove churn. Also: a single unreachable feed URL notifies an error
  and is skipped (per-feed try/except in `_fetch_news_data`) but
  deliberately does NOT flip `self._online`/the Synced-Offline header —
  that flag is Google-reachability-specific (§1a); don't "fix" this by
  folding feed failures into the same `ok` flag as the Gmail/Calendar/Drive
  fetches in `_live_refresh_thread`.

## 8. Common tasks a future session might do

- Add a new Mail pane: add id to `PANE_IDS`/`PANE_TITLES`, a neighbor entry
  in `PANE_ADJACENCY`, a widget in `compose()`, an `action_goto_pane_<x>`,
  and bind `alt+N` in `BINDINGS`.
- Add a new top-level tab: add a `TabPane` in `compose()` under
  `TabbedContent#main-tabs`, an `action_goto_tab_<x>`, bind `ctrl+N`, and add
  a branch to `on_tabbed_content_tab_activated` and `_context_help_text`.
- Change the LLM: edit `MODEL` in `ask.py`.
- Add a Google action (e.g. create event): add a helper in `gauth.py` and a
  modal/handler in `main.py`.
- Fix a modal crash: check the traceback's `main.py` line; modals subclass
  `ModalScreen` and call `self.dismiss(...)`.
- Cache a new data source: add a category name (see §1a), a `_fetch_*` /
  `_apply_*` pair, write-through in `_live_refresh_thread` (`self._cache.put`
  or `.put_many`), and a read in `_load_from_cache`. If the category is
  "content-sized" (could be large, like Drive file text) rather than a
  small summary, cache it lazily on first successful view, not eagerly for
  a whole listing — see the `drive_file_text` pattern in `_drive_preview`.
- Bump something: update CHANGELOG.md and ROADMAP.md when done.
