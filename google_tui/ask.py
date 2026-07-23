"""AI backends for the Ask pane, plus the Google Search button.

Ask pane is provider-agnostic (see AIProvider below): the app always builds
its own Google context locally (gauth.list_threads/list_events, already
using the user's Google token) and hands that text to whichever provider is
selected in Settings. This is how "sharing the Google token with the AI
models" is implemented in practice — the external CLIs never need their own
separate Google integration, they just receive the context as part of the
prompt.

  - Hermes/Nous (default): general questions go straight to the Nous chat
    endpoint (cheap, fast, no tools); questions that look like they want an
    *action* delegate to the full `hermes` CLI agent (tools, skills).
  - opencode / Claude Code / Gemini CLI: each invocation of these IS a full
    agent already, so both plain questions and action-shaped ones go through
    the same one-shot CLI call (`opencode run`, `claude -p`, `gemini -p`).

Browser-tab web search (Google CSE / DuckDuckGo / SearXNG) lives in
`fetchers.py` (`run_search` and friends), not here — this module's
`google_search`/`hermes web search` shell-out was removed once that
subcommand stopped existing in the installed `hermes` CLI; see
CHANGELOG for the fix.
"""
from __future__ import annotations
import os
import re
import shutil
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


def ask_llm(system: str, question: str, api_key: str | None = None, model: str | None = None,
            timeout: int = 90, base_url: str | None = None) -> str:
    """Call an OpenAI-chat-style endpoint -- the Nous cloud API by default,
    or Settings.nous_base_url (Settings -> AI Provider) if set, e.g. a local
    `hermes proxy start` gateway at http://127.0.0.1:8645/v1/chat/completions
    (2026-07-23: this is how a self-hosted gateway gets used instead of the
    hardcoded Nous cloud URL -- see SETUP.md's "Local Hermes gateway"
    section). Returns the assistant text. `model` overrides the default
    (config.toml's llm_model, see app_config.py).

    The "no API key" short-circuit only applies to the DEFAULT Nous cloud
    URL -- a local/self-hosted gateway commonly needs no auth at all, so a
    custom base_url still gets a real request even with no key configured
    (the Authorization header is simply omitted then, rather than sending a
    blank/garbage Bearer token)."""
    key = api_key or _API_KEY
    url = base_url or _LLM_URL
    if not key and url == _LLM_URL:
        return "(no Nous API key — set one in Settings, or in ~/.hermes/config.yaml)"
    payload = {
        "model": model or _MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "temperature": 0.3,
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
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


# ----------------------------------------------------------------------------
# Provider abstraction — pick an AI backend without being locked into Hermes
# ----------------------------------------------------------------------------

class AIProvider:
    id = "base"
    display_name = "Base"

    def is_reachable(self) -> bool:
        """Cheap, local, non-network check: is this provider even usable?"""
        raise NotImplementedError

    def ask(self, system: str, question: str, timeout: int = 90) -> str:
        """Answer a plain question, given Google context in `system`."""
        raise NotImplementedError

    def run_action(self, message: str, timeout: int = 120) -> str:
        """Handle an action-shaped request. Default: same path as ask()."""
        return self.ask("", message, timeout=timeout)


class HermesProvider(AIProvider):
    id = "hermes"
    display_name = "Hermes"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    def is_reachable(self) -> bool:
        # A configured base_url (a local/self-hosted gateway) counts as
        # "reachable" on its own -- plenty of those need no API key at all.
        return (bool(self._api_key or _API_KEY or self._base_url)
                or shutil.which("hermes") is not None)

    def ask(self, system: str, question: str, timeout: int = 90) -> str:
        return ask_llm(system, question, api_key=self._api_key, model=self._model,
                        timeout=timeout, base_url=self._base_url)

    def run_action(self, message: str, timeout: int = 120) -> str:
        return ask_hermes_agent(message, timeout=timeout)


class _CLIProvider(AIProvider):
    binary = ""

    def _build_argv(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def is_reachable(self) -> bool:
        return shutil.which(self.binary) is not None

    def ask(self, system: str, question: str, timeout: int = 90) -> str:
        prompt = f"{system}\n\n{question}" if system else question
        return self._run(prompt, timeout)

    def run_action(self, message: str, timeout: int = 120) -> str:
        return self._run(message, timeout)

    def _run(self, prompt: str, timeout: int) -> str:
        if not shutil.which(self.binary):
            return (f"({self.binary} not found on PATH — install it, or pick a "
                    f"different AI provider in Settings)")
        try:
            r = subprocess.run(self._build_argv(prompt), capture_output=True, text=True,
                               timeout=timeout)
            out = (r.stdout or "") + (r.stderr or "")
            return out.strip() or f"({self.display_name} returned no output)"
        except subprocess.TimeoutExpired:
            return f"({self.display_name} timed out)"
        except Exception as e:
            return f"({self.display_name} error: {e})"


class ClaudeCodeProvider(_CLIProvider):
    id = "claude_code"
    display_name = "Claude Code"
    binary = "claude"

    def _build_argv(self, prompt: str) -> list[str]:
        return ["claude", "-p", prompt, "--output-format", "text"]


class OpenCodeProvider(_CLIProvider):
    id = "opencode"
    display_name = "opencode"
    binary = "opencode"

    def _build_argv(self, prompt: str) -> list[str]:
        return ["opencode", "run", prompt]


class GeminiCLIProvider(_CLIProvider):
    id = "gemini_cli"
    display_name = "Gemini CLI"
    binary = "gemini"

    def _build_argv(self, prompt: str) -> list[str]:
        return ["gemini", "-p", prompt]


PROVIDER_CLASSES = {
    "hermes": HermesProvider,
    "claude_code": ClaudeCodeProvider,
    "opencode": OpenCodeProvider,
    "gemini_cli": GeminiCLIProvider,
}

PROVIDER_CHOICES = [
    ("Hermes (Nous LLM + agent)", "hermes"),
    ("Claude Code", "claude_code"),
    ("opencode", "opencode"),
    ("Gemini CLI", "gemini_cli"),
]


def get_provider(provider_id: str, *, nous_api_key: str | None = None,
                  model: str | None = None, nous_base_url: str | None = None) -> AIProvider:
    cls = PROVIDER_CLASSES.get(provider_id, HermesProvider)
    if cls is HermesProvider:
        return HermesProvider(api_key=nous_api_key, model=model, base_url=nous_base_url)
    return cls()


def display_name(provider_id: str) -> str:
    """Short display label for a provider id -- e.g. "Hermes", "Claude Code",
    "opencode", "Gemini CLI". PROVIDER_CHOICES' labels carry extra
    parenthetical text ("Hermes (Nous LLM + agent)") meant for the Settings
    RadioSet, not compact enough for a pane title or an Input placeholder
    (the Dashboard's Hermes card, the Ctrl+K quick-ask popup). Reads
    `display_name` straight off the class -- no instantiation needed."""
    return PROVIDER_CLASSES.get(provider_id, HermesProvider).display_name


def any_provider_reachable(nous_api_key: str | None = None, nous_base_url: str | None = None) -> bool:
    return any(get_provider(pid, nous_api_key=nous_api_key, nous_base_url=nous_base_url).is_reachable()
              for pid in PROVIDER_CLASSES)
