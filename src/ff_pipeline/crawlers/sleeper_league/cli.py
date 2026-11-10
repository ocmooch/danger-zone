"""``ff-pipeline sleeper-league`` sub-commands (S2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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


def _capture_avatars(ss: Any, settings: Settings, league_id: str) -> Any:
    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.archive_view import load_league
    from ff_pipeline.crawlers.sleeper_league.avatars import HttpBytesFetcher, download_avatars
    from ff_pipeline.crawlers.sleeper_league.client import ArchiveSleeperLeagueSource
    from ff_pipeline.crawlers.sleeper_league.runner import FRANCHISE_LEAGUE_ID
    from ff_pipeline.normalizer.sleeper_league import avatar_urls

    data = load_league(
        ArchiveSleeperLeagueSource(ss, RawStore(settings.sleeper_raw_dir)), league_id
    )
    fetcher = HttpBytesFetcher()
    try:
        return download_avatars(
            ss,
            fetcher,
            avatar_urls(data),
            league_id=FRANCHISE_LEAGUE_ID,
            assets_root=settings.assets_dir,
        )
    finally:
        fetcher.close()


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
            avatars = None
            if not dry_run and summary.league_id:
                avatars = _capture_avatars(ss, settings, summary.league_id)
                ss.commit()
    finally:
        engine.dispose()
    if avatars is not None:
        typer.echo(
            f"avatars: {avatars.stored} new, {avatars.already} known, {avatars.failed} failed"
            + (f", {avatars.aliased} same image under another URL" if avatars.aliased else "")
        )

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
    if summary.renewed_league_id:
        typer.secho(
            f"renewed league found and archived: {summary.renewed_league_id} "
            f"(season {summary.season}). Set SLEEPER_LEAGUE_ID to it; loading the new season "
            "waits for your go-ahead (owners map, rules sign-off).",
            fg="yellow",
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


@sleeper_league_app.command("load")
def load_cmd(
    league_id: str | None = typer.Option(None, "--league-id", help="Default: SLEEPER_LEAGUE_ID."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Load inside a transaction and roll it back."
    ),
) -> None:
    """Load the archived Sleeper season into the canonical tables (offline; S5)."""
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.canonical.loader import PlatformMismatchError
    from ff_pipeline.crawlers.sleeper_league.archive_view import load_league
    from ff_pipeline.crawlers.sleeper_league.client import (
        ArchiveMissError,
        ArchiveSleeperLeagueSource,
    )
    from ff_pipeline.crawlers.sleeper_league.runner import (
        load_league_season,
        reserve_by_week_from_archive,
    )
    from ff_pipeline.legacy.cli import run_preflight
    from ff_pipeline.normalizer.sleeper_league import NormalizeError
    from ff_pipeline.normalizer.sleeper_status import (
        archived_directory_revisions,
        snapshotted_days,
    )
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.legacy_guard import SealedSeasonError

    settings = _settings()
    lid = _league_id(settings, league_id)
    if not dry_run:
        run_preflight("sleeper-league load")
    store = RawStore(settings.sleeper_raw_dir)
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            source = ArchiveSleeperLeagueSource(ss, store)
            try:
                season = load_league(source, lid).season
                reserve = reserve_by_week_from_archive(ss, store, lid, season)
                revisions = archived_directory_revisions(ss, store, skip_days=snapshotted_days(ss))
                out = load_league_season(
                    ss, source, lid, reserve_by_week=reserve, directory_revisions=revisions
                )
            except (
                ArchiveMissError,
                NormalizeError,
                PlatformMismatchError,
                SealedSeasonError,
            ) as exc:
                ss.rollback()
                typer.secho(f"sleeper-league load failed: {exc}", fg="red", err=True)
                raise typer.Exit(code=1) from exc
            if dry_run:
                ss.rollback()
            else:
                ss.commit()
    finally:
        engine.dispose()
    r = out.result
    prefix = "[dry run, rolled back] " if dry_run else ""
    typer.echo(f"{prefix}sleeper-league load: season {out.season} (season_id {r.season_id})")
    for entity, c in r.counts.items():
        typer.echo(f"  {entity:13s} {c}")
    typer.echo(
        f"  players       DEF sleeper_id stamped {len(out.prep.stamped)}, "
        f"stubs {len(out.prep.stubbed)}; rostered spans refreshed {r.spans_refreshed}"
    )
    typer.echo(f"  status        {out.status_snapshots} player-day snapshots written")
    typer.echo(
        "  records vs Sleeper: "
        + ("ok (all teams)" if not r.record_mismatches else f"{len(r.record_mismatches)} differ")
    )
    for line in r.record_mismatches:
        typer.secho(f"    {line}", fg="yellow")
    for line in r.owner_changes:
        typer.echo(f"  owner updated: {line}")
    for line in r.owner_blocked:
        typer.secho(f"  owner change NOT written: {line}", fg="yellow")
    for line in r.warnings:
        typer.secho(f"  warning: {line}", fg="yellow")


@sleeper_league_app.command("rules")
def rules_cmd(
    league_id: str | None = typer.Option(None, "--league-id", help="Default: SLEEPER_LEAGUE_ID."),
    diff: int | None = typer.Option(
        None, "--diff", help="Compare with this season's stored rules (e.g. 2025)."
    ),
    write: bool = typer.Option(
        False, "--write", help="Replace the season's scoring_rules with the derived set (D-10)."
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Exit 3 if the derived rules differ from the season's stored rules (S9 weekly job).",
    ),
) -> None:
    """Derive scoring rules from the archived league's scoring_settings (S6; offline)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.archive_view import load_league
    from ff_pipeline.crawlers.sleeper_league.client import ArchiveSleeperLeagueSource
    from ff_pipeline.crawlers.sleeper_league.runner import FRANCHISE_LEAGUE_ID
    from ff_pipeline.legacy.cli import run_preflight
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.models import ScoringRule, Season
    from ff_pipeline.scoring.sleeper_rules import (
        RuleRow,
        UnmappedScoringKeyError,
        derive_rules,
        describe,
        diff_rules,
        write_season_rules,
    )

    settings = _settings()
    lid = _league_id(settings, league_id)
    if write:
        run_preflight("sleeper-league rules --write")
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            data = load_league(
                ArchiveSleeperLeagueSource(ss, RawStore(settings.sleeper_raw_dir)), lid
            )
            try:
                rows = derive_rules(data.league.get("scoring_settings") or {})
            except UnmappedScoringKeyError as exc:
                typer.secho(f"sleeper-league rules: {exc}", fg="red", err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(f"season {data.season}: {len(rows)} rules derived")
            if check:
                stored = [
                    RuleRow(
                        r.category,
                        r.stat_key,
                        float(r.points_per_unit or 0.0),
                        float(r.unit_size or 1.0),
                        r.threshold_min,
                        r.threshold_max,
                        r.flat_points,
                    )
                    for r in ss.scalars(
                        select(ScoringRule)
                        .join(Season, Season.season_id == ScoringRule.season_id)
                        .where(Season.league_id == FRANCHISE_LEAGUE_ID, Season.year == data.season)
                    )
                ]
                d = diff_rules(stored, rows)
                if stored and d.identical:
                    typer.echo("rules unchanged")
                    return
                typer.secho(
                    "scoring rules changed (or none stored): review with "
                    "`sleeper-league rules --diff <year>` and sign off before `--write`",
                    fg="yellow",
                )
                raise typer.Exit(code=3)
            if diff is not None:
                other = [
                    RuleRow(
                        r.category,
                        r.stat_key,
                        float(r.points_per_unit or 0.0),
                        float(r.unit_size or 1.0),
                        r.threshold_min,
                        r.threshold_max,
                        r.flat_points,
                    )
                    for r in ss.scalars(
                        select(ScoringRule)
                        .join(Season, Season.season_id == ScoringRule.season_id)
                        .where(Season.league_id == FRANCHISE_LEAGUE_ID, Season.year == diff)
                    )
                ]
                d = diff_rules(other, rows)
                typer.echo(f"vs {diff}: {d.same} identical")
                for r in d.only_in_a:
                    typer.echo(f"  - only {diff}:      {describe(r)}")
                for r in d.only_in_b:
                    typer.echo(f"  + only {data.season}: {describe(r)}  [{r.raw_text}]")
            else:
                for r in rows:
                    typer.echo(f"  {describe(r)}  [{r.raw_text}]")
            if not write:
                return
            season = ss.scalars(
                select(Season).where(
                    Season.league_id == FRANCHISE_LEAGUE_ID, Season.year == data.season
                )
            ).first()
            if season is None or season.platform != "sleeper":
                typer.secho(
                    f"no Sleeper season row for {data.season}; run `sleeper-league load` first",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(code=1)
            write_season_rules(ss, season.season_id, rows)
            ss.commit()
            typer.echo(f"wrote {len(rows)} scoring_rules for season {data.season}")
    finally:
        engine.dispose()


@sleeper_league_app.command("stats")
def stats_cmd(
    league_id: str | None = typer.Option(None, "--league-id", help="Default: SLEEPER_LEAGUE_ID."),
) -> None:
    """Store Sleeper's own weekly stats as non-primary raw stats (S6-D3; offline)."""
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.archive_view import load_league
    from ff_pipeline.crawlers.sleeper_league.client import ArchiveSleeperLeagueSource
    from ff_pipeline.crawlers.sleeper_league.stats import store_weekly_stats
    from ff_pipeline.legacy.cli import run_preflight
    from ff_pipeline.repository.database import create_app_engine

    settings = _settings()
    lid = _league_id(settings, league_id)
    run_preflight("sleeper-league stats")
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            source = ArchiveSleeperLeagueSource(ss, RawStore(settings.sleeper_raw_dir))
            data = load_league(source, lid)
            weeks = sorted(data.matchups_by_week) or list(range(1, data.weeks + 1))
            out = store_weekly_stats(ss, source, season=data.season, weeks=weeks)
            ss.commit()
    finally:
        engine.dispose()
    typer.echo(
        f"sleeper-league stats: season {data.season} weeks {list(out.weeks)}: "
        f"{out.rows_added} added, {out.rows_updated} updated "
        f"({out.unresolved} lines for players not in the DB skipped)"
    )


@sleeper_league_app.command("reconcile")
def reconcile_cmd(
    season: int = typer.Option(..., "--season"),
    tolerance: float = typer.Option(0.01, "--tolerance"),
    out: str | None = typer.Option(None, "--out", help="Write the markdown report here."),
) -> None:
    """Reconstructed (nflverse) vs Sleeper official points, classified (S6; read-only)."""
    from datetime import date
    from pathlib import Path

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.sleeper_league.runner import FRANCHISE_LEAGUE_ID
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.scoring.platform_reconcile import reconcile_platform_points, render_markdown
    from ff_pipeline.scoring.verify import reconcile_team_totals

    settings = _settings()
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            rec = reconcile_platform_points(
                ss, league_id=FRANCHISE_LEAGUE_ID, season_year=season, tolerance=tolerance
            )
            teams = reconcile_team_totals(
                ss,
                league_id=FRANCHISE_LEAGUE_ID,
                season_year=season,
                weeks=list(rec.finalized_weeks) or None,
                tolerance=tolerance,
            )
    finally:
        engine.dispose()
    if rec.note:
        typer.secho(f"reconcile: {rec.note}", fg="red", err=True)
        raise typer.Exit(code=1)
    started = rec.counts()
    typer.echo(
        f"{season}: started player-weeks {sum(started.values())} "
        + ", ".join(f"{k} {v}" for k, v in sorted(started.items()))
        + f"; team totals {teams.passed}/{teams.total} match"
    )
    if out:
        Path(out).write_text(
            render_markdown(
                rec, team_totals=(teams.passed, teams.total), generated=date.today().isoformat()
            ),
            encoding="utf-8",
        )
        typer.echo(f"report written to {out}")


@sleeper_league_app.command("adopt-gsis")
def adopt_gsis_cmd(
    season: int = typer.Option(..., "--season"),
    apply: bool = typer.Option(False, "--apply", help="Write the adoptions (default: list them)."),
) -> None:
    """Give league-rostered rookies the real nflverse gsis id of their twin row (S6)."""
    from sqlalchemy.orm import Session

    from ff_pipeline.legacy.cli import run_preflight
    from ff_pipeline.normalizer.gsis_adoption import apply_adoptions, plan_adoptions
    from ff_pipeline.repository.database import create_app_engine

    settings = _settings()
    if apply:
        run_preflight("sleeper-league adopt-gsis --apply")
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            plan = plan_adoptions(ss, season=season)
            for a in plan.adoptions:
                typer.echo(
                    f"  adopt {a.gsis_id} → player {a.keep_id} {a.name} ({a.position});"
                    f" twin {a.twin_id}"
                )
            for line in plan.ambiguous:
                typer.secho(f"  ambiguous, skipped: {line}", fg="yellow")
            if plan.no_twin:
                typer.echo(f"  no nflverse twin yet: {', '.join(plan.no_twin)}")
            if apply and plan.adoptions:
                apply_adoptions(ss, plan.adoptions)
                ss.commit()
    finally:
        engine.dispose()
    verb = "adopted" if apply else "would adopt"
    typer.echo(
        f"adopt-gsis {season}: {verb} {len(plan.adoptions)}; ambiguous {len(plan.ambiguous)}"
    )
    if apply and plan.adoptions:
        typer.echo("  next: `run --source nflverse --season N`, then `rescore --season N`")


UPDATE_RULES_CHANGED = 3


@sleeper_league_app.command("update")
def update_cmd(
    if_enabled: bool = typer.Option(
        False, "--if-enabled", help="Do nothing unless SLEEPER_AUTO_LOAD is true (weekly job)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run the load as a dry run and stop."),
    league_id: str | None = typer.Option(None, "--league-id", help="Default: SLEEPER_LEAGUE_ID."),
) -> None:
    """Load and score the season from the archive (S9-D2): load, rookie ids, injury reports,
    nflverse, team defense, Sleeper stats, rules check, rescore, reconcile."""
    import subprocess
    import sys
    import time

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.archive_view import load_league
    from ff_pipeline.crawlers.sleeper_league.client import (
        ArchiveMissError,
        ArchiveSleeperLeagueSource,
    )
    from ff_pipeline.crawlers.sleeper_league.runner import FRANCHISE_LEAGUE_ID
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.models import Season
    from ff_pipeline.settings import PROJECT_ROOT

    settings = _settings()
    if if_enabled and not settings.sleeper_auto_load:
        typer.echo("sleeper-league update: off (SLEEPER_AUTO_LOAD is false); nothing to do")
        return
    lid = _league_id(settings, league_id)
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            try:
                year = load_league(
                    ArchiveSleeperLeagueSource(ss, RawStore(settings.sleeper_raw_dir)), lid
                ).season
            except ArchiveMissError as exc:
                typer.secho(f"sleeper-league update: {exc}", fg="red", err=True)
                raise typer.Exit(code=1) from exc
            row = ss.scalars(
                select(Season).where(Season.league_id == FRANCHISE_LEAGUE_ID, Season.year == year)
            ).first()
            sealed = bool(row and row.is_sealed)
    finally:
        engine.dispose()
    if sealed:
        typer.echo(f"sleeper-league update: {year} is sealed; nothing to update")
        return

    y = str(year)
    report = PROJECT_ROOT / "data" / "reports" / f"{y}-reconcile.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    steps: list[tuple[str, list[str]]] = [("load", ["sleeper-league", "load", "--league-id", lid])]
    if dry_run:
        steps = [("load (dry run)", ["sleeper-league", "load", "--league-id", lid, "--dry-run"])]
    else:
        steps += [
            ("adopt-gsis", ["sleeper-league", "adopt-gsis", "--season", y, "--apply"]),
            ("injury-reports", ["injury-reports", "--season", y]),
            ("nflverse", ["run", "--source", "nflverse", "--season", y]),
            ("team_defense", ["run", "--source", "team_defense", "--season", y]),
            ("sleeper stats", ["sleeper-league", "stats", "--league-id", lid]),
            ("rules check", ["sleeper-league", "rules", "--check", "--league-id", lid]),
            ("rescore", ["rescore", "--season", y]),
            ("reconcile", ["sleeper-league", "reconcile", "--season", y, "--out", str(report)]),
        ]
    typer.echo(f"sleeper-league update: season {y}")
    attention = False
    for name, args in steps:
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "ff_pipeline.cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        took = time.monotonic() - start
        tail = [
            ln
            for ln in (proc.stdout + proc.stderr).splitlines()
            if ln.strip() and not ln.lstrip().startswith("{")  # skip JSON log records
        ][-3:]
        if name == "rules check" and proc.returncode == UPDATE_RULES_CHANGED:
            typer.secho(
                f"  {name}: CHANGED, rescore and reconcile skipped ({took:.0f}s)", fg="yellow"
            )
            attention = True
            break
        if proc.returncode != 0:
            typer.secho(
                f"  {name}: FAILED (exit {proc.returncode}, {took:.0f}s)", fg="red", err=True
            )
            for ln in tail:
                typer.secho(f"    {ln}", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"  {name}: ok ({took:.0f}s) {tail[-1] if tail else ''}".rstrip())
    if attention:
        raise typer.Exit(code=1)
    if not dry_run:
        typer.echo(f"  reconcile report: {report}")
