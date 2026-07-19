"""Unit tests for google_tui.app_config's load_config -- missing file,
valid TOML, and every per-field fallback-on-bad-value path. tests/conftest.py
has already redirected platformdirs to an isolated temp dir before this
module (or any google_tui module) was imported, so CONFIG_PATH here is never
the real ~/.config/google-tui file.
"""
from zoneinfo import ZoneInfo

from google_tui import app_config


def _write(text: str) -> None:
    app_config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    app_config.CONFIG_PATH.write_text(text)


def test_missing_file_returns_all_none_defaults():
    if app_config.CONFIG_PATH.exists():
        app_config.CONFIG_PATH.unlink()
    config = app_config.load_config()
    assert config == app_config.AppConfig()


def test_valid_toml_populates_all_fields():
    _write(
        'llm_model = "some/model"\n'
        'timezone = "America/Los_Angeles"\n'
        'pane_order = ["dash-weather", "events"]\n'
        "refresh_interval_minutes = 15\n"
        'searxng_url = "https://searx.example.org"\n'
    )
    config = app_config.load_config()
    assert config.llm_model == "some/model"
    assert config.timezone == "America/Los_Angeles"
    assert config.tzinfo == ZoneInfo("America/Los_Angeles")
    assert config.pane_order == ["dash-weather", "events"]
    assert config.refresh_interval_minutes == 15
    assert config.searxng_url == "https://searx.example.org"


def test_invalid_timezone_falls_back_to_none():
    _write('timezone = "Not/ARealZone"\n')
    config = app_config.load_config()
    assert config.timezone is None
    assert config.tzinfo is None


def test_malformed_toml_syntax_falls_back_to_defaults():
    _write("this is not [ valid toml\n")
    config = app_config.load_config()
    assert config == app_config.AppConfig()


def test_non_list_pane_order_is_ignored():
    _write('pane_order = "events"\n')
    config = app_config.load_config()
    assert config.pane_order is None


def test_non_positive_refresh_interval_is_ignored():
    _write("refresh_interval_minutes = 0\n")
    config = app_config.load_config()
    assert config.refresh_interval_minutes is None

    _write("refresh_interval_minutes = -5\n")
    config = app_config.load_config()
    assert config.refresh_interval_minutes is None


def test_blank_string_fields_are_treated_as_unset():
    _write('llm_model = ""\nsearxng_url = "   "\n')
    config = app_config.load_config()
    assert config.llm_model is None
    assert config.searxng_url is None
