"""Typer-based CLI entry point for ``ff-pipeline``.

Every subcommand from docs/08_OPERATIONS.md is wired here. M1 implements
``init``; subsequent milestones replace the stubs with real
orchestration code. The stubs are not silent no-ops — they print a
``[stub]`` line and exit non-zero so cron / scripts surface them
during the implementation phase rather than appearing to succeed.
"""

from __future__ import annotations

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
    verify: bool = typer.Option(False, "--verify", help="Run data-quality checks at end."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen; don't write."),
) -> None:
    """Full sync from all sources (or just one with --source)."""
    _bootstrap_settings_and_logging()
    _ = (source, verify, dry_run)
    _stub("run", "M4-M8")


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
