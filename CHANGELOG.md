# CHANGELOG.md — google-tui

Format: keep newest at top. One entry per meaningful change. Reference files
touched and any breaking notes.

## [2026-07-16] — Offline mutation queue: Mark Unread + Trash/Archive/Labels, plus per-item cancel

Widens the ROADMAP P3 offline mutation queue further (see the same-day
"Reply/Forward/toggle-task" entry below for the original design): Mark
Unread (Email pane) and ThreadModal's Trash/Archive/Labels now queue for
replay on reconnect instead of being blocked by `_require_online()`, and
`PendingMutationsModal` gained per-item cancel. New Event and TaskModal's
Add/Delete subtask + Delete task remain out of scope — see the updated
ROADMAP entry for why (CREATE actions need a temp local id this queue
doesn't have yet).

All four new types are PATCH-style (no visible-list insert needed), so they
copy `action_toggle_task`'s existing shape: `if not self._online: enqueue +
optimistic local mutation + notify + return`. `action_mark_unread`
(`main.py`) enqueues `{"type": "mark_unread", "thread_id"}` and flips
`self._threads_cache[tid]["unread"] = True` before re-rendering the Email
list straight from cache (`_apply_email_list`, the same no-network path the
debounced search box already used) — no full refetch needed to make the •
bullet reappear. `ThreadModal.action_trash`/`action_archive`/
`_on_labels_result` gained a new `_queue_mutation` helper (the offline
counterpart to the existing `_run_mutation`): trash/archive additionally
pop the thread out of `self.app._threads_cache` and re-render the Email
list, since the user just asked for it to leave view — same "it's gone
now" UX the online path's post-write refresh already gives, just without
the round trip. Labels have nothing inline to show (the email list doesn't
render label chips), so `modify_labels` just queues silently and — like
`_on_labels_result`'s existing `close=False` — leaves the modal open.
Dismissing with a new `"queued"` sentinel (distinct from `"refresh"`) tells
`_on_thread_modal_result` not to attempt a network refetch that would just
fail offline. `action_labels` also dropped its `_require_online()` gate
entirely (mirroring how compose already allows composing offline) — only
the actual write in `_on_labels_result` is gated/queued now.
`gauth.mark_unread`/`trash_thread`/`archive_thread`/`modify_labels` got
matching `_replay_one_mutation` dispatch arms and `_PENDING_MUTATION_LABELS`
entries.

`PendingMutationsModal` (Settings → "View queued actions") is no longer
read-only: it now takes `GoogleTUI._pending_mutations` directly (the SAME
dict, not a copy passed as `list(...)`), renders it as a `ListView` instead
of one opaque `Static` block, and Delete cancels the highlighted row via a
new `GoogleTUI._cancel_mutation(key)` (pops the queue + deletes the cache
row). Cancelling does NOT undo an already-applied optimistic local update
(a cancelled Mark-Unread leaves the • bullet showing, a cancelled Trash
leaves the thread out of the list) — reverting would need remembering
pre-mutation state, which isn't tracked; see both classes' docstrings.
Hit one sharp edge building this: naming the modal's list-repaint method
`_render` silently shadowed `Widget._render()` — Textual's own internal
method for computing a widget's paint content — and broke the compositor
with an opaque `AttributeError: 'coroutine' object has no attribute
'render_strips'` with no indication `_render` was the reserved name.
Renamed to `_render_queue`; worth remembering `_render`/`_arrange`/other
single-underscore `Widget`/`DOMNode` method names are Textual-internal and
not safe to reuse on any widget subclass.

Verified via an isolated `run_test` pilot (same isolation pattern as the
original queue entry below — `platformdirs` redirected to a temp dir before
import, `gauth.mark_unread`/`trash_thread`/`archive_thread`/`modify_labels`
mocked) driving: an offline Mark Unread (optimistic flag + queued), a
`ThreadModal` offline Trash and Archive (each queues + drops the thread
from `_threads_cache`), an offline Labels apply (queues, modal stays open,
thread stays in cache), a `PendingMutationsModal` cancel (queue depth drops
by exactly one), and replay-on-reconnect (queue drains to empty, each
mocked `gauth` call fires with the right thread id/args).

## [2026-07-16] — Live encryption-setting hot-swap: no restart needed

Closes the ROADMAP P3 item "Live encryption-setting hot-swap." Toggling
encrypt-at-rest or the key method in Settings now applies immediately
instead of clearing the cache and telling the user to restart.

Added `Cache.rekey(key: bytes | None)` (`cache.py`) — swaps `self._fernet`
in place on the existing `Cache` object rather than requiring a whole new
one. Because the object identity doesn't change, every existing reference
to it (`self._browser_tofu`, everything reading `self._cache` on the App)
keeps working with no further wiring. All four Settings code paths that
used to clear the cache and notify "restart google-tui to apply" —
encrypt-at-rest on/off (`on_switch_changed`), key method switched to
keyfile (`on_radio_set_changed`), and key method switched to/enabled with
passphrase (`_apply_settings_passphrase_result`, shared by both the
switch-on and key-method-change flows) — now call `self._cache.rekey(...)`
right after `clear_all()` and the notify text no longer mentions a
restart.

## [2026-07-16] — Offline mutation queue: Reply/Forward/toggle-task now queue instead of blocking

Partly closes the ROADMAP P3 item "Offline mutation queue" — Reply/Reply
All/Forward/New-compose send and task-toggle (including subtasks) now
queue for automatic replay on reconnect instead of being flatly disabled
while offline; Mark Unread, New Event, ThreadModal's Trash/Archive/Labels,
and TaskModal's Add/Delete subtask + Delete task remain blocked (see the
updated ROADMAP entry for why those stayed out of scope — CREATEs need a
temporary local id, DELETEs might target a queued CREATE, neither of which
this queue handles).

New `Cache` category `"pending_mutation"` (key = `uuid4`, value =
`{type, ...payload, created_at}`) persists the queue across an app
restart while still offline, loaded back in `_load_from_cache`. Added
`Cache.delete(category, key)` (`cache.py`) to remove a replayed/dropped
item — the first `Cache` category that needed row-level deletion rather
than bulk `clear_all()`.

`GoogleTUI._enqueue_mutation` writes to both `self._pending_mutations`
(in-memory) and the cache; `action_reply`/`action_reply_all`/
`action_forward`'s `_require_online()` gate is gone (composing offline is
now allowed), and `ComposeModal._send_now` branches on `self.app._online`
to enqueue instead of calling `gauth.reply_to`/`forward`/`send_message` —
which also meant giving `_send_now` its first try/except: it had none
before, so an online send failure would have propagated uncaught out of a
`set_interval` timer callback straight to the App's unhandled-exception
handler. `ComposeModal.on_mount` skips the blocking `threads().get()`
metadata fetch when offline and falls back to the cached thread summary
for Subject/From instead — `reply_all` degrades to replying just to the
sender in this case, since `list_threads` never fetches To/Cc headers, and
a warning notify says so. `action_toggle_task` and TaskModal's
`_toggle_highlighted_subtask` apply an OPTIMISTIC local update (flip the
cached task dict's `status` directly — `_selected_task()`/
`TaskModal.subtasks` hand back references into `self._tasks_cache`, not
copies, so this updates both the modal and the Tasks pane) so the checkbox
changes immediately instead of waiting for a reconnect that might be a
while off; `_on_task_modal_result` now checks `self._online` before
deciding between a real refetch (online) or a local re-render (offline —
a refetch would just fail and notify a confusing "Refresh error" right
after the optimistic toggle succeeded).

Replay: `_apply_live_refresh` kicks `_replay_pending_mutations_thread`
(worker thread) whenever a refresh succeeds and the queue is non-empty.
Processes oldest-first by `created_at` (dict insertion order isn't
reliable queue order once reloaded from `Cache.get_all`, which doesn't
preserve write order across categories/restarts). New helper
`_is_not_found_error` recognizes an `HttpError` with `.resp.status == 404`
— every queued mutation type's replay does a `.get()` before mutating
(`reply_to`/`forward` fetch the thread, `set_task_status` fetches the
task), so a target deleted while offline surfaces identically regardless
of type. A 404 drops the item with a warning notify ("its target no
longer exists"); any other exception leaves it queued for the next
reconnect attempt, but the loop still tries the REST of the queue instead
of stopping at the first failure, since one thread/task going missing says
nothing about the others. A successful replay batch triggers
`_refresh_all_thread` once, so real server state (the sent reply, the
completed task) replaces whatever optimistic/cached state was showing.

Small UI surface (per the chosen design — indicator + list, not silent):
`self._status_base` + new `_render_sub_title()` combine the existing
Connecting/Synced/Offline text with a "· N queued" suffix; every direct
`self.sub_title = ...` assignment (3 of them) was replaced with this pair.
Settings → General gained an "Offline queue" section (`Button
#settings-view-queue` + a live-updating `Static#settings-queue-info`)
opening `PendingMutationsModal` — a read-only, sorted-by-time list (no
per-item cancel; see its class docstring and the ROADMAP entry for why).

Verified via an isolated `run_test` pilot (isolated `platformdirs`
config/cache dirs, `gauth.set_task_status`/`reply_to` mocked, one queued
item made to 404 via a fake `HttpError`) confirming: an offline task
toggle applies optimistically and queues a mutation; an offline reply
opens with cached-thread prefill and queues instead of calling
`gauth.reply_to`; and replay-on-reconnect sends the toggle, drops the
404'd reply, and empties both the in-memory queue and its `notify`/
`sub_title` reflection. Also confirmed `Cache.delete` removes exactly the
targeted row.

## [2026-07-16] — Drive tab: "Load more" past the one-folder-page cap

Closes the ROADMAP P3 pagination item — Email and Events landed earlier
today (see the two entries below). `gauth.list_drive` now accepts
`page_token` and returns `(files, next_page_token)` instead of a bare list
(its 1 real caller, `main.py`'s `_fetch_drive_files`, plus
`scripts/generate_screenshot.py`'s mock, updated to match) — needed adding
`nextPageToken` to the `fields` mask explicitly, since Drive's partial-
response filtering drops it otherwise. `_fetch_drive_files` stashes the
token on `self._drive_next_page_token` (reset fresh on every folder
navigation, since a page token is folder-specific); `_apply_drive_files_async`
and `_apply_drive_search_async` append a "↓ Load more files…" row (via the
same generalized `_append_load_more_row` Email/Events use) whenever it's set
and no search filter is active. Selecting it (`action_load_more_drive` →
`_load_more_drive_thread`) fetches the next page for the CURRENT folder and
merges it into `self._drive_files` by file id — same dict-merge-not-concat
DuplicateIds fix as Email's — writing the deduplicated result back to the
`drive_listing` cache row too, not just the rendered list. Verified via an
isolated `run_test` pilot (two fake pages, `gauth.list_drive` mocked)
confirming the row appears after page 1, hides while filtered, and
disappears after loading page 2 (no further `next_page_token`).

## [2026-07-16] — Events pane: "Load more" past the 3-week window

Part of the ROADMAP P3 pagination item (Drive still open — see ROADMAP).
Unlike Gmail's cursor-based pagination, Calendar's `events.list` is a plain
date-range query, so "load more" here means refetching the WHOLE window at
a wider size rather than merging an incremental page — no `next_page_token`
equivalent to track, only `self._events_window_days` (starts at 21,
`action_load_more_events` grows it by `_EVENTS_WINDOW_STEP_DAYS` each call,
with no natural end — there's always a further-out week to ask for). An
ordinary refresh (Ctrl+R, startup) now passes `self._events_window_days`
to `gauth.list_events` instead of a literal `21`, so a widened window
persists across refreshes instead of snapping back. The "Load more" row
itself reuses the same `_append_load_more_row` helper the Email pane's row
already used — generalized to take an id/label pair instead of being
Email-specific — appended to the Events pane whenever there's no active
search filter (Events' window, unlike Email's page cursor, is *always*
further-extendable, so the row has no "no more to load" hidden state the
way Email's does). Verified via an isolated `run_test` pilot (`gauth.
list_events` mocked to return a bigger set at the wider window) confirming
the row appears after the initial window, widening the window refetches
and re-renders correctly, and the row hides while a search filter is active.

## [2026-07-16] — Email pane: "Load more" past the 80-thread cap

Part of the ROADMAP P3 pagination item (Events/Drive still open — see
ROADMAP). `gauth.list_threads` now accepts `page_token` and returns
`(threads, next_page_token)` instead of a bare list (its 3 callers in
`main.py`, plus `scripts/generate_screenshot.py`'s mock, updated to match).
`_fetch_mail_data`/`_refresh_email_for_label` stash the returned
`next_page_token` on `self._email_next_page_token`; whenever it's set (and
no search filter is active — loading page 2 while filtered would be
ambiguous about what the filter even applies to), `_apply_email_list_async`
and `_apply_mail_data_async` append a clickable "↓ Load more messages…" row
(sentinel id `LOAD_MORE_EMAIL_ID`, handled in `on_list_view_selected` like
every other row-id-prefix branch there) after the normal thread rows.
Selecting it runs `action_load_more_email` → `_load_more_email_thread`,
which fetches the next page and merges it into `self._threads_cache` by
`threadId` (not a list concat) so a thread that lands on both pages —
possible if it was already cached under a different label — can't produce
two `ListItem`s with the same `_mk_id` (a `DuplicateIds` crash, not just a
visual dupe); a dict update preserves each key's original position, so
this can't reorder the list either. Reuses the existing
`_apply_email_list`/clear+repopulate pipeline wholesale rather than adding
a new incremental-append code path, so search filtering, the generation
counter, and the `ListView.clear()`-is-async handling all keep working
unchanged. Verified via an isolated `run_test` pilot (two fake pages,
`gauth.list_threads` mocked) confirming the row appears after page 1, hides
while filtered, and disappears after loading page 2 (which has no further
`next_page_token`).

## [2026-07-16] — Guided re-auth message when the Google refresh_token is dead

Closes the ROADMAP P3 item "Token refresh handling." A missing or
revoked/expired `refresh_token` in `~/.hermes/google_token.json` makes
`Credentials.refresh()` raise `google.auth.exceptions.RefreshError` —
previously `_live_refresh_thread` (`main.py`) just caught this as a generic
`Exception` in each of its five independent try/except blocks (mail,
labels, cal_month, cal_week, drive), so the user saw up to five near-
identical raw exception strings ("Refresh error: The credentials do not
contain the necessary fields...") with no pointer to what to actually do.
`_live_refresh_thread` now checks `_google_auth_broken_detail()` first —
a fresh `gauth.get_credentials()` call that specifically catches
`RefreshError` (as opposed to `_google_creds_ok()`'s existing catch-all,
which can't distinguish a dead token from a plain network hiccup) — and if
the token really is dead, shows ONE actionable notify pointing at Settings
→ General → "Re-authorize Google account" (the existing in-app OAuth flow,
`GoogleReauthModal`) and marks the app Offline, skipping all five doomed
fetch attempts instead of running and failing every one of them. A non-auth
exception (network error, quota, etc.) falls through unchanged to the
existing per-section handling. Covers all three paths that call
`_live_refresh_thread` (startup, manual Ctrl+R, and the post-mutation
refresh after a task toggle) since they all go through this one function.
Verified via an isolated `run_test` pilot (`gauth.get_credentials` patched
to raise `RefreshError`, `GoogleTUI.notify` patched to a call-recording
mock) confirming exactly one guided notify fires and `self._online` goes
`False`; a second check confirms a non-`RefreshError` exception is not
misreported as an auth problem.

## [2026-07-16] — F1..F8 primary tab switching, Ctrl+1..8 kept as secondary alias

Closes the ROADMAP P3 item "Ctrl+# tab bindings don't work over SSH." Most
terminals (browser-based ones especially — Chrome/Firefox/Edge reserve
`Ctrl+1..8` for their own tab switching) never transmit `Ctrl+<digit>` to the
app at all. `bindings.py`'s `GLOBAL_ACTIONS` now gives each `goto_tab_*`
action two keys via `Binding`'s comma-separated `key` field (e.g.
`"f1,ctrl+1"`) — bare `F1..F8` is the SSH-safe primary binding, `Ctrl+1..8`
stays as a secondary alias for terminals that do support it, and
`Ctrl+Left/Right` remains the last-resort fallback. Surfaced a real conflict
along the way: `action_toggle_mouse` already used `f2`, and Calendar is tab
position 2, so a straight F1..F8 mapping would have collided with it —
`toggle_mouse` moved to `f12` (confirmed unused) instead of breaking the
contiguous F1..F8-for-tabs scheme. Updated help text (`HELP_GLOBAL_TEXT`,
`HELP_TEXT` in `bindings.py`), README.md, and AGENTS.md's key-bindings table
and terminal-caveat section to match. Verified via an isolated `run_test`
pilot (same isolated-`platformdirs` pattern as the Ctrl+R debounce fix,
`_load_from_cache` patched to skip `LoadingModal` so key bindings reach the
base screen) confirming F1..F8 each switch to the right tab, Ctrl+1/2/8
still work, F2 no longer triggers `action_toggle_mouse`, and F12 does.

## [2026-07-16] — Debounce Ctrl+R to stop rapid refreshes tripping Google quota

Closes the ROADMAP P3 item "Connection pool / rate limiting." Rapid `Ctrl+R`
presses each spawned a real `run_worker(self._live_refresh_thread,
thread=True, exclusive=True)` — `exclusive=True` only cancels the *previous*
worker's result from being applied, it doesn't stop the underlying blocking
Gmail/Calendar/Drive/Tasks calls already running on their own OS thread
(`Worker.cancel()`'s own docstring: cancelled work "may still be running").
So spamming the key still fired one full round of live API calls per press.
`action_refresh` (`main.py`) now tracks `self._last_manual_refresh` (a
`time.monotonic()` timestamp) and no-ops with a `notify(...,
severity="warning")` telling the user how many seconds to wait if called
again within `REFRESH_COOLDOWN_SECONDS` (5s) of the last one; genuinely new
requests past the cooldown still refresh immediately. Only the manual
Ctrl+R path is throttled — the startup refresh and the post-mutation
refreshes (task toggle, compose send) are unaffected. Verified via an
isolated `run_test` pilot (patched `_live_refresh_thread` to a call counter,
isolated `platformdirs` config/cache dirs per the pattern from
`scripts/generate_screenshot.py`) confirming a second immediate
`action_refresh()` is throttled and one issued after the cooldown fires.

## [2026-07-15] — Updater: lock concurrent-instance updates, log crashed relaunches

Two google-tui instances launched moments apart could both detect the same
pending update and run `git merge --ff-only` (plus its synchronous
`pip install -e .` post-merge hook) against the shared checkout/venv at the
same time, corrupting either relaunch — matches a report of an update
printing the wrong version/sha pairing and then dropping straight back to
the shell instead of launching. `check_for_update` (`updater.py`) now takes
a non-blocking `flock` around the merge (`_LOCK_FILE`, in the platformdirs
cache dir); a sibling instance that already holds it just skips the check
for that run rather than waiting or double-merging. The lock is deliberately
left open on success — Python fds are close-on-exec by default (PEP 446), so
`restart()`'s `os.execv` releases it automatically at exactly the safe
moment. Separately, `main()` (`main.py`) now logs the full traceback via
`_logger.exception` for both a failed update check and a crash during
`GoogleTUI().run()` itself — previously only the update-check path printed
`str(e)` with no log entry, and a crash from `.run()` before Textual's
`_handle_exception` hook is installed propagated completely unlogged.

## [2026-07-15] — Email pane: hide the labels filter until `l` is pressed

`#email-label-select` (the All Mail/Inbox `Select`) was always visible above
the email list. It's now `hidden`-by-default like `#email-bar`'s search Input,
following the same summon-on-demand pattern as `_show_pane_search`/
`_hide_pane_search` (`main.py`). `l` (`action_focus_label_select`,
`main.py:2126`) reveals it, focuses it, and opens the dropdown; Esc
(`_hide_label_select`, `main.py`, wired in `on_key`) collapses the dropdown,
re-hides the `Select`, and refocuses `#email-list`. Renamed the pane:email
shortcut hint from "l Folder" to "l Labels" (`bindings.py`: `GLOBAL_ACTIONS`,
`CONTEXT_HELP`, `_CLICK_ACTIONS`, and the full help-modal text) to match what
the field actually contains.

## [2026-07-15] — Calendar tab: `/` jump-to-next-match

Closes the last remaining ROADMAP P2 item, "Calendar tab: `/` jump-to-next-match"
(the `## P2 — UX polish` section is now empty and removed). This was flagged as
a genuine design decision, not copy-paste plumbing: every other pane's `/`
(Email/Tasks/Events/Drive/News/Contacts) live-*filters* a `ListView`, but the
Calendar tab's Month/Week views are a fetched date **grid** (`DataTable`), not a
list — there are no rows to hide. So `/` here is a **find-next**, not a filter.

### Design choice — Enter-triggered find-next, not live-as-you-type
Modeled on `ThreadModal`'s `/` find-in-thread (same-day work), not the
Contacts-style live filter. Rationale: a filter answers "show me only the
matches"; a date grid can't shrink to matches (the calendar layout is the
point). What the user actually wants is "move me to the next day/hour that has a
match" — a cursor jump, which is inherently a discrete, Enter-triggered action
(you jump, look, press Enter again to jump further), not a continuous
per-keystroke rebuild. Live-as-you-type would also fight the grid: every
keystroke would yank the cursor around mid-word. So the interaction mirrors a
text editor's find-next exactly.

### Added
- **`Input#cal-search`** in a `#cal-search-bar` row between the `CALENDAR`
  label and `#cal-tabs`, so it's visible whichever of Month/Week is active
  (same widget-id/CSS convention as `#events-search`/`#drive-search`/
  `#news-search`). `/` (`action_focus_search`) now focuses it instead of the
  old deliberate Calendar no-op branch.
- **`_cal_find(query)`** (Enter → `on_input_submitted`): finds the next
  grid cell whose event(s) match and moves the `DataTable` cursor there
  (`move_cursor(..., scroll=True)` + `focus()`), with a `Match n of m` notify.
  - **Month** (`#cal-grid`): jumps to the next **day** cell — matching days
    from `_cal_by_day` mapped to `(row, col)` via the same
    `offset + day - 1` layout `_apply_cal_month` builds.
  - **Week** (`#cal-week-grid`): jumps to the next **hour-cell** — matching
    `_cal_week_cells` keys mapped to `(hour, col + 1)` (column 0 is the Hour
    label). A multi-hour event spans several hour-cells, each a distinct
    jump target, so find-next walks the block hour by hour — deliberate.
  - **New query** starts at the first match at/after the current cursor
    (wrapping), so `/` behaves relative to where you're looking; **repeat-Enter
    of the same query** advances to the next hit and **wraps** past the last —
    the same `matches == last_matches → advance pos` idiom as `ThreadModal._find`.
  - **Matching** reuses the app-wide `_fuzzy_score()` (summary + description),
    so the short-query/false-positive behavior stays consistent everywhere:
    queries under `_FUZZY_MIN_QUERY_LEN` only match as exact substrings, never
    via `partial_ratio` (a 3-char `"tng"` fuzzy-scores 80 vs "budget planning
    meeting" but is correctly rejected).
  - **No match** notifies a warning ("No matching events in this view") and
    leaves the cursor put.
- Searches only what the active view currently has loaded (`_cal_by_day` /
  `_cal_week_cells`) — a jump within what's on screen, never a new fetch or a
  reach outside the rendered month/week.

### Verified
`run_test` + pilot, `size=(140,44)`, all `gauth` calls mocked (screenshot-script
template), fabricated events on known dates. 15/15 checks: `/` focuses
`#cal-search` from both Month and Week sub-tabs; a matching query jumps the
cursor to the correct day cell (Month) and hour-cell (Week); repeat-Enter
advances through multiple hits and wraps back; a non-matching query warns
without moving the cursor; the short non-substring `"tng"` does not
false-positive; and Email/Drive/News `/` still focus their own search inputs
(regression check — six other `/`-wiring changes landed today).

### Files
- `google_tui/main.py` — `#cal-search` widget + CSS, `action_focus_search`
  Calendar branch, `on_input_submitted` branch, `_cal_find` /
  `_cal_month_matches` / `_cal_week_matches` / `_event_matches`, and the
  `_cal_search_matches`/`_cal_search_pos` `__init__` state.
- `ROADMAP.md` — removed the (now empty) `## P2 — UX polish` section.

## [2026-07-15] — Email viewer (`ThreadModal`): help bar + remaining actions

Closes the ROADMAP P2 item "Email viewer (`ThreadModal`): help bar and
remaining actions." `R`/`A`/`F` already worked (the earlier binding-registry
pass); this finishes everything else it listed.

### Added
- **Contextual help bar inside `ThreadModal`** (`#thread-help`), consistent
  with the app's global help bar: `←/→ Prev/Next  R Reply  A Reply All
  F Forward  D Trash  S Archive  L Labels  / Search  Esc Close`. The
  actionable spans are clickable `[@click=screen.<action>]` links via a new
  `bindings.modal_help_markup(scope, ascii_mode)` (with a
  `_THREAD_MODAL_CLICK_ACTIONS` map). Routed to the `screen.` namespace, not
  `app.` — `GoogleTUI` also has `action_reply`/`reply_all`/`forward`, but
  those act on the Email *list*; the modal's own handlers must win.
- **Left/Right = prev/next message in place.** `ThreadModal` now takes
  `thread_ids`/`index` (the Email pane's display order, captured at open time
  via `_email_thread_order()`), and Left/Right re-run the same fetch/apply
  path for the adjacent thread without closing the modal. The title shows
  `THREAD (i/N)` while navigating. **No wraparound at the ends** — this
  matches the underlying list's own edges (you can't arrow "past" the last
  row in the Email pane either), so the modal shouldn't teleport from the
  last message back to the first.
- **`/` find-in-thread.** Reveals a hidden `#thread-search` Input; Enter
  scrolls to the next matching message (`Match n of m` notify), a repeated
  query advances find-next. Each message's searchable text is lowercased
  once at render time, so find is a cheap substring test, not a re-parse per
  keystroke. Escape closes the search box first if it's open, and only closes
  the whole modal on a second press.
- **`D` Trash, `S` Archive, `L` Labels** (with matching buttons in the
  button row, and a border on `#thread-box` that honors `Settings.ascii_mode`):
  - `gauth.trash_thread` — `threads().trash` (see naming note below).
  - `gauth.archive_thread` — `threads().modify` removing the `INBOX` label
    (reversible; the thread isn't deleted, just out of the inbox view).
  - `gauth.modify_labels` — `threads().modify` add/remove label ids, driven
    by a new `LabelPickerModal` (a `SelectionList` checklist of the account's
    user labels). Trash/Archive dismiss the modal with `"refresh"` so the
    Email pane drops the thread; Labels keeps the modal open.
- **`u` = mark unread from the Email list** (`gauth.mark_unread`,
  `threads().modify` adding `UNREAD`) — re-adds the `•` bullet without
  opening the thread. Added to the Email pane's context help row.

### Fixed — auto-focus stealing (found while finishing this)
`compose()` yields the `.hidden` `#thread-search` Input *before* the message
`VerticalScroll`. `.hidden` only sets CSS `display:none`, but Textual's
`Widget.focusable` keys off `visibility` (`DOMNode.visible`), which
`display:none` does **not** change — so the hidden search box still counted
as "focusable." With `App.AUTO_FOCUS = "*"`, `Screen._update_auto_focus()`
focuses the first focusable widget in DOM order on mount, i.e. that hidden
Input — and a focused `Input` swallows printable keys, so on the very first
open of the modal `R`/`A`/`F`/`D`/`S`/`L`/arrows all silently no-op'd (the
keypress went into the invisible search box instead). Confirmed live: before
the fix, pressing `d` immediately after open put `"d"` in the search Input and
never called `trash_thread`.

Fix: `ThreadModal.AUTO_FOCUS = ""`. Note it must be the **empty string, not
`None`** — `Screen.AUTO_FOCUS = None` means "inherit `app.AUTO_FOCUS`" (which
is `"*"`, the buggy value); only a falsy-but-not-`None` value makes
`_update_auto_focus`'s `if auto_focus and ...` guard skip focusing. Nothing
in this modal needs a default focus target; `/` still calls `.focus()` on the
search box itself, independently of `AUTO_FOCUS`.

### Notes / deliberate scope decisions
- **Trash, not permanent delete.** The ROADMAP wording said "delete," but
  `gauth.trash_thread` calls `threads().trash` (recoverable ~30 days),
  exactly like Gmail's own web "Delete" button — deliberately NOT
  `threads().delete` (permanent, irreversible). No user pressing a "delete"
  key on an email expects it to be unrecoverable; the recoverable behavior is
  the safe and least-surprising default. Named `trash_thread` (not
  `delete_thread`) so the code makes that distinction obvious.
- **Label picker is assign-only (add), not a full add/remove editor.** The
  thread-body fetch (`gauth.get_thread`) doesn't return per-thread `labelIds`,
  so we can't pre-check "already applied" labels to offer removal without an
  extra round-trip. "Assign labels" is what the ROADMAP asked for; a
  remove/toggle editor is a reasonable future extension (documented in
  `LabelPickerModal`'s docstring). System labels are filtered out of the
  picker — only user labels are assignable.

### Verified
`run_test` + pilot, `size=(140,44)`, every `gauth` call mocked (including
`trash_thread`/`archive_thread`/`modify_labels`/`mark_unread` — never run
against the live account): 26/26 checks — genuine list→modal open passes the
Email-pane order/index; no widget auto-focused on open (the fix); `d`/`s`/`l`
immediately after open call the right mocked mutation (`l` via
`LabelPickerModal`, modal stays open after apply); Left/Right page between
threads without closing and don't wrap at the ends; `/` opens the box +
scrolls to a match, Escape closes the box then (second press) the modal; the
help bar renders with working `[@click=...]` markup; and R/A/F still dismiss
with the correct compose tuple (no regression from the AUTO_FOCUS change).
A separate single-process run confirmed `u` calls `mark_unread` with the
highlighted thread id.

Files: `google_tui/gauth.py` (`mark_unread`/`trash_thread`/`archive_thread`/
`modify_labels`), `google_tui/bindings.py` (ThreadModal action specs +
`modal_help_markup`), `google_tui/main.py` (`ThreadModal`, `LabelPickerModal`,
`_email_thread_order`, `action_mark_unread`, help-bar CSS).

## [2026-07-15] — Calendar create event (`n`, Calendar tab + Mail's Events pane)

### Added
Closes the ROADMAP P2 item "Calendar create event." The Calendar tab (and
the Mail tab's Events pane) were read-only before this.

- `gauth.create_event(svc, summary, start, end, all_day=False,
  description="", location="")` — thin wrapper over
  `events().insert(calendarId="primary", ...)`, following `list_events`/
  `events_between`/`month_events`'s existing style. Returns the raw
  created-event dict `insert` hands back, the same shape those three
  already return, so a new event merges straight into the existing
  Month/Week grids and the Mail tab's Events pane with no special case.
  `start`/`end` are `datetime.date` for an all-day event (Calendar's API
  distinguishes `{'date': ...}` from `{'dateTime': ..., 'timeZone': ...}`)
  or a tz-aware `datetime.datetime` for a timed one — the caller attaches
  a timezone; `create_event` deliberately omits an explicit `timeZone`
  field since the offset embedded in `dateTime` is sufficient for a
  non-recurring event. No `attendees` are ever set — this app has no
  Contacts-based invite flow for events, out of scope here.

- **`main.py`: a NEW `CreateEventModal`, not an `EventModal` "create
  mode."** `ComposeModal` gained `mode == "new"` (P1 M5) cheaply because
  its reply/reply_all/forward/new modes all share the exact same three
  widgets (`#c-to`/`#c-subject`/`#c-body`) and only change what pre-fills
  them. `EventModal`'s view is a single read-only `Static` detail block
  with no input widgets at all — reusing it for creation would mean
  composing every one of the create form's Input/Switch widgets even for
  the ordinary view path and hiding them with CSS, just to share a "Close"
  button. Not worth it; `CreateEventModal` is its own small `.pane`-
  Container modal (title `Input`, an all-day `Switch`, date/start/end
  `Input`s, Create/Cancel buttons) — same minimal shape as `ContactModal`/
  `NewsEntryModal`.

- **Date/time input is plain-text `Input`s** (`YYYY-MM-DD` / `HH:MM`,
  24-hour), the same "no native date-picker widget" precedent as the
  Navigation tab's origin/destination address inputs — Textual has no
  built-in date picker and this app isn't building one from scratch for a
  single form. The all-day `Switch` *disables* (not hides) the two time
  inputs, so toggling it doesn't discard whatever the user already typed.

- **Defaults, deliberately conservative** (this is Brad's real, live
  calendar — see AGENTS.md's caution at the top of this file's own repo
  instructions): no attendees ever, a plausible 9:00–10:00 default time
  block (easy to change, better starting point than a zeroed field), and
  the date field seeds from what the Calendar tab already has in view —
  today if today falls inside the currently-viewed month/week, else the
  1st of the viewed month or that week's Monday (`_cal_default_day`).
  There's no "currently highlighted day" concept in either grid outside of
  actually clicking a populated cell (which opens `DayEventsModal`/
  `EventModal`, not this one), so this is the practical stand-in for that.

- **Binding: `n`** (new `ActionSpec` in `bindings.py`, scope `global` —
  same "bind globally, no-op unless the right tab/pane is active" pattern
  as `c`/compose). Active on the Calendar tab (both Month and Week
  sub-tabs) and the Mail tab's Events pane, since both show events. `c`
  was already taken by Email's Compose; `n` was free.

- **Refresh wiring reuses existing paths, doesn't invent a new one.**
  `CreateEventModal` dismisses `True` on a successful create (mirrors
  `TaskModal`'s `self._mutated` dismiss-timing pattern — the callback
  fires *before* the modal is actually popped, so touching base-screen
  widgets has to wait). `GoogleTUI._on_create_event_result` then runs one
  worker-thread call (`_after_create_event_thread`) that (1) calls the
  existing `_refresh_all_thread()` — the same refresh `action_toggle_task`
  and a sent message already trigger — to update the Mail tab's Events
  pane (`self._events_cache`), and (2) rebuilds whichever Calendar grid is
  currently active via the existing `_fetch_cal_month`/`_apply_cal_month`
  or `_fetch_cal_week`/`_apply_cal_week` fetch/apply pair. No new cache
  category, no new apply path.

### Verified
Throwaway `run_test` + pilot scripts (`size=(140, 44)`, every `gauth` call
mocked including the new `create_event`, per AGENTS.md §6 — this is
Brad's real calendar) covering, each in its own process: a timed event
created from the Calendar tab's Month view (right args to `create_event`,
tz-aware start/end, modal dismisses, event shows up in both
`_events_cache` and `_cal_by_day` afterward without restarting the app);
an all-day event (Switch disables the time inputs, `end` is `start + 1
day` since Calendar's all-day end date is exclusive); the same flow
triggered from the Mail tab's Events pane instead of the Calendar tab; and
a Week-view create confirming `_cal_week_cells` gets rebuilt too, not just
the Month grid.

## [2026-07-15] — Task subtasks: add/toggle/delete in TaskModal

### Added
Closes the ROADMAP P2 item "Task subtasks + add/delete." `TaskModal`
(ROADMAP called it `TaskDetailModal` — that name doesn't exist; the real
class is `TaskModal` in `main.py`) previously showed only title/status/due/
notes and had NO subtask UI at all — the ROADMAP's "shows subtasks
read-only" claim was stale/inaccurate; subtasks weren't shown anywhere in
the modal.

**API shape, verified before writing helpers (diverges from the literal
ROADMAP wording):**
- `gauth.create_task(svc, tasklist_id, title, parent=None, notes=None)` —
  Google models a subtask as an ordinary task whose `parent` field points at
  another task's id in the same tasklist; `tasks().insert` accepts `parent`
  as a query param that makes the new task a child of it. One helper covers
  both "add a top-level task" and "add a subtask" — no separate subtask
  endpoint exists.
- `gauth.delete_task(svc, tasklist_id, task_id)` — `tasks().delete`; works
  for a subtask OR a parent (Google cascades to children server-side).
- **No `patch_subtask` was added.** Toggling a subtask's completion is
  exactly `gauth.set_task_status` — already used by the Tasks pane's
  Space-to-toggle — since a subtask is still just `tasks().patch` by id
  under the hood. A separate wrapper would have been a no-op pass-through.
- `list_tasks` already returns every task flat, tagged with `_list` and
  (when present) `parent` — finding a task's children needs no extra API
  call, just a client-side filter (new module-level `_child_tasks` helper
  in `main.py`). Google's `tasks().move()` (real re-parenting/reordering
  endpoint) was considered and NOT used — nothing here needs to convert an
  existing task into another task's child, only create new subtasks and
  delete existing ones.

**`main.py`**: `TaskModal` gained a subtask `ListView` (Space toggles
complete, Delete removes, both mirroring the Tasks pane's own bindings —
necessary because Textual truncates the App-level binding walk at a
`ModalScreen` boundary, same reason `ThreadModal` needed its own `r`/`a`/`f`
bindings), an `Input` + "Add Subtask" button (same small-form pattern as
Settings' feed-add row), and a "Delete Task" button. Every gauth call runs
via `self.run_worker(..., thread=True)` per AGENTS.md §2.

**Confirm-before-delete: subtask no, top-level task yes.** A subtask delete
has no confirm dialog — consistent with this app's existing no-confirm
precedent (AGENTS.md §7) and low stakes (one small item, trivially
re-added). Deleting the TOP-LEVEL task reuses the existing `ConfirmModal`
first, because it also cascades to every subtask under it server-side and
closes the whole modal — judged worth the one extra keypress even though
nothing else in this app confirms before a mutation.

**Refresh-after-mutation had to be modal-aware.** The natural approach —
call `self.app._refresh_all_thread()` (the same call
`action_toggle_task`'s existing toggle makes) right after each mutation —
raised `NoMatches("#task-list")` intermittently: `_refresh_all_thread` ends
up doing `self.query_one("#task-list")`, and `App.query_one` resolves
against `self.screen`, the CURRENTLY ACTIVE screen (AGENTS.md's existing
NOTE on this), which is `TaskModal` while it's still open, not the base
screen `#task-list` lives on. Fixed by tracking `self._mutated: bool` on
the modal, `dismiss(self._mutated)` on close/Escape/after a task delete,
and a new `_on_task_modal_result` callback (mirrors the existing
`_on_compose_result`'s `if result == "sent": run_worker(...)` shape) that
only runs `_refresh_all_thread()` once the modal has actually dismissed.

**Bonus fix, found while touching this code: `TaskModal` could not open at
all.** `Screen.task` is a read-only property in this Textual version (the
screen's own asyncio `Task`); the old `TaskModal.__init__`'s `self.task =
task` therefore raised `AttributeError: property 'task' of 'TaskModal'
object has no setter` on EVERY construction — confirmed by instantiating
the pre-change class directly. This means pressing `Enter` on any task in
the Tasks pane has been silently crashing (worker exception, caught by
`_handle_exception`, logged, no visible detail popup) since this modal was
written, independent of subtasks. Fixed by renaming the attribute to
`self.task_data` throughout the class.

### Verified
Scratch pilot tests (`run_test`, `size=(140, 44)`), every `gauth` call
mocked including `create_task`/`delete_task` (deleted after use, per
AGENTS.md §6 — no `tests/` convention yet): opening `TaskModal` on a
fabricated task with fabricated subtasks shows both; pressing `Space` on a
highlighted subtask calls `gauth.set_task_status` with the right
`(tasklist_id, task_id, done)`; typing a title and pressing `Enter` calls
`gauth.create_task(..., parent=<parent task id>)` and the modal's list grows
to include it; pressing `Delete` on a highlighted subtask calls
`gauth.delete_task` and the row disappears. Also caught and fixed the
`ListView.clear()` AwaitRemove race from AGENTS.md's existing NOTE
(`_render_subtasks` now `await`s `clear()` instead of firing it and
`extend()`-ing immediately) — reproduced as an intermittent `DuplicateIds`
during this same verification pass.

### Files touched
`google_tui/gauth.py` (`create_task`, `delete_task`), `google_tui/main.py`
(`TaskModal` rewrite, `_child_tasks`, `_on_task_modal_result`, the
`TaskModal` push-site in `on_list_view_selected`), `ROADMAP.md`.

## [2026-07-15] — Narrow-terminal (80x25) responsive layout

### Added
Closes the ROADMAP P2 item: layout used to be fixed-percentage CSS only
(`#left { width: 65%; }`, `#drive-list-col { width: 40%; }`), untested below
the screenshot harness's 150x42 and unusable at the 80x25 the ROADMAP named
as the real target.

**Breakpoint mechanism — verified Textual 8.2.8 first, used its native
feature instead of a manual one.** Before writing any resize-tracking code,
checked whether this Textual version has something like a CSS container
query — it does: `App`/`Screen.HORIZONTAL_BREAKPOINTS` / `VERTICAL_BREAKPOINTS`
(`screen.py`'s `Screen._on_resize`), a list of `(min_width, class_name)`
tuples; Textual applies the highest-matching class to the `Screen`
automatically on every resize, no app code needed to detect the resize
itself. `GoogleTUI` now sets `HORIZONTAL_BREAKPOINTS = [(0, "-narrow"),
(NARROW_WIDTH_THRESHOLD, "-normal")]` (`NARROW_WIDTH_THRESHOLD = 100`), and
the purely-visual part of this (Drive tab stacking, below) is driven
entirely by `Screen.-narrow ...` CSS selectors — no Python logic at all.
100, not 80, so the smallest verified size (80x25) sits comfortably inside
`-narrow` rather than right at the boundary.

The one part that ISN'T pure CSS — hiding every Mail-tab pane except the
active one, since "which pane is active" is runtime state a CSS selector
can't see — is handled in Python: `GoogleTUI.on_resize` (the public,
user-overridable hook — confirmed distinct from Textual's own internal
`_on_resize`, so no MRO-dispatch collision the way a naive `_on_click`
override would have, per AGENTS.md §2's NOTE) recomputes `self._narrow`
from the event's width and calls `_apply_narrow_layout()`, which is also
called from `_focus_pane()` so switching panes while already narrow
re-applies the hide/show immediately. `on_mount` also seeds `self._narrow`
directly from `self.size.width` rather than trusting resize-event ordering
relative to mount, so a launch straight into an 80x25 terminal starts
correct instead of only fixing itself on the first real resize.

**Stack vs. hide, decided per surface:**
- **Drive tab (list + preview): STACK.** `#drive-list-col`/`#drive-preview-col`
  go from side-by-side to a 60/40 top/bottom split (`Screen.-narrow
  #drive-body { layout: vertical; }` + height overrides) when narrow. Only
  two panes here, and both are genuinely useful at once even at 80 columns —
  the list to keep browsing, the preview's who/what/where/when (and text,
  when previewable) to actually read something — so hiding either one would
  leave Drive either a bare filename list or a preview with nothing to
  browse. A 60/40 height split still leaves each one usable in 25 rows.
- **Mail tab (Email vs. Events/Tasks/Hermes): HIDE.** This is a 1-vs-3 split,
  and Events/Tasks/Hermes are already themselves stacked vertically inside
  `#right` — stacking Email on top as a 4th thing would quarter an already-
  scarce 25 rows into unreadable slivers, the opposite of "primary content
  stays dominant." Instead, exactly ONE pane is shown at a time, full width
  and full height: `#left`/`#right` and the individual `#events`/`#tasks`/
  `#hermes` containers get a `.narrow-hidden` (`display: none`) class
  toggled by `_apply_narrow_layout()` based on `PANE_IDS[self.active]` —
  the same active-pane tracking `Alt+1..4`/`Tab`/`Shift+Tab`/arrows already
  drive via `_focus_pane`, so no new navigation was needed, switching panes
  IS switching which column is visible. `Screen.-narrow #left, Screen.-narrow
  #right { width: 1fr; }` was also needed: `display: none` on the sibling
  doesn't relinquish its share of the row, so without this override the
  visible column stayed pinned at its normal 65%/1fr width with dead space
  next to it (caught and fixed during visual verification, below).

**Help-bar text**: `_narrow_wrap()` (`main.py`) word-wraps
`HELP_GLOBAL_TEXT` (111 chars) and the per-tab/pane `CONTEXT_HELP` strings
(up to 132, for `tab-settings`) via `textwrap.wrap(text, width=self.size.width
- 2)` whenever `self._narrow` is set — never truncated/abbreviated, since
`Static`'s own `DEFAULT_CSS` (`height: auto`) already lets `#help-bar` grow
to fit however many wrapped lines result, so wrapping loses no information
and never cuts a word mid-character. Left byte-for-byte unchanged above the
threshold, where every existing string already fit on one line at the sizes
this app was tested at before. Wired into `_update_help_bar()` (context row)
and a new `_update_help_global()` (global row, factored out of
`_apply_ascii_mode()`, which now also gets narrow-wrap for free), both
called from `on_resize` so the wrap width tracks the exact terminal width,
not just the narrow/not-narrow boundary.

### Verified
Throwaway `run_test` + pilot scripts, one process per size per AGENTS.md
§6 (every `gauth` call mocked, modeled on `scripts/generate_screenshot.py`):
`size=(80, 25)` (the new target), plus `size=(150, 42)` and `size=(140, 44)`
(the two sizes already in use elsewhere) to confirm no regression. At 80x25:
Email-pane-active screenshot shows `#left` filling the full width with 8
readable subject/sender rows and no `#right` bleed-through; Events-pane-
active screenshot shows Events full width/height with Tasks/Hermes/Email
all hidden; Drive tab screenshot shows the file list stacked above the
(empty-in-this-fixture) preview pane, both full width. At 150x42/140x44,
confirmed byte-identical layout/class state to before this change
(`narrow-hidden` never applied, `Screen` carries `-normal` not `-narrow`,
help text unwrapped). SVG screenshots converted to PNG via `cairosvg` and
visually inspected (not just asserted) at both the narrow and normal sizes;
this is what caught the `#left`/`#right` width bug above before it shipped.

### Files touched
`google_tui/main.py`, `ROADMAP.md`.

## [2026-07-15] — ASCII-safe mode (Settings toggle) for terminals that mangle Unicode

### Added
New `Settings.ascii_mode: bool` (default `False`, `settings.py`), a Switch
in Settings → General → "Display" (`#settings-ascii-mode-switch`) labeled
"ASCII-safe mode (for limited terminals)". Applied **live**, the same way
`show_sender_address` already is — not restart-required like the
encrypt-at-rest switch. Rationale: encrypt-at-rest needs a restart because
switching it clears/re-derives the on-disk cache's encryption key, a real
data/security boundary; ASCII mode only changes how already-loaded data and
UI chrome are *rendered*, nothing about cache contents or keys, so there's
no reason to make the user restart for a cosmetic toggle.

Covers every surface the ROADMAP item named:
- **Tab-number glyphs**: `_tab_label()` (`main.py`) now takes an
  `ascii_mode` bool and emits a plain `1`..`8` instead of the `_SUPERSCRIPT`
  digit when set. New `TAB_LABEL_SPECS` (tab id/text/number triples) lets
  `_apply_ascii_mode()` relabel every mounted `Tab` live via
  `TabbedContent.get_tab(id).label = ...` (confirmed this re-renders
  immediately — `Tab.label`'s setter calls `self.update()`).
- **CSS borders**: rather than recompiling CSS or walking widgets to poke
  `.styles.border` directly, added a parallel `.ascii-border`-suffixed CSS
  rule for every container that had a `round` (or, for two border-bottom-
  only rules, `solid`) border — `.pane`/`.pane-active`/`.section`,
  `#hermes-log`, `#drive-list-col`, `#drive-preview-col`, `#browser-doc`,
  `#nav-log`, `#settings-feed-list`, `#c-to-suggestions`,
  `#drive-preview-meta`, `.thread-msg-header` — using Textual's built-in
  `ascii` border style (`+`/`-`/`|`, confirmed via
  `textual._border.BORDER_CHARS["ascii"]`). `_apply_ascii_mode()` just
  toggles the `ascii-border` class on the matching widgets
  (`widget.set_class(ascii_mode, "ascii-border")`); Textual's normal CSS
  specificity/cascade does the rest, live, no runtime style mutation code
  needed. The extra class in each selector (e.g. `.pane.ascii-border`) is
  what gives it enough specificity to win over the base `.pane` rule
  regardless of declaration order; `.pane-active.ascii-border` is still
  declared after `.pane.ascii-border` to preserve the same override
  ordering the two non-ascii rules already had.
- **Arrow glyphs in help text**: new `bindings.ascii_safe()` — a small
  `←/→/↑/↓` → `<-`/`->`/`^`/`v` substitution table, applied to
  `HELP_GLOBAL`/`_context_help_text()`'s output (both refreshed by
  `_apply_ascii_mode()`/`_update_help_bar()`) and to `HelpModal`'s
  `HELP_TEXT` (read fresh from `self.app.settings.ascii_mode` every time
  `Ctrl+H` recomposes the modal — no live-refresh needed since it's never
  kept mounted). Kept as a find/replace over the existing hand-curated
  strings rather than a second copy of every help string.
- **Curly quotes/dashes/bullets**: `render.decode_html_entities()` gained
  an `ascii_mode: bool = False` parameter (default off, so every existing
  caller is unaffected) — a second substitution pass, applied *after* the
  real Unicode character has been produced, covering only the punctuation
  this function itself introduces (curly quotes, em/en dash, ellipsis,
  bullet, middot, guillemets). `render.py` stays I/O-free and knows
  nothing about `Settings` — every function on the call path
  (`_extract_title`, `_html_to_blocks`, `_extract_nav_links`, `parse_html`,
  `parse_feed_entry`) now threads the same plain bool through, and
  `main.py`/`fetchers.py` are the only places that read
  `Settings.ascii_mode` and pass it in — at every real call site:
  `fetchers.fetch_http`, `fetchers._strip_tags`/`search_duckduckgo`/
  `search_searxng` (via `fetchers.run_search`, which now reads
  `settings.ascii_mode` itself alongside the provider/API-key fields it
  already read off the same `Settings` object), `fetchers.compute_route`
  (Navigation tab's turn-by-turn instructions), and `main.py`'s two
  `ThreadModal`/`NewsEntryModal` → `render.parse_feed_entry()` call sites.

### Left as Unicode (honest gaps, not silently skipped)
- `parse_gopher_menu`/`parse_gemtext` never call `decode_html_entities` at
  all (gopher/gemtext have no HTML-entity concept), so `ascii_mode` has no
  effect on Gopher/Gemini page content — nothing to wire up there.
- The Email pane's unread-thread bullet (`"•" if th["unread"] else " "` in
  `_email_collapsed_line`, `main.py`) and the `"…"` ellipsis used all over
  `main.py` for truncated placeholders/snippets/"Loading…"/"Connecting…"
  status text are genuine non-ASCII glyphs this pass did NOT convert — they
  weren't inside `decode_html_entities`'s scope (they're hardcoded directly
  in `main.py`, not decoded from HTML entities) and converting every one of
  them was judged a much larger, lower-value diff than the four surfaces
  the ROADMAP item actually named. Flagging honestly rather than silently
  leaving it out of this note: a genuinely complete ASCII-safe mode would
  still mangle on a terminal that can't render `•`/`…`.

### Verified
Throwaway `run_test` + pilot script (`size=(140, 44)`, every `gauth` call
mocked): (1) `render.decode_html_entities(text, ascii_mode=True)` turns
`“Hello” — a • point… «quote»` into `"Hello" - a * point... "quote"` (pure
ASCII, confirmed via `ord(c) < 128` on every character) while
`ascii_mode=False` is byte-for-byte unchanged from before this change; (2)
`Settings(ascii_mode=True)` → `save_settings` → `load_settings()` round-
trips `True`, and same for `False`; (3) toggling
`#settings-ascii-mode-switch` live in a mounted app changes the Mail tab's
label from `"Mail ¹"` to `"Mail 1"` and `#email`'s `styles.border_top`
style from `"round"` to `"ascii"` (color unchanged, confirming the
`.pane-active` accent-color rule still won), and toggling back reverts
both cleanly.

### Files touched
`google_tui/settings.py`, `google_tui/main.py`, `google_tui/bindings.py`,
`google_tui/render.py`, `google_tui/fetchers.py`, `README.md`,
`ROADMAP.md`.

## [2026-07-15] — Extend `/` live search to Events, Drive, News, and Contacts

### Added
`action_focus_search` (`main.py`) previously early-returned for every tab
except Mail's Email/Tasks panes (see the `[2026-07-15]` "Live search within
the Email and Tasks panes" entry below). It now also covers:
- **Events pane** (Mail tab): new `Input#events-search`, near-copy of the
  Tasks wiring — `_fuzzy_filter_events` (filters `self._events_cache` on
  summary/description) + `_refresh_event_list`/`_apply_event_list_async` in
  their own exclusive worker group (`"event-search-apply"`), same reasoning
  as Tasks' own group: sharing `"mail-apply"` would let a keystroke cancel
  an in-flight full mail-data rebuild mid-repopulate. `_append_event_items`
  extracted from `_apply_mail_data_async`'s inline event-row building so
  both paths share it.
- **Drive tab**: new `Input#drive-search`, filters `self._drive_files` by
  name — scoped to the CURRENT folder's listing only, never the whole Drive
  tree, never a re-fetch per keystroke (`_fuzzy_filter_drive_files`,
  `_refresh_drive_list`/`_apply_drive_search_async`, own
  `"drive-search-apply"` group so a keystroke can't cancel an in-flight
  folder navigation). The "up" row stays unfiltered chrome.
- **News tab**: new `Input#news-search`, filters the combined-feed entry
  list by title/summary (`_fuzzy_filter_news_entries`). New
  `self._news_entries_cache` holds the last full entry set so the filter
  survives repeated `_apply_news_data` calls (cache load, live refresh,
  feed add/remove); own `"news-search-apply"` worker group, same
  don't-cancel-the-full-rebuild reasoning as Events/Drive.
- **Contacts tab**: no new filtering logic — `_fuzzy_filter_contacts` already
  existed and worked, it just wasn't reachable via `/` (only auto-focused on
  tab activation). `action_focus_search` now focuses `#contacts-search` for
  this tab too, so `/` works after focus has moved elsewhere (e.g. to
  `#contacts-list`).

All four reuse `_fuzzy_score()` (the `_FUZZY_MIN_QUERY_LEN`/threshold-75 fix
from the Email/Tasks search entry below) rather than reintroducing the
short-query false-positive bug it fixed.

Calendar is unchanged — still an explicit no-op, now with its own ROADMAP
item (`/` jump-to-next-match on the date grid) instead of being folded into
the generic "extend search" item, since it needs a different interaction,
not just another `ListView` filter.

### Verified
Throwaway `run_test` + pilot script (fabricated events/drive-files/news-
entries/contacts dataset, every `gauth` call mocked, no real network) —
confirmed for each of Events/Drive/News/Contacts: `/` focuses the right
search input, a short (3-char) non-substring query that would false-
positive under raw `rapidfuzz.fuzz.partial_ratio` (e.g. `"den"` scoring 80
against `"Entertainment center setup"`) correctly shows zero results, a real
query filters correctly, and clearing the box restores the full list. Also
confirmed Calendar's `/` remains a no-op (regression check).

## [2026-07-15] — Browser tab: Alt+H home, real fix for Alt+Left/Right/Up/Down, instant Page Up/Down/Home/End

### Added
Alt+H now jumps the Browser tab to a configurable home URL — new
`Settings.browser_home_url` (default `https://www.google.com`), editable
via a new "Browser" row (`Input#settings-browser-home-url` +
`Button#settings-save-browser-home`) in Settings → General, right next to
the existing update-check switch. `action_browser_home` (`main.py`,
`bindings.py`'s new `browser_home`/`alt+h` `ActionSpec`) mirrors the
existing bookmark-click flow: it's a no-op off the Browser tab, same as
`[`/`]` on the Calendar tab.

### Fixed
**Alt+Left "not going back"** — reproduced with a raw-byte test against
Textual 8.2.8's `XTermParser` (not just `pilot.press`, which posts a
pre-parsed `Key("alt+left", ...)` directly and therefore can't see this
class of bug at all): feeding `"\x1b[1;3D"` (the CSI-with-modifier-3 form
of Alt+Left) through the parser correctly yields a single
`Key(key='alt+left', ...)`, but feeding `"\x1b\x1b[D"` (the "double-ESC"
form several common terminals send instead for Alt+Arrow — a literal ESC
prefixing the plain, unmodified arrow-key sequence) yields TWO
independent events: `Key('escape', ...)` then `Key('left', ...)`.
`_xterm_parser.py` hits a hardcoded `process_alt=False` when a second ESC
interrupts the still-unresolved first escape sequence, so it never
synthesizes a combined `alt+left`. Depending on what's focused, the stray
`left` half then either moved the address bar's text cursor (`Input`
binds bare `left`/`right` to cursor movement) or was silently dropped
(`DocumentView` has no bare-arrow binding) — so "focus swallowing the
combo" was directionally right, but the actual mechanism was this
upstream parser gap, not anything in this app's own focus handling.
Fixed with a small compensating `GoogleTUI.on_key` override: it tracks
the timestamp of a lone `escape` Key event, and if the *very next* event
is a bare `left`/`right`/`up`/`down` within 50ms (the two halves of one
real escape sequence land in the same `feed()` call — effectively zero
elapsed time — while two genuinely separate human keypresses are always
much further apart than that), it runs the same action the real
`alt+<direction>` binding would and calls `event.prevent_default()` to
suppress the base `App._on_key`'s own binding walk (which is what would
otherwise run the address bar's cursor move) — the same
runs-before-base-class-and-can-suppress-it pattern already documented for
`GtHeader._on_click` in AGENTS.md §2. Since the underlying gap is
terminal-encoding-dependent, not Browser-tab-specific, this transparently
fixes the same dead-Alt-arrow symptom for Mail-pane navigation and
Settings sub-tab cycling too, on any terminal that uses the double-ESC
encoding; terminals that already send the combined form are unaffected
(the compensation only ever triggers on a lone `escape` immediately
followed by a bare arrow, which never happens for them).

**Page Up/Down/Home/End "very slow" in `DocumentView`** — profiled with a
fabricated ~2000-paragraph-block document and `time.perf_counter()`
around individual keypresses in a `run_test` pilot. `_render_blocks`
itself (the actual markup/link-styling work) was never the problem — 5000
blocks render in ~40ms, done once when `.document` is set, not on every
scroll. The real cost: `DocumentView` never overrode Textual's default
scroll actions, so Page Up/Down/Home/End used Textual's stock *animated*
scroll — `scroll_page_up`/`scroll_page_down` default to `speed=50`
"lines per second" (so a ~30-line viewport page took ~0.6s to glide,
confirmed: ~0.68s measured per Page Down keypress, flat regardless of
document size, matching a fixed-speed animation rather than a
size-proportional render cost) and `scroll_home`/`scroll_end` default to
a flat `duration=1.0` regardless of distance (confirmed: ~1.08s per
Home/End keypress, again flat across document sizes). `DocumentView` now
overrides `action_page_up`/`action_page_down`/`action_scroll_home`/
`action_scroll_end` to scroll with `animate=False`. Re-measured after the
fix: Page Down/Up/Home/End all dropped to ~0.09-0.1s (down from
~0.68-1.09s), which is now just ordinary keypress/repaint overhead, not
an animation duration — confirmed flat from 1 block up to 5000. (Aside,
not fixed here since it's outside what was reported slow: the *initial*
`.document =` assignment for a very large document — e.g. ~8s for 5000
blocks in the pilot harness — does scale with size; that's Textual laying
out one big `height: auto` `Static`, a separate concern from the
scroll-animation bug above.)

### Files
`google_tui/bindings.py` (`browser_home`/`alt+h` `ActionSpec`, `CONTEXT_HELP`/
`HELP_TEXT` updated), `google_tui/settings.py` (`Settings.browser_home_url`),
`google_tui/main.py` (`action_browser_home`, `GoogleTUI.on_key` +
`_ESCAPE_ALT_ARROW_ACTIONS`/`_ESCAPE_ALT_ARROW_WINDOW`, `self._pending_escape_time`,
new Settings → General "Browser" row + its `on_button_pressed` branch),
`google_tui/render.py` (`DocumentView.action_page_up`/`action_page_down`/
`action_scroll_home`/`action_scroll_end`).

### Verified
No automated test suite exists yet (ROADMAP P4). Verified with throwaway
`run_test`-pilot scripts (fabricated data, every `gauth`/`fetchers` call
mocked, deleted after use — not committed): Alt+H navigates to the
configured home URL from the Browser tab and is a no-op elsewhere;
Alt+Left correctly goes back after a couple of navigations when the
double-ESC sequence's two halves (`Key('escape', ...)` then
`Key('left', ...)`, posted back-to-back with no artificial delay, the way
the real parser emits them) are delivered with focus on either the
address bar or the document view; a real combined `alt+left` (the form
terminals that already work correctly send) still works, unchanged; a
standalone `escape` (no follow-up arrow) and a genuine bare `left`
keypress with no preceding `escape` both behave exactly as before (no
false-positive back/forward); and a before/after timing comparison on a
fabricated large document confirmed the Page Up/Down/Home/End fix (see
above).

## [2026-07-15] — Numbered inline links now work in ThreadModal/NewsEntryModal, and look like links

### Added
`render.py`'s `[N]` link numbering (nav + inline content links) already
rendered correctly everywhere `DocumentView` is used, but
`on_document_view_link_activated` (`main.py`) only ever acted on it while
the Browser tab itself was active — pressing a link's number inside
`ThreadModal` or `NewsEntryModal` silently did nothing. Both now work:
activating a link in either modal closes the modal, switches to the
Browser tab, and loads the URL there — the same "open link in browser"
behavior a mail/feed reader gives you, since neither modal has an
in-place page to navigate. Implementation note: `ModalScreen.dismiss()`
pops the screen stack synchronously but the DOM teardown is deferred, so
the tab-switch + navigate is done one `call_after_refresh` step later,
the same pattern already used for `_browser_resume_gemini_input`.
`ThreadModal`'s existing "no cross-message link renumbering" design
(each message keeps its own independent `[N]`s) is unaffected — this
only ever resolves the single link actually activated in whichever
message's `DocumentView` had focus.

Also gave link text an actual visual style: `_stylize_links` used to only
dim the bracketed `[N]` marker, leaving the anchor text itself looking
like plain body text. It now additionally locates each link's full
"anchor text `[N]`" (or, for the nav bar, "`[N]` anchor text") span and
layers a new `underline bright_cyan` style over it, so the whole link —
not just its number — reads as a link. Gopher/Gemini menu items (where
the entire block *is* the link, per `Block.link`) get the same style
applied to the whole line directly rather than via substring search. The
old "dim" pass on the bracket is left in place underneath as a fallback,
so a link whose span can't be positively matched (block text reshaped
somewhere unexpected) degrades to exactly the old look instead of erroring.

### Files
`google_tui/render.py` (`_LINK_STYLE`, `_stylize_links`/`_render_block`/
`_render_blocks`/`_render_nav` now thread `document.links` through so
they can style anchor text, not just the marker), `google_tui/main.py`
(`on_document_view_link_activated`, new `_open_link_in_browser` helper,
`ThreadModal` docstring updated to describe the new behavior instead of
the old no-op).

### Verified
No automated test suite exists yet (ROADMAP P4). Verified with three
throwaway `run_test`-pilot scripts (fabricated data, every `gauth`/
`fetchers` call mocked, deleted after use — not committed): (1) a
fabricated thread with an HTML link, opened via the normal Email-pane
flow, `1`+Enter on the message's `DocumentView` — asserted the app
switched to the Browser tab and loaded the URL; (2) same for a
fabricated feed entry pushed straight into `NewsEntryModal`; (3) a
regression check that the Browser tab's own pre-existing link activation
(navigate to a page, `1`+Enter on a link in `#browser-doc`) still works
exactly as before.

## [2026-07-15] — Live search within the Email and Tasks panes

### Added
`/` (new `focus_search` binding, `google_tui/bindings.py`) focuses a new
search box in the active pane — `Input#email-search` (Email) or
`Input#tasks-search` (Tasks) — and typing live-filters the list, debounced
the same way Contacts search already worked (`_EMAIL_SEARCH_DEBOUNCE` /
`_TASKS_SEARCH_DEBOUNCE`, 0.15s). Filters `self._threads_cache` /
`self._tasks_cache` client-side — no Gmail/Tasks call per keystroke.
Clearing the box restores the full list. Closes the ROADMAP P2 "Search
within panes" item.

### Fixed (before merge, not shipped broken)
The first implementation reused `_fuzzy_filter_contacts`'s
`rapidfuzz.fuzz.partial_ratio`-with-threshold-60 approach verbatim. That
degrades badly once the target text is much longer than the query — e.g.
`fuzz.partial_ratio("cat", "pay electric bill")` scores 66.7, clearing the
threshold and putting an unrelated task in the results for a search for
"cat". `_fuzzy_score()` now requires an exact substring match for queries
under 4 characters, and only falls back to `partial_ratio` (threshold
raised to 75) for longer queries where typo tolerance still makes sense
without the false positives. Caught by a `run_test` pilot against a
fabricated dataset before merge, not by a user report.

### Notes
The debounced re-render for Email reuses the existing `_apply_email_list`/
`"mail-apply"` worker group (same as the Contacts pattern). Tasks required
a genuinely new render path (`_apply_task_list_async`) in its **own**
exclusive worker group (`"task-search-apply"`), not `"mail-apply"`:
`_apply_mail_data_async` rebuilds Email+Events+Tasks together in one
coroutine, so a search keystroke sharing that group could cancel an
in-flight full refresh after it clears `#task-list` but before it
repopulates `#email-list`/`#event-list`, leaving those panes blank.

## [2026-07-15] — Central keybinding/help-bar registry; ThreadModal r/a/f now work

### Added
New `google_tui/bindings.py`: a single `ActionSpec` registry that generates
the App's `BINDINGS`, both help-bar rows (`HELP_GLOBAL`/context text), and
`HelpModal`'s `HELP_TEXT` — these previously lived as four independently
hand-maintained strings in `main.py` that could (and did) drift apart.
`hinted_label()` renders a button's shortcut in its own label, e.g.
"Reply (R)".

### Fixed
`ThreadModal`'s Reply/Reply All/Forward buttons had no keyboard equivalent
that actually worked while the modal was open — it's a `ModalScreen`, and
Textual truncates the app-level binding-chain walk at the modal boundary, so
the global `r`/`a`/`f` bindings never reached it (confirmed dead, not a
fragile coincidence as first assumed). `ThreadModal` now has its own
`BINDINGS` for `r`/`a`/`f`, and its buttons show the shortcut in their label.

### Not changed
`ComposeModal`'s Ctrl+Enter-to-send stays exactly as shipped in the
[2026-07-14] entry below — bindable in the registry but hidden from every UI
surface (button label, help bar, HelpModal), preserving that day's decision
not to advertise a shortcut most terminals don't transmit distinctly from
Enter. ASCII-fallback-glyph mode and narrow-terminal (80x25) responsive
layout are deliberately out of scope for this pass — see ROADMAP.md.

## [2026-07-14] — P0 live send smoke test passed; drop Ctrl+Enter hint

### Verified
Real live send confirmed working end-to-end: a genuine message sent to
bradbrownjr@outlook.com via the new Compose New (`c`) entry point, through
the 5-second cancelable countdown (`[2026-07-13]`), delivered successfully.
Closes the ROADMAP P0 item — done supervised, with a real
`~/.hermes/google_token.json`, as that item required.

### Changed
Dropped the "Ctrl+Enter to send" hint `Static` next to Compose's Send/Cancel
buttons — confirmed not firing in the user's terminal (most terminals don't
send a byte sequence distinct from plain Enter for Ctrl+Enter at all, absent
an enhanced keyboard protocol like Kitty's), so advertising it as a feature
was misleading, and it looked out of place besides. Left the `Ctrl+Enter`
handling itself in `ComposeModal.on_key` — harmless, and still works
wherever the terminal actually supports it — just no longer promised in the UI.

## [2026-07-14] — Move Compose New to the Email pane; Ctrl+Enter to send

### Changed
The blank "Compose New" entry point moved from `Button#contacts-compose-new`
(Contacts tab) to a new `c` key binding (`action_compose_new`) on the Email
pane — a no-prefill compose is Email's job, not Contacts'. No-ops outside
the Email pane. Per-contact "Compose Email" (prefills `to`) is unchanged,
still reachable from a contact's detail view.

### Added
`ComposeModal` now sends on `Ctrl+Enter` from anywhere in the form (shared
`_try_send()` helper backs both the Send button and the key), not just a
mouse click — a "Ctrl+Enter to send" hint sits next to the buttons. Note
some terminals don't distinguish Ctrl+Enter from plain Enter; the Send
button remains the reliable fallback there.

## [2026-07-14] — Log every crash, not just caught error toasts

### Added
`GoogleTUI._handle_exception()` now overrides Textual's `App._handle_
exception` — the single method every unhandled exception reaches before the
app tears down and exits, whether it came from a message handler or a
worker (`run_worker` defaults to `exit_on_error=True`, and most gauth calls
in this file run on one) — and logs the full traceback to LOG_FILE before
calling through to Textual's own handling. Previously a crash only ever
reached the terminal itself: gone the moment the pane closed, and (per this
session) a bare `google-tui | tee` pipe alone was enough to lose one
entirely with no trace and exit code 0. `on_mount` also logs a "starting"
line with the running version, so log sessions are demarcated. Documented
in AGENTS.md §5.

## [2026-07-14] — Fix duplicate startup toast, log errors to a file

### Fixed
Settings' two cache-limit `Select` widgets are constructed with `value=` set
to whatever's already saved, and Textual fires `Select.Changed` once on
mount even though nothing changed (confirmed with a standalone pilot test).
With no limit configured (the default), that fired `_prune_cache()` twice at
every startup, each popping a "No cache limits set — nothing to apply."
toast. `on_select_changed` now no-ops when the incoming value matches what's
already saved, which only the mount-time echo can do — a real user edit
always differs.

### Added
`GoogleTUI.notify()` now overrides `App.notify()` (every `Widget.notify()` —
including from ModalScreens — proxies through it, so one override catches
every call site) and appends `error`/`warning` severity notifications to
`~/.local/state/google-tui/log/google-tui.log` before showing the toast.
Toasts are ephemeral and easy to miss (doubly so when a bug fires the same
one twice, as above); this gives every error a durable record regardless.

## [2026-07-14] — Fix Ctrl+Left/Right tab cycling in Browser address bar

### Fixed
`#browser-url` (`main.py`) is a plain `Input`, and Textual's built-in
Ctrl+Left/Right word-jump bindings on `Input` shadowed the App-level
`cycle_tab_back`/`cycle_tab` bindings whenever the address bar had focus, so
tab cycling silently stopped working there. New `TabCyclingInput` subclass
redefines the same two keys (subclass `BINDINGS` for a given key override the
base class's, confirmed with a standalone Textual pilot test) to delegate to
the app's tab-cycle actions instead — Ctrl+Left/Right now always cycles tabs
regardless of which pane/input has focus. Verified via a mocked `run_test`
pilot (fake credentials/empty fetchers, zero live API calls): focusing
`#browser-url` and pressing Ctrl+Right/Ctrl+Left correctly moved `#main-tabs`
off `tab-browser`.

## [2026-07-14] — Per-commit versioning + cache size limits

### Added — the version bumps on every commit
`hooks/pre-commit` runs `scripts/bump_version.py`, which bumps the patch version
in `google_tui/__init__.py` (the source of truth) and keeps `pyproject.toml` in
lockstep, then stages both so the bump lands **in** the commit. That's what makes
the updater's "updated to vX.Y.Z" message mean anything — without it every
version was 0.1.0 forever.

Activate once per clone (this also enables the existing `post-merge` hook):

    git config core.hooksPath hooks

The hook no-ops during merge/rebase/cherry-pick (those replay commits that
already carry a version) and when nothing is staged. `bump_version.py` also
takes `--minor` / `--major` / `--show` by hand. `updater.describe()` now prefers
an *exact* tag on HEAD and otherwise reports `v{__version__} (sha)` — it no
longer falls back to a bare `git describe` like `v0.2.0-3-gabc1234`, which
contradicted the version the app reports about itself.

### Added — cache size accounting + Outlook-style limits
Settings → General now shows what the cache is actually costing you: total on
disk, item count, and a breakdown by what's using it (biggest first, with
friendly names — "Email (full messages)", "Drive (file contents)"), so someone
tight on space can see *what* to prune. Per-label thread-summary categories are
merged in the breakdown rather than listed three times.

Two opt-in limits, both defaulting to **no limit** (silently discarding
someone's offline data by default would be a rude surprise):

- **Keep cached data for** — Forever / 30 days / 90 days / 6 months / 1 year.
- **Limit cache size to** — No limit / 50 MB / 100 MB / 250 MB / 500 MB / 1 GB.
  Evicts least-recently-seen items until the cache fits.

Applied on launch and immediately whenever you change one, plus an "Apply limits
now" button. New `Cache.stats()` / `Cache.prune()` / `Cache.vacuum()`
(`google_tui/cache.py`).

Two things make eviction safe rather than lossy. First, **nothing here is
irreplaceable** — every row is a copy of something Google still has, and since
the historyId/modifiedTime revalidation went in, a pruned row is re-fetched
automatically the next time you open it. Pruning costs a little latency, never
data. Second, age is measured by `updated_at`, which is **rewritten every time a
row is re-seen** on a refresh — so it's a "last seen" stamp, not "first cached".
Mail still in your inbox and articles still in a feed keep getting touched and
never expire; only things that fell off the list, or a Drive file you haven't
opened in months, age out. `prune()` VACUUMs afterwards, because a SQLite DELETE
frees pages without shrinking the file — and reporting freed space while the
number on screen doesn't move is the one thing a user watching it won't forgive.

Off-menu values in a hand-edited `settings.json` (`"cache_max_mb": 42`) now snap
to the nearest offered option instead of crashing the Settings tab with
Textual's `InvalidSelectValueError`.

## [2026-07-14] — Cache revalidation + startup update check

### Changed — stop re-downloading data we already have
Startup was already cache-first (`_load_from_cache` paints from SQLite before
any network call), but three paths then re-pulled data that hadn't changed.
All three now **revalidate** against a change token instead of refetching:

- **Thread summaries** (`gauth.list_threads`). The `threads().list` response
  already carries each thread's `historyId`, and Gmail bumps it on any change
  to the thread. Cached rows now store it, and callers pass their cached rows
  in as `known=` — any listed thread whose historyId still matches is reused
  verbatim and never fetched. A refresh where nothing changed now costs **one
  API call** (the list itself) instead of re-pulling all 80 summaries. Verified:
  cold cache 80 fetches → unchanged refresh 0 fetches → 3 changed threads
  fetches exactly 3. Cache rows predating the field are treated as stale and
  refetched once, never blindly trusted.
- **Thread bodies** (`ThreadModal._fetch_thread`). `cache.py`'s docstring has
  always claimed to cache "thread bodies" — nothing ever did, so every reopen of
  the same email re-downloaded the entire thread. Bodies are now cached in a
  `thread_body` category, stamped with the thread's historyId and reused while
  it matches. Side benefit: an already-read thread is now readable offline.
  `mark_read` is only called when the thread is actually unread.
- **Drive previews** (`_drive_preview_fetch`). The `drive_file_meta` /
  `drive_file_text` caches were consulted **only when offline**, so the normal
  online path re-downloaded every file on every look. The folder listing already
  returns each file's `modifiedTime` (free, no extra call), which Drive bumps on
  every edit — so an unchanged file is now served from cache with no network at
  all, and the expensive part (the file body download) is skipped entirely.

### Added — startup update check (`google_tui/updater.py`)
The app is an editable checkout of its own git repo, so an update is a
fast-forward + re-exec, not a wheel download. Runs on the console before the TUI
starts, printing one line:

- `Downloading update... updated to v1.2.3`
- `No update found, loading application`
- `Can't reach update server, skipping update check.`
- (plus `Local changes present, skipping update check.` and `Local branch has
  diverged from origin, skipping update check.`)

Safety rules, all verified against throwaway repos: **never touches uncommitted
work** (a dirty tracked file skips the check outright — untracked cruft like
`__pycache__` does not, since a fast-forward can't clobber it); **fast-forward
only**, never a merge/rebase/reset, so a diverged branch is left for a human;
and **never blocks startup** — every git call is timeout-bounded (an unreachable
origin gives up in ~3s) and any failure degrades to a printed line and a normal
launch. On success it re-execs, because the running interpreter has already
imported the old modules and would otherwise report an update it isn't running.

Toggle in Settings → General, or `--no-update` / `GOOGLE_TUI_NO_UPDATE=1`.
`__version__` added to `google_tui/__init__.py`; `updater.describe()` prefers a
release tag and falls back to version + short sha.

## [2026-07-14] — UI responsiveness + getting the OAuth URL out of the app

### Fixed — the app was slow because blocking network calls ran on the event loop
An `async def` Textual worker does **not** get its own thread: it runs on the
event loop. Four code paths did blocking Google/LLM HTTP inside one, which
froze the whole UI — keystrokes, repaints, the lot — until the network replied.
All four now run with `thread=True`, fetching off-thread and applying widget
changes back on the main thread via `call_from_thread` (`google_tui/main.py`):

- `refresh_all` → **`_refresh_all_thread`**. Runs after sending mail and after
  toggling a task, and does a full Gmail + Calendar + Tasks fetch. This is the
  one that froze the UI for ~20 seconds at a stretch.
- `action_toggle_task` also called `gauth.set_task_status()` **inline** — a
  network write on the event loop, during a single keypress. Now threaded.
- `_hermes_worker` → **`_hermes_thread`**. Was blocking the UI on a Gmail +
  Calendar context fetch *and* the LLM round-trip; the app was unusable for the
  entire time the model was thinking.
- `_drive_preview` → **`_drive_preview_thread`**. The worst one: it fired on
  every Drive list *highlight change* — i.e. every arrow keypress — and each
  fire was a metadata round-trip **plus a full file download**, on the event
  loop. Holding Down through a folder downloaded one file per row.

### Changed — Gmail thread listing is ~10x fewer round-trips
`gauth.list_threads()` fetched each thread's metadata in its own sequential
HTTPS call: ~160 round-trips at `max_results=80`, previously measured at **~20
seconds** and documented in AGENTS.md as "normal, not a hang". It now issues
those `threads().get()` calls through Gmail's HTTP **batch** endpoint
(`new_batch_http_request()`, 50 sub-requests per call) — the same fetch is now
2 round-trips. Row order is preserved and a failing sub-request is skipped
rather than taking the whole inbox down (`google_tui/gauth.py`).

### Changed — debounced the two per-keystroke handlers
- **Drive preview** waits `_DRIVE_PREVIEW_DEBOUNCE` (0.25s) for the cursor to
  settle before fetching, so arrowing through 20 rows costs **one** preview
  instead of 20, and previews already fetched this session are served from
  `_drive_preview_cache` with no network call at all.
- **Contacts search** waits `_CONTACTS_SEARCH_DEBOUNCE` (0.15s) — each keystroke
  used to fuzzy-match the entire address book and rebuild every row.

### Changed — `ListView.extend()` instead of `append()` in a loop
`append()` mounts one widget per call (mount + layout + repaint each), so an
80-thread inbox paid for 80 separate mount cycles; the contacts list paid for a
full set on *every keystroke*. All list population (email, events, tasks, drive,
news, contacts) now batches into a single `extend()` mount.

### Added — three ways to get the OAuth URL out of the terminal
The authorization URL in the re-auth modal was effectively un-copyable: while a
TUI holds the mouse, the terminal can't draw its own selection, so you can't
just drag over the URL. `GoogleReauthModal` now offers:

- **Copy URL** button — copies via **OSC 52**, which is interpreted by the
  *terminal emulator*, so the URL lands on the clipboard of the machine you're
  sitting at even when the app runs on a headless box over SSH. Not universal
  (macOS Terminal ignores it; tmux needs `set -g set-clipboard on`), hence:
- **Save to file** button — writes the URL to `AUTH_URL_FILE`
  (`~/.cache/google-tui/auth_url.txt`) to `cat`/`scp`/open from another shell.
  Works no matter what the terminal supports.
- **F2 (global)** — `action_toggle_mouse` releases the mouse back to the
  terminal, restoring native click-drag selection **anywhere in the app**; F2
  again recaptures it. This is the general fix for "I can't select text in this
  app", not just for the OAuth URL. Documented in `HELP_TEXT` and the help bar.

## [2026-07-14]

### Added
- **In-app Google re-authorization.** New "Re-authorize Google account"
  button — in Settings → General, and in `OnboardingWizardModal` whenever
  Google auth is a diagnosed problem — replaces writing/running a one-off
  OAuth script (SETUP.md §7's old only option) for the two situations that
  used to require it: the routine 7-day token expiry on Testing-status
  Google Cloud apps, and adding a new scope to an existing token (e.g. the
  `contacts.readonly` scope P1 M5 needs). This app commonly runs on a
  headless VM or a display-less laptop, so it's deliberately NOT the usual
  `InstalledAppFlow.run_local_server()` (spawn a local server, auto-open a
  browser, wait for the redirect) — instead a manual copy-URL/paste-code
  flow in a new `GoogleReauthModal`: shows an authorization URL to open in
  any browser on any device, then accepts either the resulting (deliberately
  failed-to-load) redirect URL or just its bare code pasted back, exchanged
  via `gauth.complete_reauth`. New `gauth.build_reauth_flow`/
  `reauth_authorization_url`/`complete_reauth` reuse the OAuth client
  (`client_id`/`client_secret`/`token_uri`) already embedded in the existing
  token file — so re-authorizing never needs the downloaded
  client_secret.json again after the very first setup — with
  `access_type="offline"`/`prompt="consent"` forced so a RE-consent still
  returns a fresh `refresh_token`, which Google otherwise only issues on a
  true first-ever consent. On success, rebuilds `self.svc` and refreshes
  live data immediately — no restart required, unlike the encrypt-at-rest
  settings. Does NOT support a genuine first-ever setup (no token file at
  all yet); that still goes through SETUP.md's manual walkthrough once,
  since there's no existing OAuth client to reuse. New
  `google-auth-oauthlib` dependency. (`gauth.py`, `main.py`,
  `setup_instructions.py`, `SETUP.md`, `README.md`, `pyproject.toml`)
- **`scripts/generate_screenshot.py`**, extracted from the one-off script
  used to produce `assets/screenshot.png` (P1 M7 below) so the hero image
  can be regenerated later without re-deriving the approach — documented in
  AGENTS.md §6.
- **Repo hero screenshot (P1 M7, last of the P1 epics — see ROADMAP).**
  `assets/screenshot.png`, added to the top of `README.md`. Generated by
  driving the app through Textual's `run_test` pilot against an entirely
  fabricated dataset (fake threads/events/tasks/Drive files/contacts —
  zero real PII, zero live API calls: `gauth.get_credentials`/`services`
  and every `gauth.list_*`/`get_thread` call are mocked) — the same
  `save_screenshot` → `cairosvg` pipeline documented in AGENTS.md §6, run
  once and committed as a static asset rather than regenerated at build
  time. Captures the Mail tab (Email/Events/Tasks/Hermes panes) with the
  now-8-tab bar visible, including the new Contacts tab (P1 M5). With this,
  every P1 epic from the 2026-07-13 planning pass has shipped.
- **Rich HTML email rendering in `ThreadModal` (P1 M4).** Each message in a
  thread is now rendered through M1's shared `render.py`/`DocumentView`
  instead of the old plain-text-stripped `RichLog`: `gauth.get_thread` gains
  an additive `"html_body"` key per message (via a new `_extract_html_body`,
  a `text/html`-preferring sibling of `_extract_body`; the existing `"body"`
  plain-text key is untouched, so `ask.py`'s context-building is unaffected).
  `ThreadModal` mounts one (From/Date `Static` header + `DocumentView`) pair
  per message, stacked oldest-first in a `VerticalScroll`
  (`#thread-messages`); each message's body — HTML or plain — goes through
  `render.parse_feed_entry` (the same HTML-sniffing entry point News uses),
  so there's a single rendering path for both cases instead of two. Not
  merged into one Document per thread — that would require renumbering each
  message's `[N]` link markers to stay unique, and `ThreadModal`'s links
  aren't interactive today anyway (same as `NewsEntryModal`), so that
  complexity wasn't earning its keep for v1. (`gauth.py`, `main.py`)
- **Contacts tab + fuzzy Compose autocomplete (P1 M5).** New 8th tab
  (`Ctrl+8`, `tab-contacts`) backed by a new `gauth.list_contacts` (People
  API `people.connections.list` against `resourceName="people/me"`,
  paginated, returns `{resource_name, name, email, phone}` dicts) via a new
  `"people"` service in `gauth.services()`. Deliberately does NOT call
  `otherContacts.list` (Gmail-derived auto-contacts) — that needs a separate
  `contacts.other.readonly` scope not requested by this project. Requires
  the `contacts.readonly` scope, added to `SETUP.md` §7's scope list ahead
  of this change; against a token minted before that scope existed, the
  fetch raises and is caught with an actionable `notify(severity="error")`
  pointing at re-running the OAuth flow, rather than crashing the tab.
  Contacts are fetched lazily (first tab activation, not on every
  startup/`Ctrl+R` — they change far less often than mail/calendar/drive),
  cached offline in a new `Cache` category (`"contact"`, keyed by
  `resource_name`), and filterable live via `Input#contacts-search` +
  `rapidfuzz.fuzz.partial_ratio` against name/email (client-side, no
  re-fetch per keystroke). `Enter`/`Space` on a contact opens `ContactModal`
  (name/email/phone + a "Compose Email" button). `ComposeModal` gained a
  `mode == "new"` blank-compose path (`thread_id=None`, optional `to=`
  prefill) — sent via a new `gauth.send_message` call — reachable from a
  contact's "Compose Email" button or the Contacts tab's own "Compose New"
  button; this also delivers the standing "compose from scratch" wishlist
  item. `ComposeModal`'s `#c-to` field shows a live fuzzy-matched
  suggestion dropdown (`#c-to-suggestions`, up to 6 matches, matches only
  the fragment after the last comma so a partially-typed multi-recipient
  list still autocompletes correctly) sourced from `self.app._contacts_cache`
  client-side; no-ops silently if contacts were never fetched (e.g. missing
  scope) rather than erroring. New `rapidfuzz` dependency (`pyproject.toml`).
  (`gauth.py`, `main.py`, `cache.py`, `pyproject.toml`, `SETUP.md`)
- **Browser tab "new tab page" bookmarks row.** A row of four starter-
  destination buttons (`#browser-bookmarks`, a `Horizontal` right below
  `#browser-bar`, before `#browser-doc`) demonstrating the tab's multi-
  protocol nature — one shortcut per non-search protocol the tab already
  speaks: Google (`https://www.google.com`), Wikipedia
  (`https://en.wikipedia.org`), Gopherpedia (`gopher://gopher.floodgap.com`),
  and Gemini Protocol (`gemini://geminiprotocol.net/`) — a new module-level
  `_BROWSER_BOOKMARKS` list near `_classify_address` in `main.py`. This is a
  session-lifetime "new tab page" pattern, not a persistent bookmark bar:
  `self._browser_started: bool` (new `__init__` attribute) flips to `True`
  the first time `_browser_apply_document` runs on a genuinely successful
  page load, at which point `#browser-bookmarks` gets the existing `.hidden`
  CSS class and never reappears for the rest of the session (the
  Gemini-input-required/redirect-confirm intermediate branches deliberately
  don't trigger this — only a real successful apply does). Clicking a
  bookmark button (`on_button_pressed`'s new `event.button.id.startswith(
  "browser-bookmark-")` branch, looking the index up in
  `_BROWSER_BOOKMARKS`) sets `#browser-url`'s value and calls
  `_browser_navigate(url, push_history=True)` — the same path typed input
  already used through the `browser-go` button.
- **Navigation tab (`Ctrl+6`, P1 M6) — driving directions via the Google
  Routes API.** A 7th full-width tab (`TAB_ORDER` gains `"tab-navigation"`
  between `"tab-news"` and `"tab-settings"`; Settings shifts from `Ctrl+6`
  to `Ctrl+7`) with two free-text `Input`s (`#nav-origin`/`#nav-destination`)
  and a `Button#nav-go` — `Enter` in either input works too. Fetching is a
  new `fetchers.compute_route(origin, destination, api_key, units="IMPERIAL")`,
  which `POST`s to `https://routes.googleapis.com/directions/v2:
  computeRoutes` — unlike every other fetcher in this module (query-param
  API keys via `requests.get`), the Routes API needs a JSON POST body
  (`origin`/`destination` as free-text `{"address": ...}` objects — no
  Places API/geocoding needed on this app's end, the Routes API does that
  itself — `travelMode: "DRIVE"`, `routingPreference: "TRAFFIC_AWARE"`)
  plus mandatory `X-Goog-Api-Key`/`X-Goog-FieldMask` headers. Field mask
  (`ROUTES_FIELD_MASK`) requests route + per-step distance/duration plus
  `localizedValues` for both, so the response already carries ready-made
  human-readable strings like `"5.2 mi"`/`"12 mins"` instead of requiring
  client-side unit formatting. Returns a new `fetchers.RouteResult`
  dataclass (`distance_text`, `duration_text`, `duration_seconds` parsed
  from the `"772s"`-shaped `routes[0].duration` string, `steps:
  list[RouteStep]`) — deliberately a plain dataclass, not a
  `render.Document`, since there's nothing to hyperlink-navigate in a
  turn-by-turn step list. Steps are flattened across every leg
  (`for leg in route["legs"]: for step in leg["steps"]`) even though a
  single-waypoint route always yields exactly one leg today — defensive
  against future multi-stop support. `navigationInstruction.instructions`
  text is run through `render.decode_html_entities` defensively. Unlike
  `run_search`'s silent DuckDuckGo fallback, there's no fallback provider
  for driving directions, so every failure mode (missing key, HTTP 4xx/5xx,
  malformed JSON, zero routes returned) raises `fetchers.BrowserFetchError`
  with a user-facing message — reused from the Browser tab despite the
  name (its own docstring says "caught by main.py and shown via notify()",
  which is provider-agnostic) rather than inventing a parallel exception
  type. A 401/403 gets an extra hint pointing at the Routes API key setting
  and SETUP.md §6 (Cloud Billing must be linked — Routes API is paid Google
  Maps Platform, unlike the Workspace APIs the rest of this app uses).
  Fetch/apply split (`_nav_go` → `run_worker(thread=True, exclusive=True,
  group="nav-fetch")` → `_nav_fetch_thread` → `call_from_thread` to
  `_nav_apply_result`/`_nav_apply_error`) exactly mirrors the Browser tab's
  `_browser_navigate`/`_browser_fetch_thread`/`_browser_apply_document`.
  Results render into `Static#nav-summary` (route totals) and
  `RichLog#nav-log` (`markup=False`, numbered step list — same read-only
  sequential-text pattern as `ThreadModal`'s `#thread-body`, no per-row
  action needed). `Button#nav-export` writes the last-computed route
  (`self._nav_last_result: fetchers.RouteResult | None`, new `__init__`
  attribute) to a plain-text itinerary file via new module-level
  `_export_itinerary`/`_nav_export_filename`/`_slugify` helpers, at
  `platformdirs.user_documents_dir()/google-tui/route_<origin>_to_
  <destination>_<YYYYMMDD-HHMMSS>.txt` — runs synchronously on the main
  thread (no worker) since it's a small local write. New Settings sub-tab
  `TabPane#settings-tab-navigation` (5th sibling in `#settings-tabs`,
  appended to `SETTINGS_TAB_ORDER`): `Input#settings-routes-key`
  (password-masked) + `Button#settings-save-routes`, backing a new
  `Settings.routes_api_key: str | None` field (`settings.py`). No new
  Settings fields for units/language/travel-mode — those are hardcoded
  (`IMPERIAL`/`en-US`/`DRIVE`) as a deliberate v1 simplification; can
  become Settings fields later if needed. `HELP_TEXT` gains a NAVIGATION
  TAB section (between NEWS TAB and SETTINGS TAB) and its `Ctrl+1..6`
  line becomes `Ctrl+1..7`; `_context_help_text` gains a `tab-navigation`
  branch; `on_tabbed_content_tab_activated` focuses `#nav-origin` when the
  tab activates.

### Fixed
- **Browser tab Search mode was broken — `hermes web search` doesn't exist
  anymore.** Replaced the shell-out (`ask.google_search` → `hermes web
  search "<q>"`, which just prints argparse's top-level usage on the
  installed `hermes` CLI — documented as a known-broken P3 ROADMAP item
  since M2) with three real search backends implemented directly in
  `fetchers.py`, configurable in a new Settings sub-tab, defaulting to
  Google:
  - `search_google_cse(query, api_key, cse_id)` — Google Custom Search
    JSON API (`GET https://www.googleapis.com/customsearch/v1`), parsing
    `response.json()["items"]` (`title`/`link`/`snippet`). Needs an API key
    + a Programmable Search Engine ID ("cx") — SETUP.md gets a new §9
    walking through creating both, matching the existing Google Cloud
    Console walkthrough's style.
  - `search_duckduckgo(query)` — DuckDuckGo's non-JS HTML results page
    (`GET https://html.duckduckgo.com/html/`), needs a real browser-like
    `User-Agent` (confirmed empirically: DDG's HTML endpoint 403s this
    app's normal `DEFAULT_USER_AGENT`). Outbound links are wrapped in a
    DDG redirector (`//duckduckgo.com/l/?uddg=<url-encoded-target>&...`) —
    unwrapped via `urllib.parse.parse_qs`/`unquote` so numbered links go
    straight to the real target. No API key needed — this is the reliable
    no-config-needed baseline every other provider path falls back to.
    Regex/string extraction, not a real HTML parser (DDG's markup can
    drift); returns a valid empty-results `Document` rather than raising
    if nothing parses, matching this app's existing degrade-gracefully
    philosophy.
  - `search_searxng(query, base_url)` — a self-hosted/public SearXNG
    instance's JSON output (`GET {base_url}/search?format=json`), parsing
    `response.json()["results"]` (`title`/`url`/`content`). Some public
    instances disable `format=json`; on any exception this falls back
    *within the same function* to fetching the same URL without
    `format=json` and routing the HTML response through the existing
    `render.parse_html` (already used by `fetch_http`) instead of writing
    a second bespoke parser.
  - `_search_results_to_document(query, results)` — shared helper: turns a
    flat `list[tuple[title, url, snippet]]` into a `Document` with real
    numbered `render.Link`s, giving Search results the exact same `[N]` +
    digit + `Enter` navigation every other Browser mode (Gopher/Gemini
    menus, HTTP page links) already has — a genuine improvement over the
    old `_search_result_document()` (removed, along with the now-unused
    `_SEARCH_RESULT_URL_RE`), which only regex-linkified bare `https?://`
    tokens out of `hermes`'s opaque, unstructured stdout.
  - `run_search(query, settings)` — the dispatcher, with DuckDuckGo as the
    fallback for every path: `search_provider == "google"` tries
    `search_google_cse` only if BOTH `google_cse_api_key` and
    `google_cse_id` are set, and falls through to `search_duckduckgo` on
    ANY exception (or immediately, with no half-configured API attempt, if
    either key is missing); `search_provider == "searxng"` tries
    `search_searxng` only if `searxng_url` is set, same fallback-on-
    exception behavior; `search_provider == "duckduckgo"` calls
    `search_duckduckgo` directly. `main.py`'s `_browser_fetch_dispatch`
    search branch now just calls `fetchers.run_search(target, self.settings)`.
  - `ask.google_search` (the old shell-out) removed entirely — grepped the
    whole repo first to confirm its Browser-tab call site was the only
    caller (the Hermes Ask pane's `needs_agent`/`ask_hermes_agent`/
    `AIProvider` classes are a separate, untouched surface).
  - New Settings sub-tab, `TabPane#settings-tab-search` (4th sibling inside
    `TabbedContent#settings-tabs`, appended to `SETTINGS_TAB_ORDER` so
    `Alt+Left/Right` cycling picks it up): `RadioSet#settings-search-provider`
    (Google/DuckDuckGo/SearXNG) plus two conditionally-`.hidden` field
    groups (`#settings-google-group`: API key + Search Engine ID Inputs;
    `#settings-searxng-group`: instance URL Input) that show/hide both at
    compose time and live (`on_radio_set_changed`'s new
    `settings-search-provider` branch) — both groups can be hidden
    simultaneously when DuckDuckGo is selected. `Button#settings-save-search`
    writes the three new `Settings` fields (empty Input → `None`, matching
    the existing Nous-key-Save convention) and calls `save_settings`.
  - New `Settings` fields: `search_provider` (default `"google"`),
    `google_cse_api_key`, `google_cse_id`, `searxng_url` (all
    `str | None`, `settings.py`).
  - `HELP_TEXT`'s `SETTINGS TAB`/`BROWSER TAB` sections and
    `_context_help_text`'s `tab-settings` branch updated for the new
    sub-tab and the bookmarks row.

### Verification
Headless `run_test` + `pilot`, each Textual scenario its own process (per
AGENTS.md §6), all network mocked (`fetchers.search_google_cse`/
`search_duckduckgo`/`search_searxng`/`run_search`/`fetch_http`, plus
`gauth.get_credentials` mocked and `app.svc` short-circuited to `None` so
the background live-refresh thread's real Gmail/Calendar/Drive calls fail
fast inside their own try/excepts instead of touching the network) —
bookmarks row visible with 4 buttons on a fresh Browser visit, clicking one
navigates (mocked `fetch_http` invoked, `#browser-doc`'s document updates)
and the row hides afterward; typing free text into `#browser-url` and
submitting calls `fetchers.run_search` with the typed text and `app.settings`,
and the resulting Document's numbered `[N]` links are present and
activatable via digit + `Enter` (verified navigating to link `[1]`'s real
URL); the Settings Search sub-tab's Google/SearXNG field groups show/hide
correctly both at compose time (`Settings.search_provider` pre-set to each
of the 3 values across separate processes) and live (simulated `RadioSet`
click); saving search settings round-trips through `settings.json` via
`load_settings()`. Separately, `fetchers.run_search`'s fallback logic and
each backend's response parsing were unit-tested directly with NO Textual
involved (mocked `requests.get`) — google-unconfigured/raises-on-call,
searxng-unconfigured/raises-on-call, DuckDuckGo HTML parsing + redirect
unwrapping + graceful-empty-on-failure, Google CSE item parsing, SearXNG
JSON-then-HTML-fallback. `python -c "import google_tui.main"` compiles
cleanly. Confirmed the real `~/.config/google-tui`/`~/.cache/google-tui`
were untouched throughout (tests ran under isolated `XDG_CONFIG_HOME`/
`XDG_CACHE_HOME`). Scratch test scripts deleted when done (no committed
`tests/` dir, per AGENTS.md §6).

**Navigation tab verification:** `python -c "import google_tui.main"`
compiles cleanly. Headless `run_test` + `pilot` (mocked `gauth.services`/
`list_threads`/`list_events`/`list_tasklists`/`list_tasks`/`list_drive` so
the live-refresh worker fails fast instead of touching the network) —
`action_goto_tab_navigation` lands on `tab-navigation` and all its widgets
(`#nav-origin`, `#nav-destination`, `#nav-go`, `#nav-log`, `#nav-export`)
resolve; calling `_nav_go()` with no `routes_api_key` set notifies a
warning instead of crashing (no live API key was available in this
environment, so an actual `compute_route` call against the real Routes API
was NOT exercised — that's the one part of this feature that couldn't be
verified end-to-end here); the full 7-tab `Ctrl+Right` cycle
(`action_cycle_tab`) visits `tab-mail` → ... → `tab-navigation` →
`tab-settings` → back to `tab-mail`, matching `TAB_ORDER`; the 5-way
Settings sub-tab cycle (`_cycle_settings_tab`) visits `settings-tab-
navigation` last, matching `SETTINGS_TAB_ORDER`; typing into
`#settings-routes-key` and pressing `#settings-save-routes` round-trips
`Settings.routes_api_key` through `settings.json`. `main._export_itinerary`
was called directly against a hand-built `fetchers.RouteResult` and its
output file's content (route header, distance/duration, numbered steps)
matched the expected format. All of the above ran under isolated
`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_DATA_HOME` so the real
`~/.config/google-tui`/`~/.cache/google-tui` were untouched — except one
`_export_itinerary` smoke-test run that (correctly, per its design) used
the *real* `platformdirs.user_documents_dir()` since that path isn't
XDG-env-overridable; the resulting test file was deleted immediately
after inspection. Scratch test scripts deleted when done, no committed
`tests/` dir.

### Fixed (live-usage bug reports)
- **Space-expand on a multi-message thread only showed the latest message.**
  A "(5)" thread's Space-to-expand inline preview
  (`main._toggle_thread_expand`) only ever echoed the last message's Gmail
  `snippet` field — `gauth.list_threads` never fetched the other messages'
  content in the first place. Now, for any thread with `count > 1`, expand
  triggers a background fetch of the full thread (`gauth.get_thread`, the
  same call `ThreadModal`/Enter already used) and renders one line per
  message (From + a short body snippet) via new `_thread_expanded_text`,
  caching the result per thread in `self._thread_full_cache` so repeated
  collapse/expand doesn't re-fetch. Falls back to the old single-snippet
  text (plus a "press Enter for full thread" hint) if the background fetch
  fails.
- **Email pane defaulted to All Mail instead of Inbox.** `Settings.
  default_label_id` already defaults to `"INBOX"` in code, but an existing
  local `settings.json` written before that default existed (or after
  manually selecting All Mail once) permanently overrode it — nothing in
  `load_settings`/`save_settings` ever revisits an already-persisted value.
  No code change needed; fixed by resetting the affected `settings.json`. If
  you're still seeing All Mail on launch, switch the Email pane's label
  dropdown to Inbox once — it persists from then on.
- **Contacts tab before Settings in the tab order.** `TAB_ORDER`, the
  `compose()` `TabPane` block order (Contacts' block moved earlier, ahead of
  Settings'), the on-screen tab numbers (`_tab_label`), and the `Ctrl+7`/
  `Ctrl+8` bindings (now Contacts/Settings respectively, was Settings/
  Contacts) all updated together so the visual order and the keyboard
  shortcuts stay consistent.
- **Contacts pane filled the screen with "(no name)" rows when the Google
  token was missing or expired.** Root cause: `_load_from_cache` always
  re-renders whatever contacts happen to be on disk from a prior session
  regardless of whether the *current* token is valid, and the row renderer
  unconditionally printed the literal string `"(no name)"` for any entry
  without a display name (common for real Google "other contacts" too).
  Fixed two ways: (1) new `GoogleTUI._google_creds_ok()` (same check
  `_diagnose_setup` uses) gates the Contacts tab's lazy fetch, the Refresh
  button, and rendering itself — when the token is missing/invalid, the
  pane shows one explanatory row ("Not connected — Google token is missing
  or expired. Reconnect from Settings -> General to load contacts.")
  instead of stale/blank data, tracked via new `self._contacts_auth_broken`
  and cleared automatically on the next successful fetch or in-app re-auth;
  (2) `_apply_contacts_list_async` (and `ComposeModal._update_to_suggestions`'s
  To-field autocomplete) now skip contacts with neither name nor email, and
  show the email address instead of "(no name)" when only the email is
  present.
- **Sender addresses shown in the Email list by default.** New `Settings.
  show_sender_address` (default `False`) plus a "Show sender address in
  list" switch in Settings → General. Off (the new default): the list shows
  just the sender's display name, parsed from the raw `From` header via
  `email.utils.parseaddr` (falls back to the bare address if there's no
  name to parse). On: shows the original raw `"Name <addr>"` text, same as
  before this change. New module-level `_format_sender` backs both
  `_email_collapsed_line` and `_thread_expanded_text`; toggling the switch
  re-renders the currently-loaded list immediately, no restart needed.

**Verification:** `python -c "import ast; ast.parse(...)"` on `main.py`/
`settings.py`; three headless `run_test` pilot scripts (own process each,
per AGENTS.md §6) against a fabricated dataset with `gauth.get_credentials`/
`services`/every `list_*`/`get_thread` mocked — (1) a 5-message thread's
Space-expand showed all 5 fabricated messages' senders and body text; (2)
with `get_credentials` raising, `Ctrl+7` opened Contacts (confirming the new
tab order) and rendered exactly one row containing "reconnect"/"not
connected" and no "(no name)" text; `Ctrl+8` opened Settings and its new
switch defaulted to off; (3) a fabricated thread's collapsed line showed
name-only by default, then showed the full `Name <addr>` text immediately
after toggling the new switch live, no restart. One process hygiene note:
these scratch scripts wiped and reused the *real* `~/.config/google-tui`/
`~/.cache/google-tui` (mirroring `scripts/generate_screenshot.py`'s reset
step) instead of isolated `XDG_CONFIG_HOME`/`XDG_CACHE_HOME` like this
project's other verification runs — the live `settings.json` was restored
by hand afterward (`default_label_id: "INBOX"`, `show_sender_address:
false`), and the cache was left cleared (harmless; repopulates from Google
on next launch). Future scratch scripts here should isolate `XDG_*` like
the `[2026-07-13]` entries above did.

## [2026-07-13]

### Fixed (live-testing bug reports, second pass)
- **Header grew to 3 rows on click, shrank back on a second click.** This
  was Textual's OWN built-in `Header` behavior (`Header._on_click ->
  self.toggle_class("-tall")`, mapped to `height: 3` in `Header.
  DEFAULT_CSS`), not custom app code, and it wasn't wanted. Fix: a new
  module-level `GtHeader(Header)` in `main.py`, used in `compose()` in
  place of a bare `Header()`. The first attempt — a no-op override
  (`def _on_click(self): pass`) — looked right by inspection but does
  **NOT** actually suppress the base class's handler: confirmed via a live
  pilot click that `Header`'s own `_on_click` still ran and toggled the
  class anyway. Root cause: Textual's `MessagePump._get_dispatch_methods()`
  walks the full MRO and, for naming-convention handlers like `_on_click`
  (as opposed to `@on`-decorated ones), invokes the method from **every**
  class in the MRO that defines one — no dedup. The real fix is
  `event.prevent_default()`, whose docstring explicitly says "prevent
  handlers in any base classes from being called"; `_get_dispatch_methods`
  checks `message._no_default_action` at the top of each MRO-loop iteration
  and `break`s before reaching `Header`'s own handler. Documented as a new
  NOTE in AGENTS.md §2 (override-an-internal-handler gotcha) since it'll
  bite again on any future `_on_xxx` override. Verified with a headless
  `run_test` pilot click (had to pass an explicit `offset=` too — the
  default `pilot.click(widget)` offset landed on `HeaderIcon`, a child
  widget docked left that calls `event.stop()` in its own `on_click` and
  opens the command palette, so the very first version of this test
  "passed" without ever exercising `Header`'s own handler at all — also now
  a NOTE in AGENTS.md §6).
- **Settings tab restructured into sub-tabs, `Alt+Left/Right` switches
  between them.** Was one long `VerticalScroll` with encryption controls,
  AI-provider controls, and News-feed subscription management all crammed
  together. Now a nested `TabbedContent#settings-tabs` (mirrors the
  existing `TabbedContent#cal-tabs` Month/Week pattern on the Calendar
  tab) with three sub-tabs, each wrapped in its own independently-
  scrolling `VerticalScroll` rather than one outer scroll around the whole
  thing: `TabPane#settings-tab-general` (encrypt switch, key-method
  RadioSet, clear-cache button, cache-info Static), `TabPane#settings-tab-ai`
  (AI-provider RadioSet, Nous API key Input/Save), `TabPane#settings-tab-feeds`
  (News-feed subscription ListView/add/remove) — content relocated
  unchanged, not redesigned. New `SETTINGS_TAB_ORDER` constant (alongside
  the existing `TAB_ORDER`) and `_cycle_settings_tab(step)` helper (modeled
  on the existing `_cycle_tab`) back a new third branch in
  `action_switch_left`/`action_switch_right`: Mail tab still does pane
  adjacency, Browser tab still does history back/forward, Settings tab now
  cycles sub-tabs — a clean `elif` chain, regression-tested that the first
  two still work. `on_tabbed_content_tab_activated`'s existing guard
  (`if event.tabbed_content.id != "main-tabs": return`) already correctly
  no-ops for `#settings-tabs` sub-tab activation, the same way it already
  did for `#cal-tabs`; no new branch needed. There are now THREE
  `TabbedContent` widgets in the DOM (`#main-tabs`, `#cal-tabs`,
  `#settings-tabs`) — AGENTS.md's existing NOTE about always querying by ID
  (never a bare `self.query_one(TabbedContent)`) now covers all three. A
  4th sub-tab (`settings-tab-search`, Browser search providers) is a
  planned follow-up that can now drop in as a plain sibling `TabPane`.
- **`Select#email-label-select` (folder/label picker) had no keyboard
  shortcut.** New `l` binding (`action_focus_label_select`) focuses the
  Select and opens its dropdown — confirmed via a live pilot test that
  setting `.expanded = True` directly actually opens the overlay in this
  Textual version (it does; `Select._watch_expanded` shows/focuses the
  overlay). No-ops outside the Mail tab's Email pane. Verified `l` has no
  existing binding collision (`r`/`a`/`f`/`space` were already taken) and
  that it still types normally into a focused text `Input` (Textual gives
  the focused widget first crack at printable keys before bubbling to
  app-level `BINDINGS`).
- **Space on an email thread did the exact same thing as Enter, instead of
  the documented "expand" behavior.** `HELP_TEXT`, `_context_help_text`,
  README.md, and AGENTS.md all already claimed "Space = Expand" for the
  Email pane, but the code just pushed `ThreadModal`, identically to
  Enter — the documented behavior was never actually implemented. Fixed as
  a lightweight inline expand (deliberately NOT the full multi-message
  thread-tree UI — that's ROADMAP's separate, larger, not-yet-started P2
  "Threading depth" item, left untouched): `gauth.list_threads` now
  includes `"snippet"` in each thread dict (Gmail message resources carry
  a top-level `snippet` regardless of `format`, so this is free — no extra
  API call). New `self._threads_cache: dict[str, dict]` (threadId -> thread
  dict, populated everywhere `_apply_email_list`/`_apply_mail_data_async`
  populate the list) and `self._expanded_thread_ids: set[str]` back a new
  `_toggle_thread_expand(thread_id)` that mutates ONLY the one highlighted
  `ListItem`'s `Label` text in place (`self.query_one(f"#{_mk_id('t',
  thread_id)} Label", Label).update(...)`) — deliberately does NOT call
  `ListView.clear()`/repopulate, sidestepping the async-`clear()`-races
  trap documented elsewhere in AGENTS.md entirely rather than working
  around it. Collapsed text is the existing one-line format (factored out
  as a new `_email_collapsed_line(th)` helper, shared with
  `_append_email_items`); expanded text appends one line with the
  snippet (truncated to ~100 chars) and, if the thread has more than one
  message, a `(N messages)` note. `action_context_space`'s Email-pane
  branch now calls `_toggle_thread_expand` instead of pushing
  `ThreadModal`; Enter (`on_list_view_selected`) is unchanged and remains
  the only way to open the full thread modal.
- **Opening an email scrolled to the bottom instead of the top.**
  `ThreadModal`'s `#thread-body` is a `RichLog`, which defaults to
  `auto_scroll=True` (scrolls to the end on every `write()` call) — hence
  landing at the bottom on open. Fixed by passing `scroll_end=False` on
  all three `write()` calls in `ThreadModal` (`on_mount`'s "Loading…",
  `_apply_thread`, `_apply_error`). Message ORDER is unchanged
  (`gauth.get_thread` still returns oldest-first) — whether the most-recent
  message should show first instead is a separate, genuinely undecided
  design question the repo owner flagged themselves ("need to confirm"),
  left alone rather than guessed at; a fast, clearly-scoped follow-up if
  wanted.

Verified all five fixes above with headless `run_test` + `pilot` scenarios
(mocked `gauth.*`/`ask.*`, no real network, each scenario its own process
per AGENTS.md §6), including regression checks that Mail-tab pane
adjacency and Browser-tab history back/forward on `Alt+Left/Right` still
work after adding the Settings-sub-tab branch. Scratch test scripts
deleted after verification (this repo doesn't keep a committed `tests/`
dir).

### Fixed
- **`ThreadModal` crashed the whole app on any network error while opening a
  thread** (e.g. `SSLError: [SSL: RECORD_LAYER_FAILURE]` seen live while the
  background reconnect/live-refresh was still in flight on app startup).
  `on_mount` called the blocking `gauth.get_thread`/`gauth.mark_read`
  synchronously on the main thread with no `try`/`except` — the one modal in
  the app that fetches live data in `on_mount` instead of reading from an
  already-populated dict (`EventModal`/`TaskModal` don't hit the network at
  all), and the only gauth call anywhere without the fetch/apply thread split
  the rest of the app uses (see AGENTS.md §2's NOTE on the startup/refresh
  worker). Fixed by splitting into `_fetch_thread` (runs via
  `self.run_worker(..., thread=True)`) and `_apply_thread`/`_apply_error`
  (posted back via `self.app.call_from_thread(...)` — `call_from_thread`
  lives on `App`, not `Screen`, so a `ModalScreen` worker must reach through
  `self.app`). A failed fetch now shows the error inline in the modal body
  plus a `notify(severity="error")` instead of taking down the process;
  `mark_read` failures are swallowed (best-effort, not worth a second error).
  Verified via two headless `run_test` pilots (mocked `gauth.get_thread`
  raising and succeeding) — not a full offline-body-caching fix, that's
  still ROADMAP P4's separate "Cache email bodies for offline reading" item.

### Added
- **News tab — RSS/Atom reader + feed subscription management (P1 M3).**
  Adds a sixth full-width tab (`Ctrl+5`, `TAB_ORDER` = `[..., "tab-browser",
  "tab-news", "tab-settings"]`), pushing Settings from `Ctrl+5`→`Ctrl+6`
  (`_SUPERSCRIPT` gained a `6: "⁶"` entry; `BINDINGS`, `action_goto_tab_
  settings`/new `action_goto_tab_news`, `on_tabbed_content_tab_activated`,
  `_context_help_text`, and `HELP_TEXT` all renumbered/extended
  accordingly). New `fetchers.fetch_feed(url, timeout=15) -> list[dict]`
  (new `feedparser` dependency, added to `pyproject.toml` and installed
  into `.venv`) fetches a feed's bytes via `requests` (same
  timeout/User-Agent control as `fetch_http`, rather than handing
  feedparser a bare URL) and returns plain dicts — `id` (`entry.id` or
  `entry.link`, feedparser's own dedup key), `title`, `link`, `summary`
  (`content` if the feed provides full content, else `summary`),
  `published` (ISO-8601 UTC derived from feedparser's normalized
  `*_parsed` struct_time, so `main.py` can sort newest-first with a plain
  string compare instead of coping with real-world feeds' inconsistent
  raw date formats), `feed_title`/`feed_url` (provenance, for the combined
  multi-feed list and for locating a feed again to remove it) — matching
  `gauth.py`'s list-of-dict convention rather than a custom dataclass, so
  caching is a direct `Cache.put_many`. `render.py` stays fetch-agnostic
  per its M1 design; its pre-existing `parse_feed_entry(title,
  html_or_text, base_url)` (built for M3, unused until now) turns one
  entry's body into a `Document` for `render.DocumentView`, reused as-is.
  `Settings.feed_urls: list[str]` (new, `settings.py`) holds subscriptions
  in the same plaintext-JSON dataclass as everything else in Settings. The
  News tab itself is `ListView#news-list`, the same lightbar pattern as
  the Email pane, combining every subscribed feed's entries (like the
  Tasks pane combines all Google tasklists) sorted newest-first, each row
  `MM/DD  [Feed Title] Entry Title` (both truncated, same convention as
  `_append_email_items`). `Enter`/`Space` opens `NewsEntryModal` (new;
  modeled on `EventModal`/`TaskModal`'s shape — pushed WITHOUT a
  `push_screen` callback, unlike `ThreadModal`, since there's no
  follow-up action to relay back after Close), which parses the entry via
  `render.parse_feed_entry` into a `render.DocumentView`. Item ids use
  `_mk_id("n", entry["id"])`; because a feed entry's real id is very often
  a URL (lossy once `_mk_id` sanitizes it), lookup goes through a rebuilt
  `self._news_by_cid: dict[str, dict]` map rather than the `cid[2:]` slice
  the Email/Tasks/Events lists use. Fully wired into this app's
  cache-first/offline-capable data flow like every other source: a new
  `Cache` category `"feed_entry"` (keyed by entry id); `_fetch_news_data`/
  `_write_news_cache`/`_apply_news_data` follow the same fetch/apply split
  and `run_worker(..., exclusive=True, group=...)` + generation-counter
  (`_news_apply_gen`) pattern as `_apply_mail_data_async`/
  `_apply_drive_files_async` (this list can be repopulated more than once
  per session — cache load, live refresh, AND every feed add/remove in
  Settings — so a bare `ListView.clear()` + deferred populate would hit
  the same `DuplicateIds` race documented in AGENTS.md); wired into both
  `_load_from_cache` (cache-first startup) and `_live_refresh_thread`
  (startup live sync + `Ctrl+R`, which therefore refreshes News for free).
  Each subscribed feed is fetched in its own try/except inside
  `_fetch_news_data`, so one dead feed URL doesn't take the others down —
  but, a deliberate design call not explicitly spelled out in the
  originating brief: feed failures do **not** flip `self._online`/the
  Synced-Offline header the way a Gmail/Calendar/Drive failure does,
  because that flag is specifically about *Google* reachability
  (AGENTS.md §1a) and feed URLs are unrelated third-party sites — a dead
  RSS feed now just gets its own error `notify()`, not a false "Offline"
  banner. Settings tab gained a feed-subscription manager
  (`ListView#settings-feed-list` + `Input#settings-feed-url` +
  `Button#settings-add-feed` + `Button#settings-remove-feed`, CSS reusing
  `.settings-row`): adding a URL validates/dedupes, saves, appends a row,
  and kicks a one-off background fetch (`_fetch_and_merge_one_feed`,
  `thread=True`, group `"news-fetch-one"`) so that feed isn't empty in the
  News tab until the next full refresh; removing drops the URL from
  `Settings.feed_urls`, re-renders `#news-list` with that feed's cached
  entries filtered out, and removes the row directly (`ListItem.remove()`)
  rather than a full list clear+repopulate. New module-level helper
  `_feed_list_item(url)` builds each Settings row and stashes the raw URL
  as a plain `.feed_url` attribute on the `ListItem` (needed because
  `_mk_id` can't be reversed for a URL-shaped id).
  **Found and fixed along the way**: feed titles/entry titles are
  untrusted external text, and the News-list row format literally wraps
  the feed title in `[...]` — exactly the syntax Textual's
  `Content.from_markup()` (what `Label`/`Static` route through by
  default) uses for style tags, which silently swallowed
  `"[Feed Title]"` during testing instead of displaying it. Confirmed
  `rich.markup.escape()` does **not** reliably fix this (its tag-detection
  regex didn't even touch a bracketed phrase containing a space, and
  `Content.from_markup()` still ate it downstream) — the correct fix is
  constructing the affected `Label`/`Static` widgets with `markup=False`
  (a one-time constructor flag honored by every later `.update()` call),
  used for `#news-entry-meta`, the News-list rows, and Settings'
  feed-URL rows. Verified with a headless Textual `run_test` pilot (own
  process, per AGENTS.md §6): mocked `fetchers.fetch_feed` with two
  synthetic feeds; News tab reachable via `action_goto_tab_news`;
  `#news-list` populates newest-first with correctly-escaped
  `[Feed Title]` rows; `Enter` opens `NewsEntryModal` with rendered
  content; Settings shows the 2 existing subscriptions; adding a feed
  persists to `settings.json` (`load_settings()` round-trip), shows up in
  both `#settings-feed-list` and (after the background one-off fetch)
  `#news-list`; removing a feed persists too and shrinks both lists. Also
  a plain `python -c "import google_tui.main"` compile/import smoke check.
  (`google_tui/fetchers.py`, `google_tui/settings.py`, `google_tui/main.py`,
  `pyproject.toml`, `AGENTS.md`, `README.md`, `ROADMAP.md` changed.)
- **Browser tab — Web/Gopher/Gemini/Search (P1 M2).** Replaces the
  standalone Search tab in place at `Ctrl+4` (`TAB_ORDER` and
  `BINDINGS`/`action_goto_tab_browser` updated accordingly; Settings stays
  at `Ctrl+5`), consuming M1's `render.py`/`DocumentView` directly — an
  address bar (`Input#browser-url`), a mode badge
  (`Static#browser-mode`: WEB/GOPHER/GEMINI/SEARCH — the only visual cue
  for Search mode, since bare query text has no scheme prefix to show it),
  and `DocumentView#browser-doc` for the rendered page. New
  `google_tui/fetchers.py` holds all the actual network I/O (`render.py`
  itself stays fetch-agnostic, per its M1 design): `fetch_http` (routes
  `text/html` through `render.parse_html`, other `text/*` through plain
  paragraph blocks, anything else raises `BrowserFetchError`); `fetch_gopher`
  (no existing client to port — raw `socket` I/O, `render.parse_gopher_url`
  to re-derive host/port/item-type/selector, `render.parse_gopher_menu` for
  type `1`, plain paragraphs for type `0`, a clear error for the `URL:`
  web-link selector convention and for any other item type); `fetch_gemini`
  (implemented from spec — TLS via `ssl.SSLContext(PROTOCOL_TLS_CLIENT)`
  with `CERT_NONE`/`check_hostname=False`, deliberately NOT
  `create_default_context()`, since self-signed certs are the Gemini norm;
  SHA-256 cert-fingerprint TOFU pinning via a new `GeminiTofuStore`
  wrapping `Cache`'s new `"gemini_cert"` category, keyed
  `f"{host}:{port}"`, checked *before* reading the response body; full
  1x/2x/3x/4x/5x/6x status dispatch — 1x raises `GeminiInputRequired`, 3x
  auto-follows same-host/scheme redirects up to 5 hops or raises
  `GeminiRedirectConfirm` otherwise, 6x is a "not supported yet" stub).
  Address-bar submission is classified by a new `_classify_address()`
  helper in `main.py` (omnibox heuristic: explicit
  `http(s)/gopher/gemini://` wins, a single dotted-word-with-no-space gets
  `https://` prepended, everything else — including any text containing a
  space — is a web search via the existing `ask.google_search`, with
  `search:` as an explicit escape hatch). Search results are rendered as a
  real linkified `Document` (`_search_result_document()`, regex-extracts
  every `https?://…` token from `hermes web search`'s opaque stdout into a
  numbered `Link`) rather than dumped into a `RichLog`, so numbered-link
  nav (matching bpq-apps' UX) works uniformly across all four Browser
  modes — this was the one real design call in M2, since the CLI's output
  format isn't structured/guaranteed. History is a session-lifetime-only
  in-memory `list[BrowserHistoryEntry]` (already-fetched `Document`s plus
  scroll position, not just URLs — Back/Forward never re-fetches, works
  fully offline); `Alt+Left/Right` are Back/Forward when the Browser tab is
  active (reusing `action_switch_left/right`, which already no-op outside
  Mail-tab-adjacency elsewhere), `Tab`/`Shift+Tab` toggle focus between the
  address bar and the page. Two new modals — `GeminiInputModal` (status
  10/11 prompts, masked input for "sensitive") and a reusable `ConfirmModal`
  (Gemini cross-host/cross-scheme redirect confirmation) — both funnel
  their result back through `_browser_navigate()`, deferred via
  `call_after_refresh` per the documented push_screen-callback-timing
  gotcha (AGENTS.md §2). The Browser tab is never gated by
  `self._require_online()` (that flag tracks Google reachability
  specifically, not arbitrary web/gopher/gemini fetches), and there's no
  SQLite cache category for page content itself — only the `gemini_cert`
  TOFU store persists; that's a deliberate v1 non-goal, tracked in
  ROADMAP. `HELP_TEXT`'s `SEARCH TAB` section and `_context_help_text()`'s
  `tab-search` branch became `BROWSER TAB`/`tab-browser`. Verified with
  headless Textual `run_test` pilots (mocked `fetchers.fetch_http`/
  `fetch_gemini` and `ask.google_search`, one scenario per process per
  AGENTS.md §6): Browser tab shows at `Ctrl+4`; address-bar submit renders
  a mocked `Document`; activating a numbered link navigates and grows
  history; `Alt+Left/Right` restore prior pages with zero additional
  fetches; bare-text input reaches `ask.google_search` and renders a
  linkified result Document; the Gemini status-10 input-required modal
  round-trip (push, submit, resume navigation) works end to end. Also unit-
  tested `fetchers.py`'s HTTP/Gopher/Gemini parsing/dispatch logic directly
  against synthetic fixture data (mocked `requests.get`/`socket.
  create_connection`/`ssl.SSLContext.wrap_socket`, zero real sockets) —
  covers content-type routing, gopher item-type dispatch and the `URL:`
  selector error, and the full Gemini status-code matrix including TOFU
  pin-then-mismatch and same-host-autofollow-vs-cross-host-confirm
  redirect branching. Found along the way: the `hermes web search`
  subcommand this feature (and the old Search tab) depends on no longer
  exists in the installed `hermes` CLI in this environment — tracked as a
  new ROADMAP P3 item rather than fixed here (out of scope for `ask.py`,
  which M2 deliberately left untouched); the Browser tab's Search mode
  degrades gracefully (an empty-link Document) rather than crashing when
  this happens. (`google_tui/fetchers.py` new; `google_tui/main.py`,
  `AGENTS.md`, `ROADMAP.md`)
- **`google_tui/render.py` — shared HTML/Gopher/Gemtext rendering module
  (P1 M1).** Protocol-agnostic `Document`/`Block`/`Link` model plus a
  Textual `DocumentView` widget, meant to be consumed by the future
  Browser (M2), News (M3), and rich-HTML-email (M4) epics instead of each
  rolling its own parser. Ports `bpq-apps/apps/htmlview.py`'s nav-vs-content
  link separation heuristic and `apps/gopher.py`'s tab-delimited menu
  parser (both packet-BBS apps whose `print()`/`input()` I/O and
  `__EXIT__`/`__MAIN__` sentinel-string control flow were left behind in
  favor of a real `LinkActivated` Textual message), and adds a from-spec
  Gemtext parser (no existing client to port). Fixes made during the port:
  entity decoding now keeps real Unicode instead of stripping to ASCII, a
  hardcoded-domain nav heuristic became a same-site `urlparse` check, and
  `<pre>`/`<code>` preformatted-block handling was added (didn't exist in
  the source). Not wired into `main.py`/any tab yet — that's M2/M3/M4's
  job. Design docs and audit only exist in the session that built this;
  the code and its docstrings are the reference going forward.
- **`SETUP.md` — Google Cloud Console walkthrough (P1 feature epic).**
  Step-by-step guide: create a project, enable Gmail/Calendar/Drive/Tasks/
  People/Routes APIs, configure the OAuth branding (Google rebranded the
  old "OAuth consent screen" into **Google Auth Platform** — Branding/
  Audience/Clients tabs, confirmed live via search since this UI moves),
  add yourself as a test user (flags the real caveat: Testing-mode tokens
  expire every 7 days unless the app is published/verified), create a
  Desktop-app OAuth client, and run the local auth flow. Recommends
  **People API** for the future Contacts tab and **Routes API** for the
  future Navigation tab — noting Routes is the maintained replacement for
  the now-deprecated Directions API, and that Maps Platform is the first
  API in this project requiring **Cloud Billing** (Workspace APIs are
  free). README updated to reflect labels/folders, multi-provider AI, the
  send countdown, and the onboarding wizard, and now links to `SETUP.md`.
- **Multi-provider AI + onboarding wizard (P1 feature epic).** The Ask pane
  is no longer locked into Hermes. `google_tui/ask.py` gets an `AIProvider`
  abstraction — `HermesProvider` (existing Nous LLM + `hermes` CLI agent),
  `ClaudeCodeProvider` (`claude -p --output-format text`), `OpenCodeProvider`
  (`opencode run`), `GeminiCLIProvider` (`gemini -p`) — all picked from a
  new "AI provider" radio group in the Settings tab and persisted to
  `settings.ai_provider`. Every provider gets the same Google context
  (recent email/events, built locally via `gauth`) handed to it as part of
  the prompt — that's how the Google token is "shared" with each provider,
  without needing separate Google integrations per CLI. Settings also
  gained a Nous API key field (`settings-nous-key`), so Hermes no longer
  requires hand-editing `~/.hermes/config.yaml`; the Settings tab container
  changed from `Container` to `VerticalScroll` since it no longer fits one
  screen. New `google_tui/setup_instructions.py` holds the shared
  Google-account and AI-provider setup text, reused by both the wizard and
  (later) `SETUP.md`. On launch, `GoogleTUI._diagnose_setup()` checks for a
  valid Google token and at least one reachable AI provider; if either is
  missing, an `OnboardingWizardModal` shows the relevant instructions
  before the normal tabs, with "Retry" (re-diagnose) and "Continue anyway"
  (proceed in the existing degraded/offline mode) options. Verified with
  mocked `run_test` pilots: wizard shows/hides correctly based on
  diagnosis, Continue anyway proceeds to normal startup, provider radio
  switch and Nous key save both persist to settings. (`google_tui/ask.py`,
  `google_tui/main.py`, `google_tui/settings.py`,
  `google_tui/setup_instructions.py`)
- **Labels as folders (P1 feature epic).** A `Select` dropdown
  above the Email pane (`#email-label-select`) lets you switch between
  Gmail labels/folders — "All Mail" plus every system and user label
  (nested user labels like `Family/Kids` shown indented by depth).
  `gauth.list_labels` (`users.labels.list`) and `gauth.list_threads(...,
  label_ids=...)` (`threads.list(labelIds=...)`) back it. Picking a label
  persists to `settings.default_label_id`, shows the cached threads for
  that label instantly (new label-scoped cache category
  `thread_summary:<label_id>`, replacing the old flat `thread_summary`
  category), and kicks a background refetch if online. Defaults to
  `INBOX` — previously the Email pane had no label filter at all (closer
  to All Mail than an inbox). Verified with a mocked `run_test` pilot:
  initial load shows Inbox-only threads, switching to a nested user label
  re-fetches and re-caches correctly. (`google_tui/gauth.py`,
  `google_tui/main.py`, `google_tui/settings.py`)
- **Send confirmation via a 5-second cancelable countdown.**
  `ComposeModal` no longer fires `gauth.reply_to`/`forward` the instant
  Send is clicked. Clicking Send disables the To/Subject/body fields and
  the Send button and shows a "Sending in N…" countdown
  (`ComposeModal.SEND_COUNTDOWN_SECONDS = 5`); the actual send only
  happens once it reaches zero. Cancel or `Esc` at any point during the
  countdown aborts it and re-enables the form instead of sending.
  (`google_tui/main.py`)

### Fixed
- **Dead `on_dismiss` handler.** `ModalScreen.Dismissed` doesn't exist in
  the installed Textual version, so `GoogleTUI.on_dismiss` was never
  invoked — `ThreadModal`'s Reply/Reply All/Forward buttons, and the
  direct `r`/`a`/`f` keybindings, silently did nothing (no `ComposeModal`
  opened, no refresh after send). Replaced with explicit
  `push_screen(..., callback)` pairs (`_on_thread_modal_result` →
  `_open_compose_from_thread` → `_on_compose_result`), deferred one step
  via `call_after_refresh` per the push_screen-callback-timing note in
  AGENTS.md §2. Verified with a scripted `run_test` pilot (mocked
  `gauth`): Reply from `ThreadModal` now reliably opens `ComposeModal`,
  and a completed send now triggers `refresh_all`.

## [Unreleased]

### Added
- **Local cache + offline mode.** `google_tui/cache.py` (SQLite,
  `cache_items(category, key, payload, updated_at)`) persists thread
  summaries, events, tasks, calendar month/week data, and Drive listings/
  metadata/text. Startup is now cache-first: cached data populates the UI
  instantly, then a background thread reconnects to Google
  (`Header.sub_title`: `Connecting…` → `Synced HH:MM` or `Offline (cached
  HH:MM)`). `LoadingModal` only appears on a genuine first run with an
  empty cache. Reply/Reply All/Forward/toggle-task are disabled with a
  warning notify while offline; Drive preview falls back to cached
  metadata/text for files viewed at least once online. No offline mutation
  queue — this is read-only browsing of cached data, not a sync engine
  (tracked as a P2 follow-up in ROADMAP).
- **Settings tab** (`Ctrl+5`, `google_tui/settings.py` +
  `google_tui/cache.py`): encrypt-at-rest toggle for the local cache, off
  by default. Two key methods, both offered as a choice: a random local
  key file (`~/.config/google-tui/cache.key`, chmod 600, no prompt) or a
  passphrase typed at launch (scrypt-derived key, verified against a
  stored canary, never written to disk itself). Small "browse" cache rows
  (thread/event/task summaries) are bulk-decrypted on every list
  population; large "content" rows (Drive file text) are decrypted one at
  a time, only when actually opened — encryption overhead scales with what
  you look at, not with total cache size. Turning encryption on/off, or
  switching key method, clears the cache immediately and asks for a
  restart (no re-encryption/migration).
- `LoadingModal`, shown the instant the app mounts, before any Google API
  call — the initial fetch (mail + calendar + drive) reliably takes
  10-20+ seconds. Runs on a background worker THREAD (not just an asyncio
  worker) so the event loop stays free to actually paint the modal instead
  of freezing the terminal with nothing on screen.
- `Ctrl+Left/Right` to cycle tabs — a reliable fallback for `Ctrl+1..4`,
  which most terminals (and every major browser, for its own tab-switching)
  don't transmit at all.
- `AGENTS.md`, `ROADMAP.md`, `CHANGELOG.md` for cold-start continuation.
- `google-tui` launcher at `/home/bradb/.local/bin/google-tui` (on PATH)
  that bakes in venv activation — runs from any shell without sourcing `.venv`.
- **Tab/pane redesign.** Four full-width tabs (Mail / Calendar / Drive /
  Search) now live in the blue bar (`Ctrl+1..4`), styled as the outer
  `TabbedContent`'s own tab strip. The Mail tab holds the four panes (Email /
  Events / Tasks / Hermes, `Alt+1..4` or adjacency-based `Alt+arrows`) that
  used to be the whole app. Tab and pane numbers are shown dimmed at all
  times (not hidden-until-modifier-held — Textual has no key-release event
  to detect that; see AGENTS.md §2).
- **Calendar tab.** Full month grid with events listed inside each day's
  square (`+N more` opens `DayEventsModal` with the full list) and a new
  hour-grid week view (24 hour rows x 7 day columns), modeling Google
  Calendar's web UI. `[`/`]` page the month or week.
- **Drive tab.** Folder browser with a live preview pane: metadata (owner,
  type, path, created/modified — `gauth.get_file_metadata`) always shown,
  plus a text preview for non-binary/non-image files, updating as the cursor
  moves rather than requiring Enter.
- **Search tab**, inline instead of a modal.
- Two-row help bar: a contextual row (current tab/pane's shortcuts) above a
  static global-shortcuts row, both wrapping instead of truncating on a
  narrow terminal. `HelpModal` (`Ctrl+H`) has the full reference.
- `Ctrl+Q` quit (was bare `q`); `Ctrl+P` is Textual's own command palette.

### Fixed
- Tab bar collapsed from 2 rows to 1 (the second row was Textual's own
  `Tabs` underline indicator, made redundant once tabs got a real
  active-state background) and inactive tab labels are now fully legible
  (explicit `color: $text` instead of Textual's default 50%-dim, which read
  poorly against the blue bar).
- Widened the Email pane (`#left` 45% → 65%; right column panes had a lot of
  dead whitespace) and removed a doubled border on `#left` that was nesting
  Email's own pane border one level deeper than Events/Tasks/Hermes,
  visually offsetting it by a line.
- `gauth.read_drive_text` called `files().get(file_id=...)` — the Google
  Drive API v3 parameter is `fileId` (camelCase); the wrong name crashed
  every Drive text preview with "unexpected keyword argument". Only
  affected files (folders always worked, since they never called this path).
- `ListView.clear()` returns an unsynchronized `AwaitRemove` — now that mail
  and Drive data can be applied twice per session (once from cache, once
  from the live refresh), a fire-and-forget clear + deferred repopulate
  intermittently raised `DuplicateIds`. Fixed by properly `await`ing the
  clear inside an exclusive worker, plus a generation counter to drop any
  populate that got superseded mid-flight.

### Changed
- `_mk_id` helper moved to MODULE level (was a class method) so `DriveModal`
  can use it; fixed a latent bug where naming a method `_id` collided with
  Textual's internal `DOMNode._id` (caused `'NoneType' not callable` at
  `refresh_all`). (main.py)
- Mail-tab "Calendar" pane renamed to "Events" to stop clashing with the new
  Calendar tab; `PANE_ADJACENCY` (an explicit map) replaces the old
  `active ± 1/2` arithmetic, which assumed a 2x2 grid that no longer matches
  the layout (Email spans the full left column against a 3-row right column).
- `refresh_all` now clears each list before repopulating — `Ctrl+R` used to
  duplicate every row instead of replacing them.
- `gauth.month_events` refactored to share a new `events_between(svc, start,
  end)` helper instead of duplicating the `events.list` call shape.
- Every gauth-touching method on `GoogleTUI` split into a `_fetch_*` (pure
  data) / `_apply_*` (widget mutation) pair, so the initial load can run the
  fetches on a background thread while still safely applying results back on
  the main thread via `call_from_thread`.

### Removed
- `CalendarModal`, `DriveModal`, `DriveFileModal`, `SearchModal` — their
  content is now inline in the Calendar/Drive/Search tabs instead of behind
  a keypress-triggered modal.
- Bare `1`-`4`, `q`, `c`, `d`, `s` bindings — superseded by `Ctrl+1..4` /
  `Alt+1..4` (tabs vs. panes no longer share the same number keys) and
  `Ctrl+Q`; `c`/`d`/`s` opened modals that no longer exist.

## [2026-07-12] — Initial build

### Added
- Multi-pane Textual TUI at `/home/bradb/google-tui` (Python package
  `google_tui`).
- Google auth wrapper (`gauth.py`) using `~/.hermes/google_token.json`
  directly (the bundled `google-workspace` skill's CLI lacks Tasks + Drive
  list, so we hit the APIs directly).
- Email pane: threaded Gmail list (80 threads), lightbar, `Enter` thread
  view, `r`/`a`/`f` reply/reply-all/forward compose modal.
- Calendar pane: upcoming events (3 weeks), lightbar + detail dialog, full
  month + week view modal (`CalendarModal`).
- Tasks pane: all task lists combined, lightbar, `Space` toggle complete
  (live), `Enter` detail/subtasks view.
- Hermes Ask pane: general questions → Nous LLM (`tencent/hy3:free`) with live
  Google context; action questions → full Hermes agent (shells `hermes`).
- Drive button: folder browse, plaintext read (Docs→txt, Sheets→csv),
  binary download.
- Search button: text search via searxng (`hermes web search`).
- Pane switching: Alt+Left/Right/Up/Down, Tab/Shift+Tab, 1-4, Ctrl+R refresh.
- Verified against live Google data via Textual `run_test` pilot (email/
  calendar/tasks/drive/search/thread modals all open; task toggle + compose
  open confirmed via real key presses).

### Known limitations (see ROADMAP)
- No send confirmation (compose sends immediately).
- Email/events/drive capped (80 threads / 3 weeks / one folder page).
- Calendar week view is a simple day-column list, not a time-grid.
- Live email send not exercised end-to-end (would actually send mail).
