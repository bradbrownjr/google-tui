"""Hermes Ask pane + Google Search button backends.

Ask pane (option 1, in-process agent):
  - For general questions and questions about the user's Google data we call the
    Nous inference endpoint directly with a system prompt that has access to the
    live Google context we pass in.
  - If the user's message looks like it wants an *action* or a skill run, we
    delegate to the `hermes` CLI so the full agent (tools, skills) handles it.

Search button:
  - We could not find a hardcoded searxng URL, so we shell `hermes` web search,
    which already knows the configured searxng backend. Returns text results.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path

import requests

CONFIG_PATH = Path(os.path.expanduser("~/.hermes/config.yaml"))
_LLM_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
_MODEL = "tencent/hy3:free"


def _read_api_key() -> str | None:
    try:
        txt = CONFIG_PATH.read_text()
    except Exception:
        return None
    m = re.search(r"api_key:\s*(\S+)", txt)
    return m.group(1).strip().strip('"') if m else None


_API_KEY = _read_api_key()


def ask_llm(system: str, question: str, timeout: int = 90) -> str:
    """Call the Nous chat endpoint. Returns the assistant text."""
    if not _API_KEY:
        return "(no API key found in ~/.hermes/config.yaml)"
    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "temperature": 0.3,
    }
    try:
        r = requests.post(_LLM_URL, headers={"Authorization": f"Bearer {_API_KEY}",
                                              "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"(LLM error: {e})"


# Heuristics: does this look like it needs the full Hermes agent (action/skill)?
_ACTION_RE = re.compile(
    r"\b(create|send|schedule|add|delete|update|book|invite|upload|share|run|trigger|"
    r"summariz|draft|reply|skills?|tool)\b", re.I)


def needs_agent(message: str) -> bool:
    return bool(_ACTION_RE.search(message))


def ask_hermes_agent(message: str, timeout: int = 120) -> str:
    """Delegate to the hermes CLI (full agent, with tools/skills)."""
    try:
        r = subprocess.run(["hermes", "-p", message], capture_output=True, text=True,
                           timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip() or "(hermes returned no output)"
    except subprocess.TimeoutExpired:
        return "(hermes timed out)"
    except Exception as e:
        return f"(hermes error: {e})"


def google_search(query: str, timeout: int = 90) -> str:
    """Run a web search via the hermes CLI (uses its searxng backend)."""
    try:
        r = subprocess.run(["hermes", "web", "search", query],
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip() or "(no results)"
    except subprocess.TimeoutExpired:
        return "(search timed out)"
    except Exception as e:
        return f"(search error: {e})"
