"""CLI surface tests — verify every documented subcommand is registered."""

from __future__ import annotations

import contextlib
import re
from datetime import date
from unittest import mock

from typer.testing import CliRunner

from ff_pipeline import __version__
from ff_pipeline.cli import _default_run_season, _nfl_week_one_start, _resolve_current_week, app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI styling from Rich-rendered help.

    Typer renders help through Rich, which injects color escape codes — and can
    split a styled flag like ``--csv`` across them — whenever the output is
    treated as a color terminal. That happens under CI's forced-color env but
    not a local non-TTY run, so flag substring checks must compare plain text.
    """
    return _ANSI_RE.sub("", text)


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
    stdout = _plain(result.stdout)
    for flag in ("--start", "--end", "--season", "--source", "--week", "--force"):
        assert flag in stdout, f"missing flag in backfill --help: {flag}"


def test_verify_command_help_lists_sweep_mode() -> None:
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    stdout = _plain(result.stdout)
    for flag in ("--player", "--season", "--week", "--sweep", "--reconcile"):
        assert flag in stdout, f"missing flag in verify --help: {flag}"


def test_rescore_command_help_lists_dry_run() -> None:
    result = runner.invoke(app, ["rescore", "--help"])
    assert result.exit_code == 0
    stdout = _plain(result.stdout)
    assert "--season" in stdout
    assert "--dry-run" in stdout


def test_avatars_command_help_lists_season_range() -> None:
    result = runner.invoke(app, ["avatars", "--help"])
    assert result.exit_code == 0
    stdout = _plain(result.stdout)
    for opt in ("--start", "--end", "--season"):
        assert opt in stdout


def test_scoring_load_help_lists_season_override() -> None:
    result = runner.invoke(app, ["scoring", "load", "--help"])
    assert result.exit_code == 0
    stdout = _plain(result.stdout)
    for flag in ("--csv", "--season", "--fixtures-dir"):
        assert flag in stdout, f"missing flag in scoring load --help: {flag}"


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
        mock.patch("ff_pipeline.cli.run_preflight"),
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


def test_run_no_source_sequences_defaults_in_order() -> None:
    with _patch_run_machinery() as manager:
        result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.stdout
    # NFL.com is defunct (S2): it no longer runs by default. team_defense
    # still runs last, after the player rows exist.
    assert _sources_run(manager) == ["nflverse", "sleeper", "team_defense"]


def test_run_source_nfl_com_still_runs_explicitly() -> None:
    with _patch_run_machinery() as manager:
        result = runner.invoke(app, ["run", "--source", "nfl_com"])
    assert result.exit_code == 0, result.stdout
    assert _sources_run(manager) == ["nfl_com"]


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
        mock.patch("ff_pipeline.cli.run_preflight"),
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


@contextlib.contextmanager
def _patch_projection_backfill(backfill: mock.Mock):
    """Patch out everything `backfill-projections` touches except the backfill call.

    The command imports these names lazily inside its body, so they are patched
    at their source modules.
    """
    settings = mock.Mock(nfl_league_id="L1", league_start_year=2010)
    with (
        mock.patch("ff_pipeline.cli._bootstrap_settings_and_logging"),
        mock.patch("ff_pipeline.settings.get_settings", return_value=settings),
        mock.patch("ff_pipeline.repository.database.create_app_engine"),
        mock.patch("sqlalchemy.orm.Session"),
        mock.patch("ff_pipeline.projection_backfill.run_projection_backfill", backfill),
    ):
        yield


def _backfill_result(*, failed: int = 0) -> mock.Mock:
    return mock.Mock(
        per_week=(),
        fetched=1,
        skipped=0,
        failed=failed,
        projections_added=5,
        projections_updated=0,
    )


def test_backfill_projections_current_season_targets_only_the_run_season() -> None:
    backfill = mock.Mock(return_value=_backfill_result())
    with (
        _patch_projection_backfill(backfill),
        mock.patch("ff_pipeline.cli._default_run_season", return_value=2026),
    ):
        result = runner.invoke(app, ["backfill-projections", "--current-season"])
    assert result.exit_code == 0, result.stdout
    assert backfill.call_args.kwargs["start_year"] == 2026
    assert backfill.call_args.kwargs["end_year"] == 2026
    assert backfill.call_args.kwargs["skip_populated_weeks"] is True
    assert backfill.call_args.kwargs["matchups_only"] is True
    assert backfill.call_args.kwargs["refresh_latest_week"] is True


def test_backfill_projections_default_still_walks_from_league_start() -> None:
    backfill = mock.Mock(return_value=_backfill_result())
    with _patch_projection_backfill(backfill):
        result = runner.invoke(app, ["backfill-projections", "--end", "2025"])
    assert result.exit_code == 0, result.stdout
    assert backfill.call_args.kwargs["start_year"] == 2010
    assert backfill.call_args.kwargs["end_year"] == 2025
    assert backfill.call_args.kwargs["matchups_only"] is False
    assert backfill.call_args.kwargs["refresh_latest_week"] is False


def test_backfill_projections_current_season_rejects_explicit_years() -> None:
    backfill = mock.Mock()
    for extra in (["--season", "2025"], ["--start", "2024"], ["--end", "2025"]):
        with _patch_projection_backfill(backfill):
            result = runner.invoke(app, ["backfill-projections", "--current-season", *extra])
        assert result.exit_code == 2, extra
    backfill.assert_not_called()


def test_backfill_projections_failed_week_exits_1() -> None:
    backfill = mock.Mock(return_value=_backfill_result(failed=1))
    with (
        _patch_projection_backfill(backfill),
        mock.patch("ff_pipeline.cli._default_run_season", return_value=2026),
    ):
        result = runner.invoke(app, ["backfill-projections", "--current-season"])
    assert result.exit_code == 1


def test_nfl_week_one_starts_the_wednesday_before_the_opener() -> None:
    # Sleeper's season_start_date for 2026; openers Thu 2023-09-07, 2024-09-05, 2025-09-04.
    assert _nfl_week_one_start(2026) == date(2026, 9, 9)
    assert _nfl_week_one_start(2025) == date(2025, 9, 3)
    assert _nfl_week_one_start(2024) == date(2024, 9, 4)
    assert _nfl_week_one_start(2023) == date(2023, 9, 6)


def test_current_week_matches_captured_sleeper_state() -> None:
    # Captured /state/nfl: Mon 2026-11-09 -> week 9; Wed 2026-11-11 (UTC) -> week 10.
    assert _resolve_current_week(2026, date(2026, 11, 9)) == 9
    assert _resolve_current_week(2026, date(2026, 11, 10)) == 9
    assert _resolve_current_week(2026, date(2026, 11, 11)) == 10


def test_current_week_edges() -> None:
    assert _resolve_current_week(2026, date(2026, 9, 8)) == 1  # pre-season
    assert _resolve_current_week(2026, date(2026, 9, 9)) == 1
    assert _resolve_current_week(2026, date(2026, 9, 16)) == 2
    assert _resolve_current_week(2026, date(2027, 1, 5)) == 17  # January tail of the season
    assert _resolve_current_week(2026, date(2027, 2, 20)) == 18  # clamped
    assert _resolve_current_week(2026, date(2027, 3, 1)) == 1  # season over
    assert _resolve_current_week(2027, date(2026, 11, 11)) == 1  # future season
