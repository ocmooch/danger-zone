"""Settings module — env loading + validation + cookie redaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ff_pipeline.settings import Settings, SettingsError, load_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=False)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip every relevant env var and point the .env loader at tmp_path.

    Used by tests via ``@pytest.mark.usefixtures("isolated_env")`` so we
    don't take the fixture as an unused parameter."""
    for var in (
        "NFL_LEAGUE_ID",
        "NFL_COOKIE",
        "LEAGUE_START_YEAR",
        "DATABASE_URL",
        "API_HOST",
        "API_PORT",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "LOG_DIR",
        "NFL_COM_DELAY_SECONDS",
        "SLEEPER_REQUESTS_PER_MIN",
        "NFLVERSE_CACHE_DIR",
        "SCORING_VERIFY_TOLERANCE",
        "SAVE_RAW_HTML",
    ):
        monkeypatch.delenv(var, raising=False)

    env_path = tmp_path / ".env"
    monkeypatch.setattr(
        "ff_pipeline.settings.Settings.model_config",
        {
            "env_file": env_path,
            "env_file_encoding": "utf-8",
            "case_sensitive": False,
            "extra": "ignore",
        },
    )
    return env_path


@pytest.mark.usefixtures("isolated_env")
def test_required_vars_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("NFL_COOKIE", "fake-cookie-value")
    monkeypatch.setenv("LEAGUE_START_YEAR", "2014")

    s = Settings()  # type: ignore[call-arg]
    assert s.nfl_league_id == "12345"
    assert s.nfl_cookie.get_secret_value() == "fake-cookie-value"
    assert s.league_start_year == 2014


@pytest.mark.usefixtures("isolated_env")
def test_missing_required_raises_settings_error() -> None:
    with pytest.raises(SettingsError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert "NFL_LEAGUE_ID" in msg
    assert "NFL_COOKIE" in msg


@pytest.mark.usefixtures("isolated_env")
def test_partial_required_raises_settings_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("LEAGUE_START_YEAR", "2014")
    with pytest.raises(SettingsError) as excinfo:
        load_settings()
    assert "NFL_COOKIE" in str(excinfo.value)


@pytest.mark.usefixtures("isolated_env")
def test_cookie_redacted_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("NFL_COOKIE", "supersecret-cookie-value")
    monkeypatch.setenv("LEAGUE_START_YEAR", "2014")
    s = Settings()  # type: ignore[call-arg]
    rep = repr(s)
    assert "supersecret-cookie-value" not in rep
    assert "SecretStr" in rep or "**" in rep


@pytest.mark.usefixtures("isolated_env")
def test_unreasonable_start_year_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("NFL_COOKIE", "x")
    monkeypatch.setenv("LEAGUE_START_YEAR", "1900")
    with pytest.raises(SettingsError):
        load_settings()


@pytest.mark.usefixtures("isolated_env")
def test_defaults_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("NFL_COOKIE", "x")
    monkeypatch.setenv("LEAGUE_START_YEAR", "2014")
    s = Settings()  # type: ignore[call-arg]
    assert s.api_host == "127.0.0.1"
    assert s.api_port == 8000
    assert s.log_level == "INFO"
    assert s.log_format == "json"
    assert s.scoring_verify_tolerance == 0.1
    assert s.save_raw_html is False
