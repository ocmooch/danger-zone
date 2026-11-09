"""``ff-pipeline owners`` sub-commands: platform user → owner crosswalk (S4)."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  (typer resolves annotations at runtime)

import typer

owners_app = typer.Typer(
    name="owners",
    help="Owner identity across platforms (Sleeper user → owners row).",
    no_args_is_help=True,
)

DEFAULT_CROSSWALK = "data/owner_platform_identities.yaml"


@owners_app.command("map")
def map_cmd(
    platform: str = typer.Option("sleeper", "--platform", help="Only 'sleeper' today."),
    crosswalk: Path | None = typer.Option(  # noqa: B008  (typer-idiomatic)
        None, "--crosswalk", help=f"Crosswalk YAML (default: {DEFAULT_CROSSWALK}, gitignored)."
    ),
    league_id: str | None = typer.Option(
        None, "--league-id", help="Sleeper league (default: SLEEPER_LEAGUE_ID)."
    ),
    apply_: bool = typer.Option(
        False, "--apply", help="Write owner_platform_identities (default: validate only)."
    ),
) -> None:
    """Validate the crosswalk against the archived league; with --apply, load it."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.cli import _bootstrap_settings_and_logging
    from ff_pipeline.crawlers.sleeper_league.archive_view import load_archived_league
    from ff_pipeline.crawlers.sleeper_league.client import ArchiveMissError
    from ff_pipeline.legacy.cli import run_preflight
    from ff_pipeline.normalizer.owner_identity import (
        CrosswalkError,
        apply,
        load_crosswalk,
        validate,
    )
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.models import Owner
    from ff_pipeline.settings import PROJECT_ROOT, SettingsError, get_settings

    _bootstrap_settings_and_logging()
    if platform != "sleeper":
        typer.secho("only --platform sleeper is supported", fg="red", err=True)
        raise typer.Exit(code=2)
    settings = get_settings()
    try:
        lid = league_id or settings.require_sleeper_league_id()
    except SettingsError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=4) from exc
    path = crosswalk or PROJECT_ROOT / DEFAULT_CROSSWALK
    try:
        cw = load_crosswalk(path)
    except (OSError, CrosswalkError) as exc:
        typer.secho(f"crosswalk: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    if apply_:
        run_preflight("owners map --apply")

    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            try:
                data = load_archived_league(ss, RawStore(settings.sleeper_raw_dir), lid)
            except ArchiveMissError as exc:
                typer.secho(str(exc), fg="red", err=True)
                raise typer.Exit(code=1) from exc
            known = set(ss.scalars(select(Owner.owner_id)))
            problems = validate(cw, data.users, data.rosters, known)
            if problems:
                typer.secho(
                    f"crosswalk INVALID for season {data.season} ({len(problems)} problems):",
                    fg="red",
                    err=True,
                )
                for p in problems:
                    typer.secho(f"  - {p}", err=True)
                raise typer.Exit(code=1)
            typer.echo(
                f"crosswalk valid: {len(cw.entries)} Sleeper users → owners for season "
                f"{data.season} ({len(data.users)} users, {len(data.rosters)} rosters archived)."
            )
            if not apply_:
                typer.echo("Validate only; re-run with --apply to load owner_platform_identities.")
                return
            leagues = {str(s) for s in ss.scalars(select(Owner.league_id).distinct())}
            franchise = next(iter(leagues)) if len(leagues) == 1 else settings.nfl_league_id
            result = apply(ss, cw, season=data.season, league_id=franchise)
            ss.commit()
    finally:
        engine.dispose()
    typer.echo(
        f"owner_platform_identities: {result.inserted} inserted, {result.updated} updated, "
        f"{result.new_owners} new owners."
    )
