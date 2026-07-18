"""Drive-tab file-source backends: Google Drive, FTP, SSH (SFTP/legacy SCP).

The Drive tab's UI (main.py) was written against Google Drive's shapes only.
`DriveBackend` normalizes every source to the same shape so that UI code
(folder navigation, list rendering, preview, download) works unchanged
regardless of which source is active — see ROADMAP/AGENTS.md for the design
rationale (source-agnostic Drive tab, replacing Browser's old inline ftp://
handling).

Normalized item dict, returned by `list_children`:
    {"id": str, "name": str, "mimeType": str, "is_folder": bool,
     "modifiedTime": str, "size": str}
`id` is opaque for Google Drive, but the full remote path for FTP/SSH (these
are path-addressed filesystems with no separate id concept) -- folder
navigation's (folder_id, path) stack pattern works unchanged either way since
folder_id and path simply coincide for path-addressed sources.
"""
from __future__ import annotations

import datetime as dt
import ftplib
import mimetypes
import posixpath
import shlex
import stat
import tempfile
import threading
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

import paramiko
import scp as scp_module

from google_tui import gauth


class RemoteAuthRequired(Exception):
    """Login failed/was refused -- distinct from RemoteBackendError so the
    caller can prompt for credentials instead of just showing an error."""
    def __init__(self, protocol: str, host: str, port: int, detail: str):
        super().__init__(detail)
        self.protocol = protocol
        self.host = host
        self.port = port
        self.detail = detail


class RemoteBackendError(Exception):
    """Any other listing/preview/download failure against a remote source."""


class DriveBackend(Protocol):
    source_key: str   # "google" | f"{protocol}:{host}:{port}"
    label: str         # shown in the Drive-tab source picker
    root_id: str
    root_path: str

    def list_children(self, folder_id: str, page_token: str | None
                       ) -> tuple[list[dict], str | None]: ...

    def get_metadata(self, node_id: str) -> dict:
        """Normalized: name, mimeType, size, owner: str|None,
        createdTime: str|None, modifiedTime: str."""
        ...

    def read_preview_text(self, node_id: str) -> str: ...

    def download(self, node_id: str) -> tuple[str, bytes]: ...

    def close(self) -> None: ...


class GoogleDriveSource:
    """Thin wrapper over the existing, unmodified gauth Drive functions --
    the only backend constructed from an already-authenticated `svc`, not a
    host/port/credentials tuple."""

    source_key = "google"
    label = "Google Drive"
    root_id = "root"
    root_path = "/"

    _FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, svc):
        self.svc = svc

    def list_children(self, folder_id, page_token=None):
        files, next_page_token = gauth.list_drive(self.svc, folder_id, page_token=page_token)
        for f in files:
            f["is_folder"] = f["mimeType"] == self._FOLDER_MIME
        return files, next_page_token

    def get_metadata(self, node_id):
        meta = gauth.get_file_metadata(self.svc, node_id)
        owners = ", ".join(
            o.get("displayName", o.get("emailAddress", "?")) for o in meta.get("owners", []))
        return {
            "name": meta.get("name", ""),
            "mimeType": meta.get("mimeType", ""),
            "size": meta.get("size", ""),
            "owner": owners or None,
            "createdTime": meta.get("createdTime") or None,
            "modifiedTime": meta.get("modifiedTime", ""),
        }

    def read_preview_text(self, node_id):
        _, _, text = gauth.read_drive_text(self.svc, node_id)
        return text

    def download(self, node_id):
        return gauth.download_drive_file(self.svc, node_id)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# FTP
# ---------------------------------------------------------------------------

FTP_DEFAULT_PORT = 21
# A preview is read into memory in one shot (ftplib has no streaming reader)
# and capped -- same precedent as Drive's text preview / gauth's size cap.
# Binary files just render as replacement-char garbage past whatever's
# readable as UTF-8; no binary/text sniffing here (matches Drive's own gap).
_FTP_MAX_PREVIEW_BYTES = 200_000


def _parse_unix_ls_lines(lines: list[str]) -> list[tuple[str, dict]]:
    """Parse classic ``LIST``/``ls -la`` output (the format nearly every
    FTPD, and most SSH remote shells, emit) into ``(name, facts)`` pairs,
    the same shape ``FTP.mlsd()`` yields (just with only "type" and, for
    files, "size" filled in). Non-conforming lines (e.g. a Windows/MS-DOS
    -style listing, a totals/header line) are silently skipped rather than
    guessed at. Pure text parsing, no I/O -- shared by FtpSource's MLSD
    fallback and SshSource's exec-command fallback.
    """
    entries: list[tuple[str, dict]] = []
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9 or parts[0][:1] not in "dl-":
            continue
        perms, size_field, name = parts[0], parts[4], parts[8]
        if name in (".", ".."):
            continue
        # A symlink ("l...") could point at either a file or a directory —
        # with no cheap way to tell from listing text alone, treat it as a
        # file (the more common case for a symlink in an archive tree).
        facts = {"type": "dir" if perms.startswith("d") else "file"}
        if not perms.startswith("d") and size_field.isdigit():
            facts["size"] = int(size_field)
        entries.append((name, facts))
    return entries


def _mlsd_modify_to_iso(modify: str) -> str:
    """RFC 3659 MLSD "modify" fact (YYYYMMDDHHMMSS[.sss], UTC) -> ISO 8601,
    so it's comparable with Google's modifiedTime by main.py's _fmt_date and
    the preview freshness-cache check. Returns "" if unparseable."""
    try:
        d = dt.datetime.strptime(modify[:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        return d.isoformat()
    except ValueError:
        return ""


def _guess_mime(name: str, is_folder: bool) -> str:
    if is_folder:
        return "application/vnd.folder"  # never compared against Google's folder sentinel; is_folder is authoritative
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class FtpSource:
    """No persistent connection -- opens/logs in/quits fresh per call, same
    as the FTP handling this replaces (main.py's old Browser-tab flow). FTP
    control-channel login is cheap, and this sidesteps concurrent-worker
    thread-safety concerns entirely (unlike SshSource, which must hold one
    connection open and lock around it)."""

    root_id = "/"
    root_path = "/"

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username or "anonymous"
        self.password = password or "anonymous@"
        self.source_key = f"ftp:{host}:{port}"
        self.label = f"ftp://{host}"

    def _connect(self) -> ftplib.FTP:
        ftp = ftplib.FTP()
        try:
            ftp.connect(self.host, self.port, timeout=15)
        except OSError as e:
            raise RemoteBackendError(f"FTP connection to {self.host}:{self.port} failed: {e}") from e
        try:
            ftp.login(self.username, self.password)
        except ftplib.error_perm as e:
            try:
                ftp.close()
            except Exception:
                pass
            raise RemoteAuthRequired("ftp", self.host, self.port, str(e)) from e
        return ftp

    def list_children(self, folder_id, page_token=None):
        ftp = self._connect()
        try:
            try:
                ftp.cwd(folder_id)
            except ftplib.error_perm as e:
                raise RemoteBackendError(f"Cannot open {folder_id}: {e}") from e
            try:
                raw_entries = sorted(ftp.mlsd(), key=lambda e: e[0])
                has_mtime = True
            except Exception:
                # MLSD (RFC 3659) is far from universal — confirmed live
                # against ftp.gnu.org, which refuses it ("500 Unknown
                # command"). Fall back to classic LIST text parsing.
                raw_entries = sorted(_parse_unix_ls_lines(_ftp_list_lines(ftp)), key=lambda e: e[0])
                has_mtime = False
            items = []
            base = folder_id.rstrip("/")
            for name, facts in raw_entries:
                if name in (".", ".."):
                    continue
                is_folder = facts.get("type") == "dir"
                size = facts.get("size")
                modified = _mlsd_modify_to_iso(facts["modify"]) if has_mtime and "modify" in facts else ""
                items.append({
                    "id": f"{base}/{name}",
                    "name": name,
                    "mimeType": _guess_mime(name, is_folder),
                    "is_folder": is_folder,
                    "modifiedTime": modified,
                    "size": str(size) if size is not None else "",
                })
            return items, None  # no pagination -- a listing is one shot
        except ftplib.all_errors as e:
            raise RemoteBackendError(f"FTP error: {e}") from e
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def get_metadata(self, node_id):
        ftp = self._connect()
        try:
            name = posixpath.basename(node_id) or node_id
            size = None
            modified = ""
            try:
                size = ftp.size(node_id)
            except Exception:
                pass
            try:
                resp = ftp.sendcmd(f"MDTM {node_id}")  # "213 YYYYMMDDHHMMSS"
                modified = _mlsd_modify_to_iso(resp.split(None, 1)[1])
            except Exception:
                pass
            return {
                "name": name,
                "mimeType": _guess_mime(name, False),
                "size": str(size) if size is not None else "",
                "owner": None,
                "createdTime": None,
                "modifiedTime": modified,
            }
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def read_preview_text(self, node_id):
        ftp = self._connect()
        try:
            chunks: list[bytes] = []
            total = 0

            def _collect(data: bytes) -> None:
                nonlocal total
                total += len(data)
                if total <= _FTP_MAX_PREVIEW_BYTES:
                    chunks.append(data)

            try:
                ftp.retrbinary(f"RETR {node_id}", _collect)
            except ftplib.error_perm as e:
                raise RemoteBackendError(f"Cannot retrieve {node_id}: {e}") from e
            text = b"".join(chunks).decode("utf-8", errors="replace")
            if total > _FTP_MAX_PREVIEW_BYTES:
                text += f"\n[truncated — file exceeds the {_FTP_MAX_PREVIEW_BYTES:,}-byte preview limit]"
            return text
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def download(self, node_id):
        ftp = self._connect()
        try:
            chunks: list[bytes] = []
            try:
                ftp.retrbinary(f"RETR {node_id}", chunks.append)
            except ftplib.error_perm as e:
                raise RemoteBackendError(f"Cannot retrieve {node_id}: {e}") from e
            return posixpath.basename(node_id) or node_id, b"".join(chunks)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def close(self):
        pass  # nothing persistent to close


def _ftp_list_lines(ftp: ftplib.FTP) -> list[str]:
    lines: list[str] = []
    ftp.retrlines("LIST", lines.append)
    return lines


def parse_ftp_url(url: str) -> tuple[str, int, str, str, str]:
    """(host, port, path, username, password) from an ftp:// URL, applying
    the same anonymous-login default the old Browser-tab flow used."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise RemoteBackendError(f"Invalid ftp URL: {url}")
    port = parsed.port or FTP_DEFAULT_PORT
    path = unquote(parsed.path) or "/"
    user = parsed.username or "anonymous"
    passwd = parsed.password or "anonymous@"
    return host, port, path, user, passwd


def parse_sftp_url(url: str) -> tuple[str, int, str, str, str]:
    """(host, port, path, username, password) from an sftp:// URL. No
    anonymous-login convention for SSH -- an empty username/password means
    "prompt the user," not "try anonymous" (unlike FTP)."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise RemoteBackendError(f"Invalid sftp URL: {url}")
    port = parsed.port or SSH_DEFAULT_PORT
    path = unquote(parsed.path) or "/"
    return host, port, path, parsed.username or "", parsed.password or ""


# ---------------------------------------------------------------------------
# SSH (SFTP, falling back to a legacy-SCP-compatible exec mode)
# ---------------------------------------------------------------------------

SSH_DEFAULT_PORT = 22
_SSH_MAX_PREVIEW_BYTES = 200_000


class SshSource:
    """One persistent paramiko.SSHClient, reused across calls (unlike
    FtpSource) since an SSH handshake+auth is comparatively expensive.
    Tries the SFTP subsystem first (real mtime/size, true partial reads);
    falls back — for this instance's lifetime, decided once — to an
    exec-channel mode (find/ls + head + the `scp` package's SCPClient) only
    when the SFTP subsystem itself is refused, which is the only situation
    where legacy-SCP-only support actually matters (raw SCP has no listing
    or partial-read primitive of its own; that's why the fallback exists).

    Guarded by a lock: main.py runs Drive listing/preview/download in
    independent worker groups, so two calls can legitimately race against
    the same backend instance (e.g. arrowing to a new row while a prior
    download is still in flight) -- paramiko's SFTPClient is not safe for
    unsynchronized concurrent use.

    Host-key verification uses paramiko.AutoAddPolicy (trust-on-first-use,
    in-memory only, no persistent known_hosts) — a disclosed simplification
    for v1, not a full known-hosts store like fetchers.GeminiTofuStore has
    for Gemini certs; revisit if that gap matters in practice.
    """

    root_id = "/"
    root_path = "/"

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.source_key = f"ssh:{host}:{port}"
        self.label = f"ssh://{host}"
        self._lock = threading.Lock()
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._exec_fallback = False  # decided once, on first connect

    def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.host, port=self.port, username=self.username,
                            password=self.password, timeout=15,
                            look_for_keys=False, allow_agent=False)
        except paramiko.AuthenticationException as e:
            raise RemoteAuthRequired("ssh", self.host, self.port, str(e)) from e
        except (paramiko.SSHException, OSError) as e:
            raise RemoteBackendError(f"SSH connection to {self.host}:{self.port} failed: {e}") from e
        try:
            self._sftp = client.open_sftp()
        except paramiko.SSHException:
            # Subsystem request refused (server disables SFTP but still
            # allows exec/scp) -- NOT any other exception, so a transient
            # blip surfaces as a real error instead of silently downgrading
            # a whole session to the slower/riskier exec fallback.
            self._sftp = None
            self._exec_fallback = True
        self._client = client

    def _exec(self, command: str) -> tuple[str, str, int]:
        """Run `command` over an exec channel; (stdout, stderr, exit_status)."""
        stdin, stdout, stderr = self._client.exec_command(command)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        return out, err, status

    def list_children(self, folder_id, page_token=None):
        with self._lock:
            self._ensure_connected()
            if self._sftp is not None:
                return self._list_children_sftp(folder_id), None
            return self._list_children_exec(folder_id), None

    def _list_children_sftp(self, folder_id: str) -> list[dict]:
        try:
            entries = self._sftp.listdir_attr(folder_id)
        except OSError as e:
            raise RemoteBackendError(f"Cannot open {folder_id}: {e}") from e
        base = folder_id.rstrip("/")
        items = []
        for a in entries:
            is_folder = stat.S_ISDIR(a.st_mode) if a.st_mode is not None else False
            modified = (dt.datetime.fromtimestamp(a.st_mtime, tz=dt.timezone.utc).isoformat()
                        if a.st_mtime else "")
            items.append({
                "id": f"{base}/{a.filename}",
                "name": a.filename,
                "mimeType": _guess_mime(a.filename, is_folder),
                "is_folder": is_folder,
                "modifiedTime": modified,
                "size": str(a.st_size) if a.st_size is not None else "",
            })
        return sorted(items, key=lambda i: i["name"])

    def _list_children_exec(self, folder_id: str) -> list[dict]:
        quoted = shlex.quote(folder_id)
        # GNU find -printf: unambiguous type char + byte size + epoch mtime,
        # far more robust than parsing `ls -la` text (which varies across
        # BusyBox/GNU coreutils/BSD). Only fall back to `ls -la` (parsed via
        # the shared _parse_unix_ls_lines) if `find` itself isn't available
        # -- best-effort against whatever remote shell is on the other end,
        # not exhaustively tested against every OS.
        out, err, status = self._exec(
            f"find {quoted} -mindepth 1 -maxdepth 1 -printf '%f\\t%y\\t%s\\t%T@\\n'")
        base = folder_id.rstrip("/")
        if status == 0 and not err.strip():
            items = []
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) != 4:
                    continue
                name, type_char, size_s, mtime_s = parts
                is_folder = type_char == "d"
                try:
                    modified = dt.datetime.fromtimestamp(float(mtime_s), tz=dt.timezone.utc).isoformat()
                except ValueError:
                    modified = ""
                items.append({
                    "id": f"{base}/{name}",
                    "name": name,
                    "mimeType": _guess_mime(name, is_folder),
                    "is_folder": is_folder,
                    "modifiedTime": modified,
                    "size": size_s if size_s.isdigit() else "",
                })
            return sorted(items, key=lambda i: i["name"])
        # `find` unavailable/erred -- fall back to ls -la text parsing (no
        # mtime available from this path, same gap FTP's LIST fallback has).
        out, err, status = self._exec(f"ls -la -- {quoted}")
        if status != 0:
            raise RemoteBackendError(f"Cannot open {folder_id}: {err.strip() or 'ls failed'}")
        items = []
        for name, facts in _parse_unix_ls_lines(out.splitlines()):
            is_folder = facts.get("type") == "dir"
            size = facts.get("size")
            items.append({
                "id": f"{base}/{name}",
                "name": name,
                "mimeType": _guess_mime(name, is_folder),
                "is_folder": is_folder,
                "modifiedTime": "",
                "size": str(size) if size is not None else "",
            })
        return sorted(items, key=lambda i: i["name"])

    def get_metadata(self, node_id):
        with self._lock:
            self._ensure_connected()
            name = posixpath.basename(node_id) or node_id
            size = ""
            modified = ""
            if self._sftp is not None:
                try:
                    a = self._sftp.stat(node_id)
                    size = str(a.st_size) if a.st_size is not None else ""
                    if a.st_mtime:
                        modified = dt.datetime.fromtimestamp(a.st_mtime, tz=dt.timezone.utc).isoformat()
                except OSError as e:
                    raise RemoteBackendError(f"Cannot stat {node_id}: {e}") from e
            else:
                out, err, status = self._exec(f"stat -c '%s\\t%Y' -- {shlex.quote(node_id)}")
                if status == 0 and "\t" in out:
                    size_s, mtime_s = out.strip().split("\t", 1)
                    size = size_s if size_s.isdigit() else ""
                    try:
                        modified = dt.datetime.fromtimestamp(float(mtime_s), tz=dt.timezone.utc).isoformat()
                    except ValueError:
                        pass
            return {
                "name": name,
                "mimeType": _guess_mime(name, False),
                "size": size,
                "owner": None,
                "createdTime": None,
                "modifiedTime": modified,
            }

    def read_preview_text(self, node_id):
        with self._lock:
            self._ensure_connected()
            if self._sftp is not None:
                try:
                    with self._sftp.open(node_id, "rb") as f:
                        data = f.read(_SSH_MAX_PREVIEW_BYTES + 1)
                except OSError as e:
                    raise RemoteBackendError(f"Cannot read {node_id}: {e}") from e
                truncated = len(data) > _SSH_MAX_PREVIEW_BYTES
                text = data[:_SSH_MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
            else:
                # No true partial-read without SFTP -- `head -c` over exec
                # bounds what's TRANSFERRED too, unlike FTP's fallback (which
                # still receives the whole file over the wire and just stops
                # storing past the cap).
                out, err, status = self._exec(
                    f"head -c {_SSH_MAX_PREVIEW_BYTES + 1} -- {shlex.quote(node_id)}")
                if status != 0:
                    raise RemoteBackendError(f"Cannot read {node_id}: {err.strip() or 'head failed'}")
                data = out.encode("utf-8", errors="replace")
                truncated = len(data) > _SSH_MAX_PREVIEW_BYTES
                text = data[:_SSH_MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
            if truncated:
                text += f"\n[truncated — file exceeds the {_SSH_MAX_PREVIEW_BYTES:,}-byte preview limit]"
            return text

    def download(self, node_id):
        with self._lock:
            self._ensure_connected()
            name = posixpath.basename(node_id) or node_id
            if self._sftp is not None:
                try:
                    with self._sftp.open(node_id, "rb") as f:
                        return name, f.read()
                except OSError as e:
                    raise RemoteBackendError(f"Cannot download {node_id}: {e}") from e
            # Legacy-SCP-only path: the `scp` package has no in-memory API
            # (paramiko itself doesn't implement the SCP wire protocol at
            # all -- this is why the separate dependency exists), so this
            # goes through a temp file.
            with tempfile.TemporaryDirectory() as tmp:
                local_path = Path(tmp) / name
                try:
                    with scp_module.SCPClient(self._client.get_transport()) as client:
                        client.get(node_id, str(local_path))
                except scp_module.SCPException as e:
                    raise RemoteBackendError(f"Cannot download {node_id}: {e}") from e
                return name, local_path.read_bytes()

    def close(self):
        with self._lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None


def source_key_for(protocol: str, host: str, port: int) -> str:
    return f"{protocol}:{host}:{port}"


def build_source(protocol: str, host: str, port: int, username: str, password: str) -> DriveBackend:
    """Factory used by both the Drive-tab source picker and the Browser
    ftp://sftp:// redirect, so there's exactly one place deciding which
    class backs a given protocol."""
    if protocol == "ftp":
        return FtpSource(host, port, username, password)
    if protocol == "ssh":
        return SshSource(host, port, username, password)
    raise ValueError(f"Unknown remote protocol: {protocol}")
