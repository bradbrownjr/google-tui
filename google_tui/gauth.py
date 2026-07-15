"""Google auth + read/write helpers built directly on the existing Hermes token.

The Hermes google-workspace skill's google_api.py does NOT implement Tasks and
Drive only exposes search/get. We build Credentials straight from
~/.hermes/google_token.json (it has a refresh_token + the tasks scope) so we get
native Gmail threads, Calendar, Tasks, and Drive listing.
"""
from __future__ import annotations
import base64
import json
import os
import email as email_lib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_PATH = os.path.expanduser("~/.hermes/google_token.json")

_scopes_cache: dict | None = None


def get_credentials() -> Credentials:
    tok = json.load(open(TOKEN_PATH))
    creds = Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        client_id=tok.get("client_id"),
        client_secret=tok.get("client_secret"),
        token_uri=tok.get("token_uri"),
        scopes=tok.get("scopes"),
    )
    if not creds.valid:
        creds.refresh(Request())
    return creds


def services() -> dict:
    creds = get_credentials()
    return {
        "gmail": build("gmail", "v1", credentials=creds, cache_discovery=False),
        "calendar": build("calendar", "v3", credentials=creds, cache_discovery=False),
        "drive": build("drive", "v3", credentials=creds, cache_discovery=False),
        "tasks": build("tasks", "v1", credentials=creds, cache_discovery=False),
        "people": build("people", "v1", credentials=creds, cache_discovery=False),
    }


# Keep in sync with SETUP.md §7's scope list — that file documents the same
# set for the manual first-time flow that mints TOKEN_PATH in the first
# place (reauthorize() below can't run at all until that's happened once).
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
]


# This app commonly runs on a headless VM or an underpowered laptop with no
# X11/Wayland compositor (see AGENTS.md) — InstalledAppFlow.run_local_server()
# (spawn a local HTTP server, auto-open a browser, wait for the redirect to
# hit it) is the WRONG tool here: there's often no browser to open, and even
# if the user opens the auth URL on a different device (their phone), that
# device's browser can never reach a server listening on the headless
# machine's localhost. build_reauth_flow/reauth_authorization_url/
# complete_reauth below implement the manual alternative instead: show a
# URL to open ANYWHERE, then accept whatever the browser lands on (or just
# the bare code) pasted back into the TUI. See GoogleReauthModal in main.py
# for the interactive side of this.
#
# redirect_uri="http://localhost" is never actually connected to — it's
# purely a placeholder Google's "Desktop app" OAuth client type accepts
# without pre-registration (RFC 8252 loopback exception). Because it's
# "http" not "https", oauthlib refuses to parse a pasted redirect URL
# unless OAUTHLIB_INSECURE_TRANSPORT is set — safe here since the loopback
# leg is never actually used for anything sensitive (nothing listens on
# it), only the exchange with Google's real (https) token endpoint is.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def build_reauth_flow(scopes: list[str] | None = None):
    """Builds (but does not run) a re-authorization flow — step 1 of 3, see
    reauth_authorization_url/complete_reauth below.

    Reuses the OAuth CLIENT (client_id/client_secret/token_uri) already
    embedded in the EXISTING token file, so the user never has to re-find or
    re-paste their downloaded client_secret.json for a re-auth — only a
    genuinely first-ever setup (no TOKEN_PATH yet) still needs that, since
    there is no existing client to reuse. That first-time case still goes
    through the manual SETUP.md walkthrough; this function deliberately
    doesn't try to replace it.

    Fast/local (file read + object construction, no network) — safe to call
    from the main thread, unlike complete_reauth below.
    """
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"No existing token at {TOKEN_PATH} — this looks like a first-time "
            "setup, which still needs the manual OAuth client walkthrough in "
            "SETUP.md (§1-7). Re-authorizing an EXISTING token never needs "
            "that again after this first time."
        )
    tok = json.load(open(TOKEN_PATH))
    client_id = tok.get("client_id")
    client_secret = tok.get("client_secret")
    token_uri = tok.get("token_uri") or "https://oauth2.googleapis.com/token"
    if not client_id or not client_secret:
        raise RuntimeError(
            f"{TOKEN_PATH} is missing client_id/client_secret — can't reuse it "
            "for re-authorization. Re-run the manual flow in SETUP.md §7."
        )
    # Imported lazily: only these functions need it, and it's an optional
    # dependency every other gauth call has no use for.
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": token_uri,
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes or GOOGLE_SCOPES)
    flow.redirect_uri = "http://localhost"
    return flow


def reauth_authorization_url(flow) -> str:
    """Step 2 of 3: the URL to show the user (any device, any browser — see
    the module note above). Also local/no-network; safe on the main thread.
    """
    # access_type="offline" + prompt="consent": without BOTH of these, a
    # RE-consent (the normal case here — the user has already granted access
    # once before) very often does NOT come back with a refresh_token at
    # all, since Google only issues one on a truly first-ever consent by
    # default. Forcing both guarantees a fresh refresh_token every time,
    # which is the entire point of a flow meant to replace an expired one.
    url, _state = flow.authorization_url(access_type="offline", prompt="consent")
    return url


def complete_reauth(flow, response_or_code: str) -> None:
    """Step 3 of 3: exchanges whatever the user pasted back — either the
    FULL URL their browser tried (and failed) to load after consenting, or
    just the bare `code=...` value out of it, whichever they found easier to
    copy — for tokens, and overwrites TOKEN_PATH with the result.

    BLOCKS on a network call (the token exchange with Google) — callers MUST
    run this on a worker thread, same rule as every other gauth call (see
    AGENTS.md's fetch/apply split). Must be called with the SAME `flow`
    object `reauth_authorization_url` was called on (it carries the OAuth
    `state` generated in that step, needed to validate a full pasted URL).
    """
    response_or_code = response_or_code.strip()
    if "://" in response_or_code:
        flow.fetch_token(authorization_response=response_or_code)
    else:
        flow.fetch_token(code=response_or_code)
    creds = flow.credentials
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


# ----------------------------------------------------------------------------
# Gmail (threaded)
# ----------------------------------------------------------------------------

def list_labels(svc) -> list[dict]:
    g = svc["gmail"]
    return g.users().labels().list(userId="me").execute().get("labels", [])


def _thread_summary(thread_id: str, th: dict, history_id: str = "") -> dict | None:
    """Shape one threads().get(format="metadata") response into the row dict
    the Email pane renders. Returns None for a thread with no messages."""
    msgs = th.get("messages", [])
    if not msgs:
        return None
    last = msgs[-1]
    hdrs = {h["name"].lower(): h["value"] for h in last.get("payload", {}).get("headers", [])}
    return {
        "threadId": thread_id,
        "subject": hdrs.get("subject", "(no subject)"),
        "from": hdrs.get("from", ""),
        "date": hdrs.get("date", ""),
        "count": len(msgs),
        "unread": any("UNREAD" in m.get("labelIds", []) for m in msgs),
        # Gmail message resources include a top-level "snippet" regardless
        # of `format` — no extra API call needed. Backs the Email pane's
        # Space-to-expand inline preview (main.py's _toggle_thread_expand).
        "snippet": last.get("snippet", ""),
        # Gmail bumps a thread's historyId on ANY change to it — new message,
        # read/unread, label edit. Stored on the row so the next refresh can
        # tell "this thread is byte-for-byte what I already have" from "this
        # one actually changed" without fetching it. See list_threads().
        "historyId": str(history_id or th.get("historyId") or ""),
    }


# Gmail's JSON-batch endpoint accepts up to 100 sub-requests per HTTP call;
# Google's own docs recommend staying at/below 50 to avoid rate-limit blowback.
_BATCH_SIZE = 50


def list_threads(svc, max_results: int = 50, q: str | None = None,
                 label_ids: list[str] | None = None,
                 known: dict[str, dict] | None = None) -> list[dict]:
    """List threads with their metadata.

    Two things keep this cheap:

    1. **Revalidate against the cache instead of refetching it.** The
       `threads().list` response already carries each thread's current
       `historyId`, and Gmail bumps that on any change to the thread (new
       message, read/unread, label edit). `known` is the caller's cached
       {thread_id: summary_row} map; any listed thread whose historyId still
       matches its cached row is *already in hand* and is reused verbatim —
       we never ask Gmail for it. On a refresh where nothing has changed (the
       common case) this whole function costs ONE API call.

    2. Whatever genuinely did change is fetched through Gmail's HTTP **batch**
       endpoint (one request per _BATCH_SIZE threads), not one sequential
       round-trip each. The old loop cost ~160 sequential calls at
       max_results=80 and was measured at ~20 SECONDS.

    Order is preserved (rows are reassembled in the order Gmail listed them),
    and a failed sub-request is skipped rather than taking the whole list down.
    """
    g = svc["gmail"]
    params = {"userId": "me", "maxResults": max_results}
    if q:
        params["q"] = q
    if label_ids:
        params["labelIds"] = label_ids
    resp = g.users().threads().list(**params).execute()
    listed = resp.get("threads", [])
    if not listed:
        return []

    known = known or {}
    reused: dict[str, dict] = {}
    stale: list[str] = []
    ids: list[str] = []
    for t in listed:
        tid = t["id"]
        ids.append(tid)
        hid = str(t.get("historyId") or "")
        cached = known.get(tid)
        # A cached row with no historyId predates this field (or came from an
        # older cache) — treat it as stale so it gets refetched once and picks
        # one up. Never reuse a row we can't prove is current.
        if cached and hid and str(cached.get("historyId") or "") == hid:
            reused[tid] = cached
        else:
            stale.append(tid)

    fetched: dict[str, dict] = {}

    def _on_thread(request_id: str, response: dict, exception) -> None:
        if exception is not None or not response:
            return  # skip this row; a single bad thread shouldn't kill the list
        fetched[request_id] = response

    for i in range(0, len(stale), _BATCH_SIZE):
        chunk = stale[i:i + _BATCH_SIZE]
        batch = g.new_batch_http_request()
        for tid in chunk:
            batch.add(
                g.users().threads().get(
                    userId="me", id=tid, format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ),
                request_id=tid,
                callback=_on_thread,
            )
        batch.execute()

    out = []
    for tid in ids:
        if tid in reused:
            out.append(reused[tid])
            continue
        th = fetched.get(tid)
        if th is None:
            continue
        row = _thread_summary(tid, th)
        if row is not None:
            out.append(row)
    return out


def get_thread(svc, thread_id: str) -> list[dict]:
    g = svc["gmail"]
    th = g.users().threads().get(userId="me", id=thread_id, format="full").execute()
    msgs = []
    for m in th.get("messages", []):
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        body = _extract_body(m.get("payload", {}))
        html_body = _extract_html_body(m.get("payload", {}))
        msgs.append({
            "id": m["id"],
            "from": hdrs.get("from", ""),
            "to": hdrs.get("to", ""),
            "subject": hdrs.get("subject", ""),
            "date": hdrs.get("date", ""),
            "body": body,
            # HTML body (P1 M4), empty string when the message has no HTML
            # part at all (plain-text-only mail). "body" above is untouched
            # — ask.py's build_ctx() and other callers rely on it staying a
            # plain-text string; html_body is purely additive.
            "html_body": html_body,
        })
    return msgs


def _extract_body(payload: dict) -> str:
    if "parts" in payload:
        for p in payload["parts"]:
            if p.get("mimeType") == "text/plain" and "data" in p.get("body", {}):
                return _decode(p["body"]["data"])
        for p in payload["parts"]:
            r = _extract_body(p)
            if r:
                return r
    body = payload.get("body", {})
    if "data" in body:
        return _decode(body["data"])
    return ""


def _extract_html_body(payload: dict) -> str:
    """Sibling to `_extract_body`, preferring `text/html` parts instead of
    `text/plain`. Returns "" when the message has no HTML part (plain-text-
    only mail) — callers (ThreadModal) fall back to the plain-text `body` in
    that case rather than treating "" as an error.
    """
    if "parts" in payload:
        for p in payload["parts"]:
            if p.get("mimeType") == "text/html" and "data" in p.get("body", {}):
                return _decode(p["body"]["data"])
        for p in payload["parts"]:
            r = _extract_html_body(p)
            if r:
                return r
        return ""
    if payload.get("mimeType") == "text/html":
        body = payload.get("body", {})
        if "data" in body:
            return _decode(body["data"])
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", "replace")


def send_message(svc, to: str, subject: str, body: str,
                 in_reply_to: str | None = None, references: str | None = None,
                 thread_id: str | None = None) -> dict:
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    obj = {"raw": raw}
    if thread_id:
        obj["threadId"] = thread_id
    return svc["gmail"].users().messages().send(userId="me", body=obj).execute()


def reply_to(svc, thread_id: str, body: str, reply_all: bool = False) -> dict:
    g = svc["gmail"]
    th = g.users().threads().get(userId="me", id=thread_id, format="metadata",
                                 metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID", "References"]).execute()
    last = th["messages"][-1]
    hdrs = {h["name"].lower(): h["value"] for h in last.get("payload", {}).get("headers", [])}
    sender = hdrs.get("from", "")
    to = sender
    if reply_all:
        extra = []
        for k in ("to", "cc"):
            if hdrs.get(k):
                extra.append(hdrs[k])
        to = ", ".join([sender] + extra)
    subject = hdrs.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    return send_message(
        svc, to=to, subject=subject, body=body,
        in_reply_to=hdrs.get("message-id"),
        references=(hdrs.get("references", "") + " " + hdrs.get("message-id", "")).strip(),
        thread_id=thread_id,
    )


def forward(svc, thread_id: str, to: str, body_prefix: str = "") -> dict:
    g = svc["gmail"]
    th = g.users().threads().get(userId="me", id=thread_id, format="full").execute()
    last = th["messages"][-1]
    hdrs = {h["name"].lower(): h["value"] for h in last.get("payload", {}).get("headers", [])}
    subject = hdrs.get("subject", "")
    if not subject.lower().startswith("fwd:"):
        subject = "Fwd: " + subject
    original = _extract_body(last.get("payload", {}))
    forwarded = f"\n\n---------- Forwarded message ----------\nFrom: {hdrs.get('from','')}\nDate: {hdrs.get('date','')}\nSubject: {hdrs.get('subject','')}\nTo: {hdrs.get('to','')}\n\n{original}"
    return send_message(svc, to=to, subject=subject, body=body_prefix + forwarded)


def mark_read(svc, thread_id: str) -> None:
    g = svc["gmail"]
    th = g.users().threads().get(userId="me", id=thread_id, format="minimal").execute()
    ids = [m["id"] for m in th.get("messages", [])]
    if ids:
        g.users().messages().batchModify(
            userId="me", body={"ids": ids, "removeLabelIds": ["UNREAD"]}).execute()


def mark_unread(svc, thread_id: str) -> dict:
    """Mark a whole thread unread again (adds the UNREAD label to every
    message in it). The inverse of ``mark_read``; ``threads().modify`` is
    the thread-level counterpart to ``mark_read``'s per-message
    ``batchModify`` — either works, thread-level is one call."""
    g = svc["gmail"]
    return g.users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": ["UNREAD"]}).execute()


def trash_thread(svc, thread_id: str) -> dict:
    """Move a whole thread to Trash — the RECOVERABLE delete.

    Named ``trash_thread`` (not ``delete_thread``) on purpose: this calls
    ``threads().trash``, which moves the thread to Trash (recoverable for
    ~30 days), exactly like pressing "Delete" in Gmail's own web UI does.
    It deliberately does NOT call ``threads().delete``, which is a
    permanent, irreversible removal — a genuinely destructive action the
    ROADMAP's literal "delete" wording would have implied but no user
    pressing a "delete" key expects. See CHANGELOG [2026-07-15]."""
    g = svc["gmail"]
    return g.users().threads().trash(userId="me", id=thread_id).execute()


def archive_thread(svc, thread_id: str) -> dict:
    """Archive a thread = remove it from the Inbox (remove the INBOX label).

    Safely reversible: the thread isn't deleted, it just no longer appears
    in the Inbox view. This is what "remove from inbox"/"save & archive"
    means in Gmail."""
    g = svc["gmail"]
    return g.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": ["INBOX"]}).execute()


def modify_labels(svc, thread_id: str, add: list[str] | None = None,
                  remove: list[str] | None = None) -> dict:
    """Add/remove Gmail labels on a whole thread via ``threads().modify``.

    ``add``/``remove`` are lists of label IDs (not display names — see
    ``list_labels``). A no-op (both empty) short-circuits without an API
    call."""
    add = add or []
    remove = remove or []
    if not add and not remove:
        return {}
    g = svc["gmail"]
    body: dict = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    return g.users().threads().modify(userId="me", id=thread_id, body=body).execute()


# ----------------------------------------------------------------------------
# Calendar
# ----------------------------------------------------------------------------

def list_events(svc, days: int = 14) -> list[dict]:
    cal = svc["calendar"]
    now = datetime.now(timezone.utc)
    resp = cal.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(days=days)).isoformat(),
        orderBy="startTime", singleEvents=True,
    ).execute()
    return resp.get("items", [])


def events_between(svc, start: datetime, end: datetime) -> list[dict]:
    cal = svc["calendar"]
    resp = cal.events().list(
        calendarId="primary",
        timeMin=start.isoformat(), timeMax=end.isoformat(),
        orderBy="startTime", singleEvents=True,
    ).execute()
    return resp.get("items", [])


def month_events(svc, year: int, month: int) -> list[dict]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return events_between(svc, start, end)


def create_event(svc, summary: str, start, end, all_day: bool = False,
                  description: str = "", location: str = "") -> dict:
    """Create a Calendar event via `events().insert`.

    Returns the raw created-event dict `events().insert` hands back, the
    same shape `list_events`/`events_between`/`month_events` already
    return (a plain Calendar API event resource) — so a freshly created
    event merges straight into the Month/Week grids and the Mail tab's
    Events pane with no special case.

    `start`/`end` are `datetime.date` for an all-day event (`all_day=True`
    — Calendar's API distinguishes `{'date': 'YYYY-MM-DD'}` from
    `{'dateTime': ..., 'timeZone': ...}`) or a tz-AWARE `datetime.datetime`
    for a timed event. The caller (`CreateEventModal` in main.py) is
    responsible for attaching a timezone before calling this: a naive
    datetime's `.isoformat()` carries no UTC offset, and Calendar's API
    needs one (or an explicit `timeZone` field, which this omits — the
    offset embedded in `dateTime` is sufficient for a non-recurring event)
    to place the event at the intended wall-clock time rather than UTC.

    No `attendees` field is ever set — this app has no Contacts-based
    invite flow for events (out of scope; see ROADMAP history for the
    "Calendar create event" item this implements).
    """
    cal = svc["calendar"]
    if all_day:
        start_body = {"date": start.isoformat()}
        end_body = {"date": end.isoformat()}
    else:
        start_body = {"dateTime": start.isoformat()}
        end_body = {"dateTime": end.isoformat()}
    body: dict = {"summary": summary, "start": start_body, "end": end_body}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    return cal.events().insert(calendarId="primary", body=body).execute()


def _event_start(e: dict) -> datetime:
    s = e.get("start", {})
    if "dateTime" in s:
        return datetime.fromisoformat(s["dateTime"])
    return datetime.fromisoformat(s["date"] + "T00:00:00+00:00")


# ----------------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------------

def list_tasklists(svc) -> list[dict]:
    return svc["tasks"].tasklists().list().execute().get("items", [])


def list_tasks(svc, list_id: str, show_completed: bool = True) -> list[dict]:
    params = {"tasklist": list_id, "showCompleted": show_completed, "showHidden": False}
    items = svc["tasks"].tasks().list(**params).execute().get("items", [])
    return items  # each: id, title, status, parent, notes, due


def set_task_status(svc, list_id: str, task_id: str, completed: bool) -> dict:
    t = svc["tasks"]
    cur = t.tasks().get(tasklist=list_id, task=task_id).execute()
    cur["status"] = "completed" if completed else "needsAction"
    if completed:
        cur["completed"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        cur.pop("completed", None)
    return t.tasks().update(tasklist=list_id, task=task_id, body=cur).execute()


def create_task(svc, tasklist_id: str, title: str, parent: str | None = None,
                 notes: str | None = None) -> dict:
    """Create a task, or a subtask if `parent` is given.

    Google's Tasks API models a subtask as an ordinary task whose `parent`
    field points at another task's id in the SAME tasklist — `tasks().list`
    already returns that `parent` field on every item (see `list_tasks`
    above), and `tasks().insert` accepts `parent` as a query parameter that
    makes the newly-created task a child of it. So one helper covers both
    "add a top-level task" (parent=None) and "add a subtask" (parent=<id>) —
    there is no separate subtask-creation endpoint to call.
    """
    body: dict = {"title": title}
    if notes:
        body["notes"] = notes
    kwargs: dict = {"tasklist": tasklist_id, "body": body}
    if parent:
        kwargs["parent"] = parent
    return svc["tasks"].tasks().insert(**kwargs).execute()


def delete_task(svc, tasklist_id: str, task_id: str) -> None:
    """Delete a task OR a subtask — both are just `tasks().delete` by id;
    Google cascades the deletion to any of its own children automatically.
    """
    svc["tasks"].tasks().delete(tasklist=tasklist_id, task=task_id).execute()


# ----------------------------------------------------------------------------
# Drive
# ----------------------------------------------------------------------------

_MIME_EXPORT = {
    "document": "text/plain",
    "spreadsheet": "text/csv",
    "presentation": "text/plain",
    "drawing": "image/png",
}


def list_drive(svc, folder_id: str = "root", max_results: int = 200) -> list[dict]:
    d = svc["drive"]
    q = f"'{folder_id}' in parents and trashed=false"
    resp = d.files().list(
        q=q, pageSize=max_results,
        fields="files(id,name,mimeType,modifiedTime,parents,size)",
    ).execute()
    return resp.get("files", [])


def get_file_metadata(svc, file_id: str) -> dict:
    d = svc["drive"]
    return d.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size,owners,modifiedTime,createdTime,parents,webViewLink",
    ).execute()


def read_drive_text(svc, file_id: str):
    d = svc["drive"]
    meta = d.files().get(fileId=file_id, fields="mimeType,name").execute()
    mime = meta["mimeType"]
    if mime.startswith("application/vnd.google-apps."):
        kind = mime.split(".")[-1]
        target = _MIME_EXPORT.get(kind, "text/plain")
        data = d.files().export_media(fileId=file_id, mimeType=target).execute()
    else:
        data = d.files().get_media(fileId=file_id).execute()
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    return meta["name"], mime, text


# ----------------------------------------------------------------------------
# People / Contacts (P1 M5)
# ----------------------------------------------------------------------------

def list_contacts(svc) -> list[dict]:
    """Every "My Contacts" connection (people.connections.list against
    resourceName="people/me"). Deliberately does NOT call otherContacts.list
    (Gmail-derived auto-contacts) — that needs a separate
    contacts.other.readonly scope not requested by this project; out of
    scope for v1 (see AGENTS.md / ROADMAP).

    Requires the `contacts.readonly` scope on the token (see SETUP.md §7).
    Against a token minted before that scope was added, this raises an
    HttpError (403) — the caller is responsible for catching it and
    surfacing a clear "re-run the OAuth flow" message, same as every other
    gauth call (see main.py's fetch/apply split).

    Paginates via pageToken (pageSize is capped at 1000 by the API; a
    personal account's whole contact list very likely fits in one page, but
    this loops rather than silently dropping anything past page 1).
    """
    p = svc["people"]
    out: list[dict] = []
    page_token: str | None = None
    while True:
        params = {
            "resourceName": "people/me",
            "personFields": "names,emailAddresses,phoneNumbers",
            "pageSize": 1000,
            "sortOrder": "FIRST_NAME_ASCENDING",
        }
        if page_token:
            params["pageToken"] = page_token
        resp = p.people().connections().list(**params).execute()
        for person in resp.get("connections", []):
            names = person.get("names", [])
            emails = person.get("emailAddresses", [])
            phones = person.get("phoneNumbers", [])
            out.append({
                "resource_name": person.get("resourceName", ""),
                "name": names[0].get("displayName", "") if names else "",
                "email": emails[0].get("value", "") if emails else "",
                "phone": phones[0].get("value", "") if phones else "",
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out
