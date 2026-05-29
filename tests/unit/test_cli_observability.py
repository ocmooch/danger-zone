"""CLI-level tests for the M10 commands and cookie-expiration recovery flow.

Goes through ``CliRunner`` so the settings bootstrap, logging
configuration, and Typer parameter wiring all run for real. Settings
are pointed at a tmp-dir SQLite database via ``monkeypatch`` so the
tests are hermetic — they never touch the developer's ``data/`` tree.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ff_pipeline import cli as cli_module
from ff_pipeline import observability as obs_module
from ff_pipeline import settings as settings_module
from ff_pipeline.cli import app
from ff_pipeline.crawlers.nfl_com import league as league_module
from ff_pipeline.crawlers.nfl_com.client import AuthFailureError
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import PipelineRun, SourceHealth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
def hermetic_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[settings_module.Settings]:
    """Replace ``get_settings`` with a tmp-path SQLite + logs setup.

    The real ``Settings`` model is instantiated with placeholder cookie
    + league values; the only thing the CLI cares about for these tests
    is that ``database_url`` and ``log_dir`` are writable.
    """

    db_path = tmp_path / "fantasy.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Build a real Settings object so type-safety matches production.
    fake = settings_module.Settings(
        nfl_league_id="36271",
        nfl_cookie=SecretStr("fake-cookie-value"),
        league_start_year=2014,
        database_url=f"sqlite:///{db_path}",
        log_dir=log_dir,
        log_format="console",
    )

    # Initialize the DB so status/backup have something to query.
    engine = create_app_engine(fake.database_url)
    upgrade_to_head(engine=engine)
    engine.dispose()

    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(settings_module, "get_settings", lambda: fake)
    monkeypatch.setattr(cli_module, "_bootstrap_settings_and_logging", lambda: None)

    try:
        yield fake
    finally:
        # ``monkeypatch`` restores the real ``get_settings`` automatically; we
        # only need to flush any value it cached during this test.
        if hasattr(settings_module.get_settings, "cache_clear"):
            settings_module.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("hermetic_settings")
def test_status_command_runs_on_empty_db() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "ff-pipeline status" in result.stdout
    assert "Last run: (none yet" in result.stdout
    assert "leagues=0" in result.stdout


def test_status_command_renders_seeded_run(
    hermetic_settings: settings_module.Settings,
) -> None:
    engine = create_app_engine(hermetic_settings.database_url)
    with Session(engine) as ss:
        run = PipelineRun(
            started_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
            finished_at=datetime(2026, 5, 28, 8, 1, tzinfo=UTC),
            status="success",
            mode="full_sync",
        )
        ss.add(run)
        ss.flush()
        ss.add(
            SourceHealth(
                run_id=run.run_id,
                source="nflverse",
                status="success",
                rows_added=42,
                rows_updated=7,
                duration_ms=1234,
            )
        )
        ss.commit()
    engine.dispose()

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Last run: #1 status=success" in result.stdout
    assert "nflverse" in result.stdout
    assert "rows=+42~7" in result.stdout


def test_status_verbose_flag_includes_failures(
    hermetic_settings: settings_module.Settings,
) -> None:
    engine = create_app_engine(hermetic_settings.database_url)
    with Session(engine) as ss:
        ss.add(
            PipelineRun(
                started_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
                status="failed",
                mode="full_sync",
                error_summary="AuthFailureError: cookie expired",
            )
        )
        ss.commit()
    engine.dispose()

    result = runner.invoke(app, ["status", "--verbose"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Recent failures:" in result.stdout
    assert "AuthFailureError" in result.stdout


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("hermetic_settings")
def test_backup_command_writes_dated_file(tmp_path: Path) -> None:
    target = tmp_path / "explicit-backups"
    result = runner.invoke(app, ["backup", "--backup-dir", str(target), "--keep-days", "0"])
    assert result.exit_code == 0, result.stdout + result.stderr
    files = list(target.iterdir())
    assert len(files) == 1
    assert files[0].name.startswith("fantasy-")
    assert files[0].name.endswith(".db")
    # Backup must be a valid SQLite file.
    conn = sqlite3.connect(str(files[0]))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}
    finally:
        conn.close()
    assert "pipeline_runs" in tables


@pytest.mark.usefixtures("hermetic_settings")
def test_backup_command_reports_error_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        obs_module,
        "_sqlite_path_from_url",
        lambda _url: None,
    )
    # Note: when sqlite_path returns None, perform_backup raises BackupError.
    result = runner.invoke(app, ["backup", "--backup-dir", str(tmp_path / "b")])
    assert result.exit_code == 1
    assert "SQLite" in result.stderr


# ---------------------------------------------------------------------------
# Cookie-expiration recovery flow
# ---------------------------------------------------------------------------


class _FakeNflClient:
    def __enter__(self) -> _FakeNflClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.usefixtures("hermetic_settings")
def test_run_nfl_com_with_expired_cookie_exits_77(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run --source nfl_com` must surface ``AuthFailureError`` as exit code 77.

    Exercises the recovery contract from docs/08_OPERATIONS.md: cron / scripts
    detect EX_NOPERM, alert the operator, and the user re-issues a cookie via
    ``ff-pipeline cookie set``.
    """

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AuthFailureError("cookie rejected by NFL.com — refresh via `cookie set`")

    monkeypatch.setattr(
        league_module,
        "build_default_client",
        lambda _cookie, _delay: _FakeNflClient(),
    )
    monkeypatch.setattr(league_module, "run_nfl_com", _boom)

    result = runner.invoke(app, ["run", "--source", "nfl_com", "--season", "2025"])
    assert result.exit_code == 77, result.stdout + result.stderr
    assert "cookie rejected" in result.stderr


__all__ = [
    "test_backup_command_reports_error_on_postgres",
    "test_backup_command_writes_dated_file",
    "test_run_nfl_com_with_expired_cookie_exits_77",
    "test_status_command_renders_seeded_run",
    "test_status_command_runs_on_empty_db",
    "test_status_verbose_flag_includes_failures",
]
