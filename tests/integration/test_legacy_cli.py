"""``ff-pipeline legacy …`` commands and the preflight on writing commands."""

from __future__ import annotations

import sqlite3
import sys
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from ff_pipeline import settings as settings_module
from ff_pipeline.cli import app
from tests.integration.test_legacy_guard import _build

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = _build(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    fake = settings_module.Settings(
        nfl_league_id="L",
        nfl_cookie=SecretStr("fake"),
        league_start_year=2015,
        database_url=f"sqlite:///{db_path}",
        log_dir=log_dir,
        log_format="console",
    )
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(settings_module, "get_settings", lambda: fake)
    return db_path


@pytest.mark.usefixtures("db")
def test_status_and_verify() -> None:
    status = runner.invoke(app, ["legacy", "status"])
    assert status.exit_code == 0, status.output
    assert "Seal: seal-v1" in status.output
    assert "Sealed seasons: 2015" in status.output
    assert "Lock: locked" in status.output

    for args in (["legacy", "verify"], ["legacy", "verify", "--quick"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert "PASSED" in result.output


def test_amend_runs_the_command_after_double_dash(db: Path, tmp_path: Path) -> None:
    code = (
        "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]);"
        " c.execute('UPDATE matchups SET team_score = 99 WHERE matchup_id = 1');"
        " c.commit(); c.close()"
    )
    result = runner.invoke(
        app,
        [
            "legacy",
            "amend",
            "--reason",
            "cli test",
            "--backup-dir",
            str(tmp_path / "bk"),
            "--",
            sys.executable,
            "-c",
            code,
            str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "seal-v1 -> seal-v2" in result.output
    assert "changed pk 1" in result.output


def test_preflight_blocks_writing_commands_on_mismatch(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER legacy_guard_matchups_delete")
    conn.commit()
    conn.close()
    result = runner.invoke(app, ["rescore", "--season", "2015"])
    assert result.exit_code == 1
    assert "legacy preflight failed" in result.output
    assert "legacy_guard_matchups_delete is missing" in result.output


def test_verify_exits_1_on_changed_data(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER legacy_guard_owners_update")
    conn.execute("UPDATE owners SET display_name = 'Z' WHERE owner_id = 2")
    conn.commit()
    conn.close()
    result = runner.invoke(app, ["legacy", "verify"])
    assert result.exit_code == 1
    assert "owners" in result.output and "changed pk 2" in result.output
