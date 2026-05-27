"""Typer-based CLI entry point.

For M1 this wires the ``init`` subcommand against the alembic migration
runner. M2 fills in stubs for every other operations command.
"""

from __future__ import annotations

import os

import typer

from ff_pipeline import __version__

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
    """ff-pipeline — see `ff-pipeline --help` for available commands."""


@app.command("init")
def init_cmd() -> None:
    """Create the database (if missing) and migrate to the latest schema.

    Idempotent — safe to re-run. On SQLite, the parent directory is
    created automatically; for PostgreSQL the database must already exist
    (creating one requires elevated credentials we don't carry).
    """
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head

    # Resolve DATABASE_URL from env (settings module lands in M2).
    database_url = os.environ.get("DATABASE_URL", "sqlite:///./data/fantasy.db")

    typer.echo(f"Migrating database: {database_url}")
    engine = create_app_engine(database_url)
    try:
        upgrade_to_head(engine=engine)
    finally:
        engine.dispose()
    typer.echo("Database is at latest revision.")


if __name__ == "__main__":
    app()
