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
    assert s.nfl_cookie is not None
    assert s.nfl_cookie.get_secret_value() == "fake-cookie-value"
    assert s.require_nfl_cookie() == "fake-cookie-value"
    assert s.league_start_year == 2014


@pytest.mark.usefixtures("isolated_env")
def test_missing_required_raises_settings_error() -> None:
    with pytest.raises(SettingsError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert "NFL_LEAGUE_ID" in msg
    assert "NFL_COOKIE" not in msg  # optional since S2 (NFL.com is defunct)


@pytest.mark.usefixtures("isolated_env")
def test_missing_cookie_is_allowed_but_required_by_nfl_com(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("LEAGUE_START_YEAR", "2014")
    s = load_settings()
    assert s.nfl_cookie is None
    with pytest.raises(SettingsError, match="NFL_COOKIE"):
        s.require_nfl_cookie()


@pytest.mark.usefixtures("isolated_env")
def test_blank_cookie_and_league_id_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("LEAGUE_START_YEAR", "2014")
    monkeypatch.setenv("NFL_COOKIE", "   ")
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", "")
    s = load_settings()
    assert s.nfl_cookie is None
    assert s.sleeper_league_id is None
    with pytest.raises(SettingsError, match="SLEEPER_LEAGUE_ID"):
        s.require_sleeper_league_id()


@pytest.mark.usefixtures("isolated_env")
def test_sleeper_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("LEAGUE_START_YEAR", "2014")
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", " 999 ")
    s = load_settings()
    assert s.require_sleeper_league_id() == "999"
    assert s.sleeper_raw_dir.is_absolute()
    assert s.sleeper_raw_dir.parts[-3:] == ("data", "raw", "sleeper")
    assert s.sleeper_players_refresh_hours == 24


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
