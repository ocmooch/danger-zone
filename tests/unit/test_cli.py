"""CLI surface tests — verify every documented subcommand is registered."""

from __future__ import annotations

import contextlib
from datetime import date
from unittest import mock

from typer.testing import CliRunner

from ff_pipeline import __version__
from ff_pipeline.cli import _default_run_season, app

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
    for flag in ("--player", "--season", "--week", "--sweep", "--reconcile"):
        assert flag in result.stdout, f"missing flag in verify --help: {flag}"


def test_rescore_command_help_lists_dry_run() -> None:
    result = runner.invoke(app, ["rescore", "--help"])
    assert result.exit_code == 0
    assert "--season" in result.stdout
    assert "--dry-run" in result.stdout


def test_avatars_command_help_lists_season_range() -> None:
    result = runner.invoke(app, ["avatars", "--help"])
    assert result.exit_code == 0
    for opt in ("--start", "--end", "--season"):
        assert opt in result.stdout


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


def test_run_no_source_defaults_to_previous_season_before_september() -> None:
    with (
        mock.patch("ff_pipeline.cli._default_run_season", return_value=2025),
        _patch_run_machinery() as manager,
    ):
        result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.stdout
    for call in manager.mock_calls:
        assert call.kwargs.get("target_year") == 2025 or call.kwargs.get("seasons") == [2025]


def test_run_explicit_season_overrides_safe_default() -> None:
    with (
        mock.patch("ff_pipeline.cli._default_run_season", return_value=2025),
        _patch_run_machinery() as manager,
    ):
        result = runner.invoke(app, ["run", "--season", "2026"])

    assert result.exit_code == 0, result.stdout
    for call in manager.mock_calls:
        assert call.kwargs.get("target_year") == 2026 or call.kwargs.get("seasons") == [2026]


def test_default_run_season_before_september_uses_completed_prior_year() -> None:
    assert _default_run_season(date(2026, 6, 8)) == 2025


def test_default_run_season_from_september_uses_current_year() -> None:
    assert _default_run_season(date(2026, 9, 1)) == 2026


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


@contextlib.contextmanager
def _patch_avatars_machinery(backfill: mock.Mock):
    """Patch out everything `avatars` touches except the backfill itself.

    DB/client machinery is patched at its source modules because `avatars_cmd`
    imports those names lazily inside the function body.
    """
    with (
        mock.patch("ff_pipeline.cli._bootstrap_settings_and_logging"),
        mock.patch("ff_pipeline.settings.get_settings"),
        mock.patch("ff_pipeline.repository.database.create_app_engine"),
        mock.patch("sqlalchemy.orm.Session"),
        mock.patch("ff_pipeline.crawlers.nfl_com.client.NflComClient"),
        mock.patch("ff_pipeline.crawlers.nfl_com.media.backfill_team_avatars", backfill),
    ):
        yield


def test_avatars_reports_backfill_result() -> None:
    backfill = mock.Mock(
        return_value=mock.Mock(seasons_processed=3, assets_stored=12, teams_linked=30)
    )
    with _patch_avatars_machinery(backfill):
        result = runner.invoke(app, ["avatars", "--start", "2018", "--end", "2020"])
    assert result.exit_code == 0, result.stdout
    assert backfill.call_args.kwargs["years"] == [2018, 2019, 2020]
    assert "assets stored=12" in result.stdout
    assert "teams linked=30" in result.stdout


def test_avatars_auth_failure_exits_77() -> None:
    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError

    backfill = mock.Mock(side_effect=AuthFailureError("dead cookie"))
    with _patch_avatars_machinery(backfill):
        result = runner.invoke(app, ["avatars", "--season", "2020"])
    assert result.exit_code == 77


def test_avatars_rejects_inverted_range() -> None:
    backfill = mock.Mock()
    with _patch_avatars_machinery(backfill):
        result = runner.invoke(app, ["avatars", "--start", "2021", "--end", "2019"])
    assert result.exit_code == 2
    backfill.assert_not_called()
