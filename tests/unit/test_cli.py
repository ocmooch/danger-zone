"""CLI surface tests — verify every documented subcommand is registered."""

from __future__ import annotations

import contextlib
from unittest import mock

from typer.testing import CliRunner

from ff_pipeline import __version__
from ff_pipeline.cli import app

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_exposes_top_level_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "init",
        "run",
        "backfill",
        "rescore",
        "status",
        "verify",
        "serve",
        "export",
        "backup",
        "cookie",
        "migrate",
    ):
        assert cmd in result.stdout, f"missing subcommand in --help: {cmd}"


def test_cookie_sub_app_exposes_set_and_test() -> None:
    result = runner.invoke(app, ["cookie", "--help"])
    assert result.exit_code == 0
    assert "set" in result.stdout
    assert "test" in result.stdout


def test_migrate_sub_app_exposes_up_down_status() -> None:
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0
    for cmd in ("up", "down", "status"):
        assert cmd in result.stdout


def test_run_command_help_lists_sleeper_as_source() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "sleeper" in result.stdout


def test_backfill_command_help_lists_start_end_source() -> None:
    result = runner.invoke(app, ["backfill", "--help"])
    assert result.exit_code == 0
    for flag in ("--start", "--end", "--season", "--source", "--week", "--force"):
        assert flag in result.stdout, f"missing flag in backfill --help: {flag}"


def test_verify_command_help_lists_sweep_mode() -> None:
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    for flag in ("--player", "--season", "--week", "--sweep"):
        assert flag in result.stdout, f"missing flag in verify --help: {flag}"


def test_rescore_command_help_lists_dry_run() -> None:
    result = runner.invoke(app, ["rescore", "--help"])
    assert result.exit_code == 0
    assert "--season" in result.stdout
    assert "--dry-run" in result.stdout


def test_scoring_load_help_lists_season_override() -> None:
    result = runner.invoke(app, ["scoring", "load", "--help"])
    assert result.exit_code == 0
    for flag in ("--csv", "--season", "--fixtures-dir"):
        assert flag in result.stdout, f"missing flag in scoring load --help: {flag}"


@contextlib.contextmanager
def _patch_run_machinery():
    """Patch out everything `run` touches except the source dispatch.

    The three `_run_*` helpers are attached to one parent mock so their
    call order is recorded in `manager.mock_calls`; tests assert which
    sources ran and in what order, without a database or network. The DB
    machinery is patched at its source modules because `run_cmd` imports
    those names lazily inside the function body, not at module scope.
    """
    manager = mock.Mock()
    with (
        mock.patch("ff_pipeline.cli._bootstrap_settings_and_logging"),
        mock.patch("ff_pipeline.settings.get_settings"),
        mock.patch("ff_pipeline.repository.database.create_app_engine"),
        mock.patch("sqlalchemy.orm.Session"),
        mock.patch("ff_pipeline.cli._run_nflverse", manager.nflverse),
        mock.patch("ff_pipeline.cli._run_nfl_com", manager.nfl_com),
        mock.patch("ff_pipeline.cli._run_sleeper", manager.sleeper),
        mock.patch("ff_pipeline.cli._run_team_defense", manager.team_defense),
    ):
        yield manager


def _sources_run(manager: mock.Mock) -> list[str]:
    """Ordered list of source names from the parent mock's call log."""
    return [call[0] for call in manager.mock_calls]


def test_run_no_source_sequences_all_three_in_order() -> None:
    with _patch_run_machinery() as manager:
        result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.stdout
    # team_defense runs last: it matches against the DEF players the NFL.com
    # roster sync creates earlier in the sequence.
    assert _sources_run(manager) == ["nflverse", "nfl_com", "sleeper", "team_defense"]


def test_run_single_source_runs_only_that_source() -> None:
    for src in ("nflverse", "nfl_com", "sleeper", "team_defense"):
        with _patch_run_machinery() as manager:
            result = runner.invoke(app, ["run", "--source", src])
        assert result.exit_code == 0, result.stdout
        assert _sources_run(manager) == [src]


def test_run_unknown_source_exits_with_stub_code() -> None:
    with _patch_run_machinery() as manager:
        result = runner.invoke(app, ["run", "--source", "espn"])
    # _stub exits EX_USAGE (64); no source helper should have run.
    assert result.exit_code == 64
    assert _sources_run(manager) == []
