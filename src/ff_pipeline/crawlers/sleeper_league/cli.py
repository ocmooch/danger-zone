"""``ff-pipeline sleeper-league`` sub-commands (S2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from ff_pipeline.settings import Settings

sleeper_league_app = typer.Typer(
    name="sleeper-league",
    help="Sleeper league (2026+): capture and archive raw league payloads.",
    no_args_is_help=True,
)


def _settings() -> Settings:
    from ff_pipeline.cli import _bootstrap_settings_and_logging
    from ff_pipeline.settings import get_settings

    _bootstrap_settings_and_logging()
    return get_settings()


def _league_id(settings: Settings, override: str | None) -> str:
    from ff_pipeline.settings import SettingsError

    if override:
        return override.strip()
    try:
        return settings.require_sleeper_league_id()
    except SettingsError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=4) from exc


@sleeper_league_app.command("capture")
def capture_cmd(
    season: int | None = typer.Option(
        None, "--season", help="Season to capture (default: the newest league in the chain)."
    ),
    league_id: str | None = typer.Option(
        None, "--league-id", help="Start of the lineage walk (default: SLEEPER_LEAGUE_ID)."
    ),
    force_players: bool = typer.Option(
        False, "--force-players", help="Capture the players directory even if fresh."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Fetch and report, but write nothing (no files, no rows)."
    ),
) -> None:
    """Archive every Sleeper league endpoint for one season (raw bytes + index rows)."""
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.capture import CaptureError, capture_season
    from ff_pipeline.crawlers.sleeper_league.client import LiveSleeperLeagueSource
    from ff_pipeline.crawlers.sleeper_league.lineage import LineageError
    from ff_pipeline.legacy.cli import run_preflight
    from ff_pipeline.repository.database import create_app_engine

    settings = _settings()
    start = _league_id(settings, league_id)
    if not dry_run:
        run_preflight("sleeper-league capture")

    run_id = f"sleeper-capture-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    engine = create_app_engine(settings.database_url)
    try:
        with (
            Session(engine) as ss,
            LiveSleeperLeagueSource(requests_per_min=settings.sleeper_requests_per_min) as source,
        ):
            try:
                summary = capture_season(
                    ss,
                    source,
                    RawStore(settings.sleeper_raw_dir),
                    start_league_id=start,
                    season=season,
                    run_id=run_id,
                    now=datetime.now(UTC),
                    players_refresh_hours=settings.sleeper_players_refresh_hours,
                    force_players=force_players,
                    dry_run=dry_run,
                )
            except (CaptureError, LineageError) as exc:
                typer.secho(f"sleeper-league capture failed: {exc}", fg="red", err=True)
                raise typer.Exit(code=1) from exc
            if not dry_run:
                ss.commit()
    finally:
        engine.dispose()

    prefix = "[dry run] " if dry_run else ""
    typer.echo(
        f"{prefix}sleeper-league: season {summary.season} league {summary.league_id} "
        f"weeks 1..{summary.max_week}"
    )
    typer.echo(
        f"{prefix}requests {summary.requests}, new revisions {summary.new_revisions}, "
        f"unchanged {summary.unchanged}, new files {summary.new_files}, "
        f"non-2xx {len(summary.non_ok)}, errors {len(summary.errors)}"
        + (", players directory fresh (skipped)" if summary.players_skipped else "")
    )
    for line in summary.non_ok:
        typer.echo(f"  non-2xx: {line}")
    for line in summary.errors:
        typer.secho(f"  error: {line}", fg="red", err=True)
    if summary.errors:
        raise typer.Exit(code=1)


@sleeper_league_app.command("lineage")
def lineage_cmd(
    league_id: str | None = typer.Option(
        None, "--league-id", help="Start of the walk (default: SLEEPER_LEAGUE_ID)."
    ),
) -> None:
    """Print the season → league id chain (network only; writes nothing)."""
    from ff_pipeline.crawlers.sleeper_league.client import LiveSleeperLeagueSource
    from ff_pipeline.crawlers.sleeper_league.endpoints import league_request
    from ff_pipeline.crawlers.sleeper_league.lineage import LineageError, walk_lineage

    settings = _settings()
    start = _league_id(settings, league_id)
    with LiveSleeperLeagueSource(requests_per_min=settings.sleeper_requests_per_min) as source:

        def fetch(lid: str) -> dict[str, object] | None:
            body = source.get(league_request(lid)).json()
            return body if isinstance(body, dict) else None

        try:
            chain = walk_lineage(fetch, start)
        except LineageError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc
    for link in chain:
        typer.echo(f"{link.season}\t{link.league_id}\t{link.status or ''}")


@sleeper_league_app.command("archive")
def archive_cmd(
    endpoint: str | None = typer.Option(None, "--endpoint", help="Filter by endpoint key."),
    season: int | None = typer.Option(None, "--season"),
    week: int | None = typer.Option(None, "--week"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List archived payload revisions, newest first (read-only)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.models import SourcePayload

    settings = _settings()
    q = select(SourcePayload).order_by(
        SourcePayload.last_fetched_at.desc(), SourcePayload.payload_id.desc()
    )
    if endpoint:
        q = q.where(SourcePayload.endpoint == endpoint)
    if season is not None:
        q = q.where(SourcePayload.season_year == season)
    if week is not None:
        q = q.where(SourcePayload.week == week)
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            rows = ss.scalars(q.limit(limit)).all()
            for r in rows:
                typer.echo(
                    f"{r.payload_id}\t{r.endpoint}\t{r.season_year or ''}\t{r.week or ''}\t"
                    f"{r.http_status}\t{r.byte_size}\t{r.fetch_count}x\t"
                    f"{r.last_fetched_at:%Y-%m-%d %H:%M}\t{r.sha256[:12]}\t{r.request_url}"
                )
    finally:
        engine.dispose()


@sleeper_league_app.command("resolve-players")
def resolve_players_cmd(
    league_id: str | None = typer.Option(None, "--league-id", help="Default: SLEEPER_LEAGUE_ID."),
) -> None:
    """Dry run: resolve every league-referenced Sleeper player id (read-only, archive only)."""
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.archive_view import load_archived_league
    from ff_pipeline.crawlers.sleeper_league.client import ArchiveMissError
    from ff_pipeline.normalizer.sleeper_players import league_referenced_ids, resolve
    from ff_pipeline.repository.database import create_app_engine

    settings = _settings()
    lid = _league_id(settings, league_id)
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            try:
                data = load_archived_league(ss, RawStore(settings.sleeper_raw_dir), lid)
            except ArchiveMissError as exc:
                typer.secho(str(exc), fg="red", err=True)
                raise typer.Exit(code=1) from exc
            referenced = league_referenced_ids(
                data.rosters, data.matchups, data.transactions, data.draft_picks
            )
            res = resolve(ss, referenced, data.players, season=data.season)
            ss.rollback()  # read-only: never keep anything the resolver touched
    finally:
        engine.dispose()
    typer.echo(
        f"season {data.season}: {len(referenced)} league-referenced Sleeper ids; "
        f"resolved {len(res.resolved)} {dict(sorted(res.by_method.items()))}"
        f" (via identity links: {res.linked}); stub-needed {len(res.stub_needed)}"
    )
    for c in res.stub_needed:
        typer.echo(
            f"  stub-needed: {c.sleeper_id}\t{c.name}\t{c.position or '?'}\t"
            f"{c.nfl_team or 'FA'}\t{', '.join(c.reasons)}"
        )


@sleeper_league_app.command("map-player")
def map_player_cmd(
    sleeper_id: str = typer.Argument(..., help="Sleeper player id (from resolve-players)."),
    player_id: int = typer.Argument(..., help="Existing players.player_id to pin it to."),
    note: str = typer.Option(..., "--note", help="Why (audited in player_id_overrides.notes)."),
) -> None:
    """Pin a Sleeper player id to an existing player the fuzzy rules cannot match."""
    from sqlalchemy.orm import Session

    from ff_pipeline.legacy.cli import run_preflight
    from ff_pipeline.normalizer.sleeper_players import (
        OverrideConflictError,
        add_sleeper_override,
    )
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.models import Player

    settings = _settings()
    run_preflight("sleeper-league map-player")
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            try:
                added = add_sleeper_override(ss, sleeper_id, player_id, notes=note)
            except (ValueError, OverrideConflictError) as exc:
                typer.secho(str(exc), fg="red", err=True)
                raise typer.Exit(code=1) from exc
            player = ss.get(Player, player_id)
            name = player.name_full if player else "?"  # read before commit expires it
            ss.commit()
    finally:
        engine.dispose()
    verb = "pinned" if added else "already pinned"
    typer.echo(f"Sleeper {sleeper_id} {verb} to player {player_id} ({name}).")
