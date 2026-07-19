"""Optional, hand-edited advanced configuration (ROADMAP P4, 2026-07-19).

Unlike Settings (settings.py, plaintext JSON the app itself writes on every
Settings-tab change), config.toml is read once at startup and the app never
writes to it -- same spirit as ~/.hermes/config.yaml, which ask.py already
reads a key out of. It exists for the handful of knobs that either have no
natural Settings-tab UI (LLM model override, timezone override, Dashboard
cycle order) or are power-user/rarely-touched (refresh interval, a SearXNG
default). See config.toml.example at the repo root for the documented
format.

A missing file, a syntax error, or a single bad field all fall back to
defaults rather than crashing startup -- this file is optional, so no value
in it should ever be load-bearing for the app to start.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from datetime import tzinfo as TzInfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import platformdirs

CONFIG_PATH = Path(platformdirs.user_config_dir("google-tui")) / "config.toml"

_logger = logging.getLogger("google_tui")


@dataclass
class AppConfig:
    llm_model: str | None = None
    timezone: str | None = None  # raw IANA name, kept for error messages
    tzinfo: TzInfo | None = None  # resolved ZoneInfo, precomputed at load
    pane_order: list[str] | None = None
    refresh_interval_minutes: int | None = None
    searxng_url: str | None = None  # fallback default; Settings.searxng_url wins if set


def load_config() -> AppConfig:
    try:
        raw = tomllib.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return AppConfig()
    except (tomllib.TOMLDecodeError, OSError) as e:
        _logger.warning("config.toml: could not read/parse (%s) -- ignoring", e)
        return AppConfig()

    config = AppConfig()

    llm_model = raw.get("llm_model")
    if isinstance(llm_model, str) and llm_model.strip():
        config.llm_model = llm_model.strip()

    timezone = raw.get("timezone")
    if isinstance(timezone, str) and timezone.strip():
        try:
            config.tzinfo = ZoneInfo(timezone.strip())
            config.timezone = timezone.strip()
        except (ZoneInfoNotFoundError, ValueError) as e:
            _logger.warning("config.toml: invalid timezone %r (%s) -- ignoring", timezone, e)

    pane_order = raw.get("pane_order")
    if isinstance(pane_order, list) and all(isinstance(pid, str) for pid in pane_order):
        config.pane_order = pane_order
    elif pane_order is not None:
        _logger.warning("config.toml: pane_order must be a list of strings -- ignoring")

    refresh_interval_minutes = raw.get("refresh_interval_minutes")
    if isinstance(refresh_interval_minutes, int) and not isinstance(refresh_interval_minutes, bool) \
            and refresh_interval_minutes > 0:
        config.refresh_interval_minutes = refresh_interval_minutes
    elif refresh_interval_minutes is not None:
        _logger.warning("config.toml: refresh_interval_minutes must be a positive integer -- ignoring")

    searxng_url = raw.get("searxng_url")
    if isinstance(searxng_url, str) and searxng_url.strip():
        config.searxng_url = searxng_url.strip()

    return config
