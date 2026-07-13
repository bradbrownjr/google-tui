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
    }


# ----------------------------------------------------------------------------
# Gmail (threaded)
# ----------------------------------------------------------------------------

def list_labels(svc) -> list[dict]:
    g = svc["gmail"]
    return g.users().labels().list(userId="me").execute().get("labels", [])


def list_threads(svc, max_results: int = 50, q: str | None = None,
                 label_ids: list[str] | None = None) -> list[dict]:
    g = svc["gmail"]
    params = {"userId": "me", "maxResults": max_results}
    if q:
        params["q"] = q
    if label_ids:
        params["labelIds"] = label_ids
    resp = g.users().threads().list(**params).execute()
    out = []
    for t in resp.get("threads", []):
        th = g.users().threads().get(
            userId="me", id=t["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        msgs = th.get("messages", [])
        if not msgs:
            continue
        last = msgs[-1]
        hdrs = {h["name"].lower(): h["value"] for h in last.get("payload", {}).get("headers", [])}
        out.append({
            "threadId": t["id"],
            "subject": hdrs.get("subject", "(no subject)"),
            "from": hdrs.get("from", ""),
            "date": hdrs.get("date", ""),
            "count": len(msgs),
            "unread": any("UNREAD" in m.get("labelIds", []) for m in msgs),
            # Gmail message resources include a top-level "snippet" regardless
            # of `format` — no extra API call needed. Backs the Email pane's
            # Space-to-expand inline preview (main.py's _toggle_thread_expand).
            "snippet": last.get("snippet", ""),
        })
    return out


def get_thread(svc, thread_id: str) -> list[dict]:
    g = svc["gmail"]
    th = g.users().threads().get(userId="me", id=thread_id, format="full").execute()
    msgs = []
    for m in th.get("messages", []):
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        body = _extract_body(m.get("payload", {}))
        msgs.append({
            "id": m["id"],
            "from": hdrs.get("from", ""),
            "to": hdrs.get("to", ""),
            "subject": hdrs.get("subject", ""),
            "date": hdrs.get("date", ""),
            "body": body,
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
