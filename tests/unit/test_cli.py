"""CLI surface tests — verify every documented subcommand is registered."""

from __future__ import annotations

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
