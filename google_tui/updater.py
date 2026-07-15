"""Startup self-update check.

google-tui is installed as an editable checkout of its own git repo (see
SETUP.md), so "update" here means "fast-forward the checkout to origin and
re-exec", not "download a wheel". This runs on the console BEFORE the Textual
app starts — deliberately, for two reasons: the messages are plain stdout lines
rather than TUI chrome, and an update has to take effect by re-exec'ing the
process (the running interpreter has already imported the OLD modules, so
pulling new code without restarting would report an update that isn't actually
running).

Safety rules, in order of importance:

* **Never touch uncommitted work.** If the working tree is dirty we skip
  entirely. This repo is somebody's working copy, not a deployment artifact.
* **Fast-forward only.** No merges, no rebases, no resets. If local and origin
  have diverged we skip and say so; resolving that is a human's job.
* **Never block startup.** Every git call is timeout-bounded, and *any* failure
  (offline, DNS dead, git missing, auth prompt, whatever) degrades to a printed
  line and a normal launch. An update check must not be able to stop you
  reading your mail.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Bound every git call: a hung network (or a git that decides to prompt for
# credentials) must never wedge startup. Fetch gets the longer budget since
# it's the only one that talks to the network. Merge gets the longest budget
# of all: repos with `core.hooksPath` pointed at hooks/ (see SETUP.md) run
# hooks/post-merge synchronously as *part of* the merge command, and that
# hook does `pip install -e .` whenever pyproject.toml changed — which it
# does on every commit here, since hooks/pre-commit bumps its version field
# each time. A no-op `pip install -e .` reinstall reliably takes 10+ seconds
# even when nothing actually needs installing, comfortably outrunning
# _GIT_TIMEOUT. If it does, subprocess.run(timeout=...) kills the `git`
# process, but pip keeps running as an orphan in the background, writing
# into the very venv the restarted process (see restart() below) is about
# to re-import from — a race that can corrupt the relaunch. Give merge a
# budget wide enough to cover a cold pip resolve so it always finishes
# before we act on its result.
_GIT_TIMEOUT = 10
_FETCH_TIMEOUT = 20
_MERGE_TIMEOUT = 90


def _repo_root() -> Path | None:
    """The git checkout this package lives in, or None if it isn't one (e.g.
    installed as a plain wheel — nothing to fast-forward in that case)."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() else None


def _git(root: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[int, str]:
    """Run git, returning (returncode, stdout). Never raises."""
    try:
        p = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
            # Refuse interactive credential/passphrase prompts outright: a
            # prompt with nowhere to type is a hang, and a hang here blocks the
            # whole app from starting.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""
    return p.returncode, p.stdout.strip()


def describe(root: Path | None = None) -> str:
    """Human-facing version string.

    Prefers the nearest release tag (`v1.2.3`, or `v1.2.3-4-gabc1234` a few
    commits past it). An untagged repo has no version to report, so we fall back
    to the packaged __version__ plus the short sha — NOT to a bare sha with a
    "v" glued on the front, which just looks like a corrupt version number.
    """
    root = root or _repo_root()
    if root is None:
        from . import __version__
        return f"v{__version__}"
    # An exact tag on HEAD wins — that's a deliberate release marker. Otherwise
    # __version__ is authoritative (hooks/pre-commit bumps it on every commit,
    # so it's already unique per commit) and we just append the sha to pin it.
    # Note we do NOT fall back to a bare `git describe`: that yields things like
    # "v0.2.0-3-gabc1234", which contradicts the version the app reports about
    # itself.
    rc, tag = _git(root, "describe", "--tags", "--exact-match")
    if rc == 0 and tag:
        return tag if tag[:1] == "v" else f"v{tag}"
    version = _read_version_from_disk(root)
    rc, sha = _git(root, "rev-parse", "--short", "HEAD")
    return f"v{version} ({sha})" if rc == 0 and sha else f"v{version}"


def _read_version_from_disk(root: Path) -> str:
    """`__version__` as it stands in the *checked-out file*, not the cached
    `google_tui` module attribute. describe() is called right after
    check_for_update() fast-forwards the checkout — at that point the
    already-imported module still holds the pre-update version (Python
    doesn't re-read a module off disk just because git rewrote it), while
    the sha we pair it with (`git rev-parse`) is already the new one. Reading
    the file directly keeps the two in sync instead of reporting an old
    version number next to a new commit hash.
    """
    try:
        text = (root / "google_tui" / "__init__.py").read_text()
        for line in text.splitlines():
            if line.strip().startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    from . import __version__
    return __version__


def check_for_update(quiet: bool = False) -> bool:
    """Fast-forward the checkout to origin if it's cleanly behind.

    Returns True if new code was pulled (caller should re-exec), False in every
    other case. Prints exactly one status line unless `quiet`.
    """
    def say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    root = _repo_root()
    if root is None:
        say("No update found, loading application")
        return False

    # Uncommitted changes -> hands off. Pulling over someone's work-in-progress
    # to save them a version number is a terrible trade.
    #
    # `--untracked-files=no` is deliberate: we only care about modifications to
    # TRACKED files, which are what a fast-forward could destroy. Counting
    # untracked files as "dirty" would mean a stray __pycache__ or a scratch
    # file silently wedges the updater forever — and they're not at risk anyway,
    # since `merge --ff-only` refuses (safely, without writing) if it would have
    # to overwrite an untracked file.
    rc, dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if rc != 0:
        say("Can't reach update server, skipping update check.")
        return False
    if dirty:
        say("Local changes present, skipping update check.")
        return False

    rc, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or not branch or branch == "HEAD":  # detached: no upstream to track
        say("Local changes present, skipping update check.")
        return False

    # The only network call. Anything that goes wrong here is "can't reach the
    # update server" as far as the user is concerned. `--tags` matters: without
    # it a fetch of just the branch leaves release tags unfetched, and describe()
    # would then report the new version as a bare commit sha.
    rc, _ = _git(root, "fetch", "--quiet", "--tags", "origin", branch,
                 timeout=_FETCH_TIMEOUT)
    if rc != 0:
        say("Can't reach update server, skipping update check.")
        return False

    rc_l, local = _git(root, "rev-parse", "HEAD")
    rc_r, remote = _git(root, "rev-parse", f"origin/{branch}")
    if rc_l != 0 or rc_r != 0 or not local or not remote:
        say("Can't reach update server, skipping update check.")
        return False

    if local == remote:
        say("No update found, loading application")
        return False

    # Behind == our HEAD is an ancestor of origin's. If it isn't, we've either
    # diverged or we're ahead (unpushed commits) — either way a fast-forward is
    # wrong and a merge/reset would be destructive. Leave it to the human.
    rc, _ = _git(root, "merge-base", "--is-ancestor", local, remote)
    if rc != 0:
        say("Local branch has diverged from origin, skipping update check.")
        return False

    # One line, completed in place: "Downloading update... updated to v1.2.3".
    print("Downloading update...", end="", flush=True)
    rc, _ = _git(root, "merge", "--ff-only", f"origin/{branch}", timeout=_MERGE_TIMEOUT)
    if rc != 0:
        print()  # close the dangling line before reporting the failure
        say("Can't reach update server, skipping update check.")
        return False
    print(f" updated to {describe(root)}", flush=True)
    return True


def restart() -> None:
    """Re-exec so the code we just pulled is the code that actually runs."""
    os.execv(sys.executable, [sys.executable, "-m", "google_tui", *sys.argv[1:]])
