"""Typer-based CLI entry point for ``ff-pipeline``.

Every subcommand from docs/08_OPERATIONS.md is wired here. M1 implements
``init``; subsequent milestones replace the stubs with real
orchestration code. The stubs are not silent no-ops — they print a
``[stub]`` line and exit non-zero so cron / scripts surface them
during the implementation phase rather than appearing to succeed.
"""

from __future__ import annotations

# typer reads parameter annotations at runtime (via get_type_hints) to
# resolve option types, so Path must be imported eagerly — not inside
# TYPE_CHECKING.
from pathlib import Path  # noqa: TC003

import typer

from ff_pipeline import __version__

# ---------------------------------------------------------------------------
# Root app
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="ff-pipeline",
    help="Personal fantasy football data aggregation pipeline (Phase 1).",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """ff-pipeline — see ``ff-pipeline --help`` for the full command list."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub(name: str, milestone: str) -> None:
    """Render a consistent [stub] line and exit non-zero. Stubs are kept
    visible so cron output / shell scripts notice them instead of silently
    succeeding during the build-out phase."""
    typer.secho(f"[stub] '{name}' is not implemented yet (lands in {milestone}).", fg="yellow")
    raise typer.Exit(code=64)  # EX_USAGE


def _bootstrap_settings_and_logging() -> None:
    """Load settings + configure structlog. Surfaces a clean error if
    .env is missing/incomplete instead of pydantic's raw traceback."""
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.settings import SettingsError, get_settings

    try:
        settings = get_settings()
    except SettingsError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=4) from exc  # EX_CONFIG
    configure_logging(settings)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command("init")
def init_cmd() -> None:
    """Create the database (if missing) and migrate to the latest schema.

    Idempotent. On SQLite, the parent directory is created automatically;
    for PostgreSQL the database must already exist.
    """
    _bootstrap_settings_and_logging()

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    typer.echo(f"Migrating database: {settings.database_url}")
    engine = create_app_engine(settings.database_url)
    try:
        upgrade_to_head(engine=engine)
    finally:
        engine.dispose()
    typer.echo("Database is at latest revision.")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command("run")
def run_cmd(
    source: str | None = typer.Option(
        None,
        "--source",
        help="Sync only one source: nflverse | nfl_com | sleeper.",
    ),
    season: int | None = typer.Option(
        None,
        "--season",
        help="Restrict to a single season year (default: current calendar year).",
    ),
    verify: bool = typer.Option(False, "--verify", help="Run data-quality checks at end."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen; don't write."),
) -> None:
    """Full sync from all sources (or just one with --source)."""
    _bootstrap_settings_and_logging()
    _ = (verify, dry_run)

    if source is None:
        _stub("run (multi-source)", "M5-M8")
    if source != "nflverse":
        _stub(f"run --source {source}", "M5 (nfl_com) / M6 (sleeper)")

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.runner import run_nflverse
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    target_year = season or datetime.now().year

    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            result = run_nflverse(ss, seasons=[target_year])
            ss.commit()
    finally:
        engine.dispose()

    typer.echo(
        f"nflverse: players +{result.players_added} ~{result.players_updated}, "
        f"stats +{result.stats_added} ~{result.stats_updated} "
        f"({result.duration_ms} ms)"
    )


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


@app.command("backfill")
def backfill_cmd(
    start: int | None = typer.Option(None, "--start", help="Earliest season year to backfill."),
    season: int | None = typer.Option(None, "--season", help="Backfill only this season."),
) -> None:
    """Pull historical seasons (resumable, idempotent)."""
    _bootstrap_settings_and_logging()
    _ = (start, season)
    _stub("backfill", "M9")


# ---------------------------------------------------------------------------
# rescore
# ---------------------------------------------------------------------------


@app.command("rescore")
def rescore_cmd(
    season: int | None = typer.Option(None, "--season", help="Rescore only this season."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report diffs; don't write."),
) -> None:
    """Recompute league points from raw stats using current scoring rules."""
    _bootstrap_settings_and_logging()
    _ = (season, dry_run)
    _stub("rescore", "M3 + M9")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command("status")
def status_cmd(
    verbose: bool = typer.Option(False, "--verbose", help="Include recent errors."),
) -> None:
    """Show pipeline health, last run, per-source status."""
    _bootstrap_settings_and_logging()
    _ = verbose
    _stub("status", "M10")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@app.command("verify")
def verify_cmd(
    player: str = typer.Option(..., "--player", help="Player name (e.g., 'Lamar Jackson')."),
    season: int = typer.Option(..., "--season", help="Season year."),
    week: int = typer.Option(..., "--week", help="Week number."),
) -> None:
    """Cross-check our scoring vs. NFL.com's stored point total."""
    _bootstrap_settings_and_logging()
    _ = (player, season, week)
    _stub("verify", "M9")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command("serve")
def serve_cmd(
    reload: bool = typer.Option(False, "--reload", help="Dev mode with auto-reload."),
) -> None:
    """Start the FastAPI read API."""
    _bootstrap_settings_and_logging()
    _ = reload
    _stub("serve", "M8")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@app.command("export")
def export_cmd(
    table: str = typer.Option(..., "--table", help="Table name to dump."),
    fmt: str = typer.Option("csv", "--format", help="Output format: csv | json."),
) -> None:
    """Dump a table for ad-hoc analysis."""
    _bootstrap_settings_and_logging()
    _ = (table, fmt)
    _stub("export", "M10")


# ---------------------------------------------------------------------------
# cookie sub-app
# ---------------------------------------------------------------------------

cookie_app = typer.Typer(
    name="cookie",
    help="Manage the NFL.com session cookie.",
    no_args_is_help=True,
)
app.add_typer(cookie_app, name="cookie")


@cookie_app.command("set")
def cookie_set_cmd() -> None:
    """Refresh the NFL.com cookie (interactive prompt; validates before saving)."""
    _bootstrap_settings_and_logging()
    _stub("cookie set", "M5")


@cookie_app.command("test")
def cookie_test_cmd() -> None:
    """Verify the current cookie works (one auth-check request to NFL.com)."""
    _bootstrap_settings_and_logging()
    _stub("cookie test", "M5")


# ---------------------------------------------------------------------------
# scoring sub-app
# ---------------------------------------------------------------------------

scoring_app = typer.Typer(
    name="scoring",
    help="Manage the league's scoring rules.",
    no_args_is_help=True,
)
app.add_typer(scoring_app, name="scoring")


@scoring_app.command("load")
def scoring_load_cmd(
    csv: Path = typer.Option(  # noqa: B008  (typer-idiomatic)
        ...,
        "--csv",
        help="Path to the league's scoring-rules CSV (NFL.com /settings export).",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    fixtures_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--fixtures-dir",
        help="Directory to copy the CSV into for the M9 verifier (default: tests/fixtures/scoring_rules).",
    ),
) -> None:
    """Parse a league settings export, upsert league/season/scoring_rules rows.

    Idempotent: re-running the same CSV updates ``points_per_unit`` etc.
    in place but never duplicates rules. The CSV is preserved in
    ``fixtures_dir`` so the M9 scoring verifier has a canonical copy.
    """

    _bootstrap_settings_and_logging()

    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.scoring.scraper import (
        ScoringParseError,
        apply_settings_to_db,
        parse_settings_csv,
    )
    from ff_pipeline.settings import PROJECT_ROOT, get_settings

    settings = get_settings()
    try:
        parsed = parse_settings_csv(csv)
    except ScoringParseError as exc:
        typer.secho(f"Failed to parse {csv}: {exc}", fg="red", err=True)
        raise typer.Exit(code=65) from exc  # EX_DATAERR

    if parsed.league_id != settings.nfl_league_id:
        typer.secho(
            f"League ID in CSV ({parsed.league_id}) != .env NFL_LEAGUE_ID "
            f"({settings.nfl_league_id}). Refusing to load.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=65)

    target_fixtures = fixtures_dir or (PROJECT_ROOT / "tests" / "fixtures" / "scoring_rules")
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as session:
            counts = apply_settings_to_db(
                session,
                parsed,
                source_path=csv,
                fixtures_dir=target_fixtures,
            )
            session.commit()
    finally:
        engine.dispose()

    typer.echo(
        f"Loaded {len(parsed.rules)} rules for league={parsed.league_id} "
        f"season={parsed.season_year}: +{counts.rows_added} added, "
        f"~{counts.rows_updated} updated."
    )


# ---------------------------------------------------------------------------
# migrate sub-app
# ---------------------------------------------------------------------------

migrate_app = typer.Typer(
    name="migrate",
    help="Database migration helpers (thin wrapper around alembic).",
    no_args_is_help=True,
)
app.add_typer(migrate_app, name="migrate")


@migrate_app.command("up")
def migrate_up_cmd() -> None:
    """Run pending alembic migrations to head."""
    _bootstrap_settings_and_logging()

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    engine = create_app_engine(settings.database_url)
    try:
        upgrade_to_head(engine=engine)
    finally:
        engine.dispose()
    typer.echo("Database is at latest revision.")


@migrate_app.command("down")
def migrate_down_cmd(
    rev: str = typer.Option(..., "--rev", help="Target revision (e.g. -1 to step back once)."),
) -> None:
    """Roll back to a specific alembic revision."""
    _bootstrap_settings_and_logging()
    _ = rev
    _stub("migrate down", "M10")


@migrate_app.command("status")
def migrate_status_cmd() -> None:
    """Show the current alembic revision."""
    _bootstrap_settings_and_logging()

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import current_revision
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    engine = create_app_engine(settings.database_url)
    try:
        current_revision(engine=engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    app()
