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
from typing import TYPE_CHECKING

import typer

from ff_pipeline import __version__

if TYPE_CHECKING:
    # Only referenced in the _run_* helper signatures, which typer never
    # introspects (they aren't command callbacks), so deferring is safe.
    from sqlalchemy.orm import Session

    from ff_pipeline.settings import Settings

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
    week: int | None = typer.Option(
        None,
        "--week",
        help="Override the auto-detected NFL week (1-18). Applies to --source nfl_com / sleeper.",
    ),
    snapshot_kind: str | None = typer.Option(
        None,
        "--snapshot-kind",
        help=(
            "Override the game-time snapshot heuristic: 'pre_kickoff' marks rows as the "
            "canonical pre-kickoff snapshot; 'audit' marks them as a non-authoritative "
            "audit pass. Default: derive from current UTC time. Only applies to --source nfl_com."
        ),
    ),
    verify: bool = typer.Option(False, "--verify", help="Run data-quality checks at end."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen; don't write."),
) -> None:
    """Full sync from all sources (or just one with --source).

    With no ``--source``, every source runs in sequence: nflverse →
    nfl_com → sleeper. nflverse goes first so players exist before
    NFL.com rosters and Sleeper projections resolve against them. Each
    source commits independently, so a failure mid-sequence (e.g. an
    NFL.com auth failure exiting 77) preserves the work of sources that
    already ran.
    """
    _bootstrap_settings_and_logging()
    _ = (verify, dry_run)
    if snapshot_kind is not None and snapshot_kind not in {"pre_kickoff", "audit"}:
        typer.secho(
            f"--snapshot-kind must be 'pre_kickoff' or 'audit' (got {snapshot_kind!r}).",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    if source is not None and source not in {"nflverse", "nfl_com", "sleeper", "team_defense"}:
        _stub(f"run --source {source}", "unknown source")

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    target_year = season or datetime.now().year
    # No --source = the full sequence; nflverse first so player rows exist
    # before nfl_com / sleeper try to resolve against them. team_defense
    # runs last because it matches against the DEF players that the NFL.com
    # roster sync creates.
    sources = [source] if source is not None else ["nflverse", "nfl_com", "sleeper", "team_defense"]

    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            for src in sources:
                if src == "nflverse":
                    _run_nflverse(ss, settings=settings, target_year=target_year)
                elif src == "nfl_com":
                    _run_nfl_com(
                        ss,
                        settings=settings,
                        target_year=target_year,
                        week=week,
                        snapshot_kind=snapshot_kind,
                    )
                elif src == "team_defense":
                    _run_team_defense(ss, seasons=[target_year])
                else:  # sleeper
                    _run_sleeper(ss, settings=settings, target_year=target_year, week=week)
    finally:
        engine.dispose()


def _run_nflverse(ss: Session, *, settings: Settings, target_year: int) -> None:
    """Sync players + raw weekly stats from nflverse, then commit."""
    from ff_pipeline.crawlers.nflverse.runner import run_nflverse

    result = run_nflverse(
        ss,
        seasons=[target_year],
        league_start_year=settings.league_start_year,
        relevant_positions=settings.relevant_positions_set,
    )
    ss.commit()
    typer.echo(
        f"nflverse: players +{result.players_added} "
        f"~{result.players_updated}, "
        f"stats +{result.stats_added} "
        f"~{result.stats_updated} "
        f"({result.duration_ms} ms)"
    )


def _run_team_defense(ss: Session, *, seasons: list[int]) -> None:
    """Ingest team-defense (DST) raw stats, then commit.

    Runs after the league sync so DEF players exist to match against. The
    scored points land on the next ``ff-pipeline rescore``.
    """
    from ff_pipeline.crawlers.nflverse.runner import run_team_defense

    result = run_team_defense(ss, seasons=seasons)
    ss.commit()
    typer.echo(
        f"team_defense: stats +{result.stats_added} ~{result.stats_updated}, "
        f"teams matched {result.teams_matched} / unmatched {result.teams_unmatched} "
        f"({result.duration_ms} ms)"
    )
    if result.stats_added or result.stats_updated:
        typer.secho(
            "  run `ff-pipeline rescore` to score the new DST rows.",
            fg="cyan",
        )


def _run_nfl_com(
    ss: Session,
    *,
    settings: Settings,
    target_year: int,
    week: int | None,
    snapshot_kind: str | None,
) -> None:
    """Sync the NFL.com league snapshot (rosters/matchups/etc.), then commit.

    Raises ``typer.Exit(77)`` on an expired/invalid cookie so cron and the
    multi-source sequence surface the auth failure (EX_NOPERM).
    """
    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError
    from ff_pipeline.crawlers.nfl_com.league import (
        SnapshotKind,
        build_default_client,
        run_nfl_com,
    )

    target_week = week if week is not None else _resolve_current_week(target_year)
    cookie_value = settings.nfl_cookie.get_secret_value()
    try:
        with build_default_client(cookie_value, settings.nfl_com_delay_seconds) as client:
            # mypy-friendly literal narrowing (validated by the caller).
            snapshot_arg: SnapshotKind | None = None
            if snapshot_kind == "pre_kickoff":
                snapshot_arg = "pre_kickoff"
            elif snapshot_kind == "audit":
                snapshot_arg = "audit"
            result = run_nfl_com(
                ss,
                league_id=settings.nfl_league_id,
                year=target_year,
                week=target_week,
                fetcher=client,
                snapshot_kind=snapshot_arg,
            )
        ss.commit()
    except AuthFailureError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=77) from exc  # EX_NOPERM
    typer.echo(
        f"nfl_com [{result.snapshot_kind}] week={target_week}: "
        f"owners +{result.owners_added}~{result.owners_updated}, "
        f"teams +{result.teams_added}~{result.teams_updated}, "
        f"rosters +{result.rosters_added}~{result.rosters_updated}, "
        f"matchups +{result.matchups_added}~{result.matchups_updated}, "
        f"transactions +{result.transactions_added}"
        f"~{result.transactions_updated}, "
        f"availability +{result.availability_added}"
        f"~{result.availability_updated} "
        f"({result.duration_ms} ms)"
    )
    if result.warnings:
        for w in result.warnings:
            typer.secho(f"warning: {w}", fg="yellow", err=True)


def _run_sleeper(ss: Session, *, settings: Settings, target_year: int, week: int | None) -> None:
    """Sync Sleeper projections + trending adds, then commit."""
    from ff_pipeline.crawlers.sleeper.runner import run_sleeper

    target_week = week if week is not None else _resolve_current_week(target_year)
    result = run_sleeper(
        ss,
        league_id=settings.nfl_league_id,
        year=target_year,
        week=target_week,
    )
    ss.commit()
    typer.echo(
        f"sleeper week={target_week}: "
        f"projections +{result.projections_added}"
        f"~{result.projections_updated}"
        f" (unresolved {result.unresolved_projections}), "
        f"trending +{result.trending_added}"
        f"~{result.trending_updated}"
        f" (unresolved {result.unresolved_trending}), "
        f"sleeper_ids stamped {result.players_with_sleeper_id_updated} "
        f"({result.duration_ms} ms)"
    )
    if not result.scoring_rules_found:
        typer.secho(
            "warning: no scoring rules loaded for "
            f"league={settings.nfl_league_id} year={target_year}; "
            "projected_points left NULL. Run `ff-pipeline scoring load` first.",
            fg="yellow",
            err=True,
        )


def _resolve_current_week(year: int) -> int:
    """Best-effort "what week is it now?" for the target season.

    Phase 1 uses a fixed heuristic: the NFL regular season starts the
    first Thursday of September, weeks roll on Tuesday. For pre-season
    runs (calendar year matches target, before September) we default to
    week 1 so transactions/availability still get scraped. Backfill of
    historical weeks is M9's responsibility.
    """
    from datetime import date

    today = date.today()
    if today.year != year or today.month < 9:
        return 1
    # Approximate: weeks since Sept 1, clamped to [1, 18].
    sept_first = date(year, 9, 1)
    delta_weeks = ((today - sept_first).days // 7) + 1
    return max(1, min(18, delta_weeks))


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


@app.command("backfill")
def backfill_cmd(
    start: int | None = typer.Option(
        None, "--start", help="Earliest season year to backfill (default: LEAGUE_START_YEAR)."
    ),
    end: int | None = typer.Option(
        None, "--end", help="Latest season year (inclusive). Default: current calendar year."
    ),
    season: int | None = typer.Option(
        None, "--season", help="Backfill only this season (sets --start and --end)."
    ),
    source: list[str] | None = typer.Option(  # noqa: B008  (typer-idiomatic)
        None,
        "--source",
        help=("Restrict to one source per flag, repeatable: nflverse | nfl_com. Default: both."),
    ),
    week: int = typer.Option(
        1,
        "--week",
        help=(
            "NFL.com week to attach the per-season snapshot to (rosters / "
            "matchups / availability). Defaults to 1 — sufficient for most "
            "historical-shape backfills since matchups are scraped from the "
            "history pages."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run seasons already marked complete in pipeline_runs."
    ),
) -> None:
    """Pull historical seasons (resumable, idempotent)."""
    _bootstrap_settings_and_logging()

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.backfill import (
        DEFAULT_SOURCES,
        BackfillSource,
        run_backfill,
    )
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    if season is not None:
        start_year = season
        end_year = season
    else:
        start_year = start if start is not None else settings.league_start_year
        end_year = end if end is not None else datetime.now().year

    if start_year > end_year:
        typer.secho(
            f"--start ({start_year}) must be <= --end ({end_year}).",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    chosen: tuple[BackfillSource, ...]
    if not source:
        chosen = DEFAULT_SOURCES
    else:
        bad = [s for s in source if s not in {"nflverse", "nfl_com"}]
        if bad:
            typer.secho(
                f"--source values must be nflverse|nfl_com (got {bad!r}).",
                fg="red",
                err=True,
            )
            raise typer.Exit(code=2)
        # preserve order: nflverse before nfl_com so players exist first.
        # The literal values come from a fixed tuple, so we narrow them
        # here with a cast — mypy can't see through `s in source`.
        from typing import cast

        ordered: list[BackfillSource] = [
            cast("BackfillSource", s) for s in ("nflverse", "nfl_com") if s in source
        ]
        chosen = tuple(ordered)

    cookie_value = settings.nfl_cookie.get_secret_value() if "nfl_com" in chosen else None
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            result = run_backfill(
                ss,
                league_id=settings.nfl_league_id,
                start_year=start_year,
                end_year=end_year,
                cookie_value=cookie_value,
                delay_seconds=settings.nfl_com_delay_seconds,
                sources=chosen,
                week=week,
                force=force,
                league_start_year=settings.league_start_year,
                relevant_positions=settings.relevant_positions_set,
            )
    finally:
        engine.dispose()

    for outcome in result.per_season:
        color: str | None
        if outcome.status == "completed":
            color = "green"
        elif outcome.status == "skipped":
            color = "yellow"
        else:
            color = "red"
        typer.secho(
            f"  {outcome.year} {outcome.source}: {outcome.status}"
            + (f" — {outcome.detail}" if outcome.detail else ""),
            fg=color,
        )
    typer.echo(
        f"Backfill: completed={result.completed}, skipped={result.skipped}, failed={result.failed}"
    )
    if result.aborted_at is not None:
        src, yr = result.aborted_at
        typer.secho(
            f"Aborted at {yr} {src}. Re-run `ff-pipeline backfill` to resume.",
            fg="yellow",
            err=True,
        )
        # Auth-failure detail tags the failing outcome; map to EX_NOPERM.
        failed = next(
            (
                o
                for o in result.per_season
                if o.status == "failed" and (o.source, o.year) == result.aborted_at
            ),
            None,
        )
        if failed is not None and failed.detail and "AuthFailureError" in failed.detail:
            raise typer.Exit(code=77)
        raise typer.Exit(code=1)


@app.command("reconstruct")
def reconstruct_cmd(
    start: int | None = typer.Option(
        None, "--start", help="Earliest season year (default: LEAGUE_START_YEAR)."
    ),
    end: int | None = typer.Option(
        None, "--end", help="Latest season year, inclusive (default: current year - 1)."
    ),
    season: int | None = typer.Option(
        None, "--season", help="Reconstruct only this season (sets --start and --end)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run seasons already marked complete in pipeline_runs."
    ),
) -> None:
    """Rebuild real historical league data from NFL.com /history pages.

    Per season, in order: final standings (champion / finish order / team
    names / regular-season-week boundary), every week's matchups, real
    per-week lineups from teamgamecenter, then matchup-derived team
    records. Resumable per season via ``pipeline_runs(mode='reconstruct')``.
    Requires a valid NFL_COOKIE; exits 77 on auth failure so it can be
    resumed after ``cookie set``.
    """
    _bootstrap_settings_and_logging()

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError, NflComClient
    from ff_pipeline.crawlers.nfl_com.history import run_reconstruction
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    if season is not None:
        start_year = end_year = season
    else:
        start_year = start if start is not None else settings.league_start_year
        # Default end is last *completed* season (current year - 1), since
        # an in-progress season has no final standings to reconstruct.
        end_year = end if end is not None else datetime.now().year - 1

    if start_year > end_year:
        typer.secho(f"--start ({start_year}) must be <= --end ({end_year}).", fg="red", err=True)
        raise typer.Exit(code=2)

    cookie_value = settings.nfl_cookie.get_secret_value()
    engine = create_app_engine(settings.database_url)
    client = NflComClient(cookie=cookie_value, delay_seconds=settings.nfl_com_delay_seconds)
    try:
        with Session(engine) as ss:
            try:
                results = run_reconstruction(
                    ss,
                    league_id=settings.nfl_league_id,
                    start_year=start_year,
                    end_year=end_year,
                    fetcher=client,
                    force=force,
                )
            except AuthFailureError as exc:
                typer.secho(
                    f"Auth failure during reconstruction: {exc}. "
                    "Refresh NFL_COOKIE via `cookie set`, then re-run to resume.",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(code=77) from exc
    finally:
        client.close()
        engine.dispose()

    for r in results:
        typer.secho(
            f"  {r.year}: champion_team={r.standings.champion_team_id}, "
            f"weeks={len(r.matchups.weeks_scraped)} "
            f"(playoff {len(r.matchups.playoff_weeks)}), "
            f"roster_rows={r.lineups.rows_added + r.lineups.rows_updated}, "
            f"lineup_failures={r.lineups.fetch_failures}",
            fg="green",
        )
    typer.echo(f"Reconstruct: seasons processed={len(results)} (range {start_year}-{end_year}).")


@app.command("reconstruct-owners")
def reconstruct_owners_cmd(
    start: int | None = typer.Option(
        None, "--start", help="Earliest season year (default: LEAGUE_START_YEAR)."
    ),
    end: int | None = typer.Option(
        None, "--end", help="Latest season year, inclusive (default: current year)."
    ),
) -> None:
    """Reconstruct manager identities + per-season team ownership from history.

    Reads every ``/history/{year}/owners`` page to recover the human who managed
    each franchise each year, derives one owner identity per NFL ``userId``
    (with tenure, aliases, and active/inactive status), and re-points each
    season's ``teams.owner_id`` to the true per-season manager. Fixes the
    backfill artifact where every season showed the *current* owner. Idempotent;
    requires a valid NFL_COOKIE (exit 77 on auth failure).
    """
    _bootstrap_settings_and_logging()

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError, NflComClient
    from ff_pipeline.crawlers.nfl_com.history import reconstruct_owners
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    start_year = start if start is not None else settings.league_start_year
    end_year = end if end is not None else datetime.now().year
    if start_year > end_year:
        typer.secho(f"--start ({start_year}) must be <= --end ({end_year}).", fg="red", err=True)
        raise typer.Exit(code=2)

    engine = create_app_engine(settings.database_url)
    client = NflComClient(
        cookie=settings.nfl_cookie.get_secret_value(), delay_seconds=settings.nfl_com_delay_seconds
    )
    try:
        with Session(engine) as ss:
            try:
                outcome = reconstruct_owners(
                    ss,
                    league_id=settings.nfl_league_id,
                    fetcher=client,
                    start_year=start_year,
                    end_year=end_year,
                )
                ss.commit()
            except AuthFailureError as exc:
                typer.secho(
                    f"Auth failure: {exc}. Refresh NFL_COOKIE via `cookie set`, then re-run.",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(code=77) from exc
    finally:
        client.close()
        engine.dispose()

    typer.secho(
        f"Owners: {outcome.distinct_owners} distinct managers "
        f"(+{outcome.owners_added} new, ~{outcome.owners_updated} updated, "
        f"{outcome.historical_inactive} inactive); "
        f"{outcome.team_attributions_changed} team-season attributions corrected.",
        fg="green",
    )


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------


@app.command("draft")
def draft_cmd(
    start: int | None = typer.Option(
        None, "--start", help="Earliest season year (default: LEAGUE_START_YEAR)."
    ),
    end: int | None = typer.Option(
        None, "--end", help="Latest season year, inclusive (default: current year - 1)."
    ),
    season: int | None = typer.Option(
        None, "--season", help="Capture only this season (sets --start and --end)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run seasons already captured in pipeline_runs."
    ),
) -> None:
    """Capture historical draft results from NFL.com /history pages.

    Per season, reads ``/history/{year}/draftresults`` round by round and
    writes one ``transactions`` row per pick (``transaction_type='draft'``,
    ``effective_week=0``, ``executed_at`` ordered by overall pick), mirrored
    onto ``team_rosters`` at week 0. Seasons whose draft NFL.com never
    recorded are left empty (an honest gap). Resumable per season via
    ``pipeline_runs(mode='draft')``; exits 77 on auth failure so it can be
    resumed after ``cookie set``.
    """
    _bootstrap_settings_and_logging()

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError, NflComClient
    from ff_pipeline.crawlers.nfl_com.history import run_draft_capture
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    if season is not None:
        start_year = end_year = season
    else:
        start_year = start if start is not None else settings.league_start_year
        end_year = end if end is not None else datetime.now().year - 1

    if start_year > end_year:
        typer.secho(f"--start ({start_year}) must be <= --end ({end_year}).", fg="red", err=True)
        raise typer.Exit(code=2)

    cookie_value = settings.nfl_cookie.get_secret_value()
    engine = create_app_engine(settings.database_url)
    client = NflComClient(cookie=cookie_value, delay_seconds=settings.nfl_com_delay_seconds)
    try:
        with Session(engine) as ss:
            try:
                results = run_draft_capture(
                    ss,
                    league_id=settings.nfl_league_id,
                    start_year=start_year,
                    end_year=end_year,
                    fetcher=client,
                    force=force,
                )
            except AuthFailureError as exc:
                typer.secho(
                    f"Auth failure during draft capture: {exc}. "
                    "Refresh NFL_COOKIE via `cookie set`, then re-run to resume.",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(code=77) from exc
    finally:
        client.close()
        engine.dispose()

    captured = 0
    for r in results:
        if r.available:
            captured += 1
            typer.secho(
                f"  {r.year}: picks={r.picks_parsed}, txns_added={r.txns_added} "
                f"(skipped {r.txns_skipped}), roster_rows="
                f"{r.roster_rows_added + r.roster_rows_updated}"
                + (f", unknown_team={r.unknown_team_picks}" if r.unknown_team_picks else ""),
                fg="green",
            )
        else:
            typer.secho(f"  {r.year}: no obtainable draft — recorded nothing.", fg="yellow")
    typer.echo(
        f"Draft: seasons processed={len(results)} "
        f"(captured {captured}, range {start_year}-{end_year})."
    )


# ---------------------------------------------------------------------------
# avatars
# ---------------------------------------------------------------------------


@app.command("avatars")
def avatars_cmd(
    start: int | None = typer.Option(
        None, "--start", help="Earliest season year (default: LEAGUE_START_YEAR)."
    ),
    end: int | None = typer.Option(
        None, "--end", help="Latest season year, inclusive (default: current year - 1)."
    ),
    season: int | None = typer.Option(
        None, "--season", help="Backfill only this season (sets --start and --end)."
    ),
) -> None:
    """Backfill team avatars into the ``assets`` store + ``teams`` FKs.

    Reads each season's NFL.com Managers page, downloads every team logo
    once (content-addressed under the assets dir, deduped by sha256), and
    links it onto that season's ``teams.team_avatar_asset_id``. Idempotent:
    a URL already stored short-circuits before the network and matching
    bytes reuse the existing row, so re-runs download nothing. Requires a
    valid NFL_COOKIE; exits 77 on auth failure so it can be resumed after
    ``cookie set``.
    """
    _bootstrap_settings_and_logging()

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError, NflComClient
    from ff_pipeline.crawlers.nfl_com.media import backfill_team_avatars
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    if season is not None:
        start_year = end_year = season
    else:
        start_year = start if start is not None else settings.league_start_year
        end_year = end if end is not None else datetime.now().year - 1

    if start_year > end_year:
        typer.secho(f"--start ({start_year}) must be <= --end ({end_year}).", fg="red", err=True)
        raise typer.Exit(code=2)

    years = list(range(start_year, end_year + 1))
    cookie_value = settings.nfl_cookie.get_secret_value()
    engine = create_app_engine(settings.database_url)
    client = NflComClient(cookie=cookie_value, delay_seconds=settings.nfl_com_delay_seconds)
    try:
        with Session(engine) as ss:
            try:
                result = backfill_team_avatars(
                    ss,
                    client,
                    league_id=settings.nfl_league_id,
                    assets_root=settings.assets_dir,
                    years=years,
                )
            except AuthFailureError as exc:
                typer.secho(
                    f"Auth failure during avatar backfill: {exc}. "
                    "Refresh NFL_COOKIE via `cookie set`, then re-run to resume.",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(code=77) from exc
            ss.commit()
    finally:
        client.close()
        engine.dispose()

    typer.secho(
        f"Avatars: seasons processed={result.seasons_processed}, "
        f"assets stored={result.assets_stored}, teams linked={result.teams_linked} "
        f"(range {start_year}-{end_year}).",
        fg="green",
    )


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

    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.scoring.rescore import rescore_seasons
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    season_years = [season] if season is not None else None

    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            result = rescore_seasons(
                ss,
                season_years=season_years,
                league_id=settings.nfl_league_id,
                dry_run=dry_run,
            )
            if not dry_run:
                ss.commit()
    finally:
        engine.dispose()

    typer.echo(
        f"Rescore: seasons={result.seasons_processed} scored={result.rows_scored} "
        f"added={result.rows_added} updated={result.rows_updated} "
        f"unchanged={result.rows_unchanged}"
    )
    if result.missing_rules_seasons:
        years = ", ".join(str(y) for y in result.missing_rules_seasons)
        typer.secho(
            f"warning: no scoring rules loaded for seasons: {years} "
            "(load via `ff-pipeline scoring load`).",
            fg="yellow",
            err=True,
        )
    if dry_run:
        if not result.diffs:
            typer.echo("No changes from current scored values.")
        else:
            typer.echo(f"Diffs (showing up to {len(result.diffs)}):")
            for d in result.diffs:
                prev = "—" if d.previous_total is None else f"{d.previous_total:.2f}"
                typer.echo(
                    f"  season_id={d.season_id} player_id={d.player_id} "
                    f"week={d.week}: {prev} -> {d.new_total:.2f}"
                )


@app.command("team-defense")
def team_defense_cmd(
    season: int | None = typer.Option(
        None,
        "--season",
        help="Ingest only this season (default: every season with DEF players).",
    ),
    start_year: int | None = typer.Option(
        None,
        "--start-year",
        help="First season for a range backfill (defaults to LEAGUE_START_YEAR).",
    ),
    end_year: int | None = typer.Option(
        None,
        "--end-year",
        help="Last season for a range backfill (defaults to the current year).",
    ),
) -> None:
    """Ingest team-defense (DST) raw stats from nflverse for past seasons.

    DST stats are derived from nflverse team stats + schedules and matched
    to rostered DEF players. Run after the league data is backfilled (DEF
    players must exist), then run ``ff-pipeline rescore`` to score them.
    """
    _bootstrap_settings_and_logging()

    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.runner import run_team_defense
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    if season is not None:
        seasons = [season]
    else:
        first = start_year if start_year is not None else settings.league_start_year
        last = end_year if end_year is not None else datetime.now().year
        if first > last:
            typer.secho(
                f"--start-year ({first}) must be <= --end-year ({last}).", fg="red", err=True
            )
            raise typer.Exit(code=2)
        seasons = list(range(first, last + 1))

    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            result = run_team_defense(ss, seasons=seasons)
            ss.commit()
    finally:
        engine.dispose()

    typer.echo(
        f"team-defense: seasons={len(seasons)} stats +{result.stats_added} "
        f"~{result.stats_updated}, teams matched {result.teams_matched} / "
        f"unmatched {result.teams_unmatched}"
    )
    if result.stats_added or result.stats_updated:
        typer.secho("Run `ff-pipeline rescore` to score the new DST rows.", fg="cyan")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command("status")
def status_cmd(
    verbose: bool = typer.Option(False, "--verbose", help="Include recent errors."),
) -> None:
    """Show pipeline health, last run, per-source status."""
    _bootstrap_settings_and_logging()

    from sqlalchemy.orm import Session

    from ff_pipeline.observability import collect_status, render_status
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.settings import PROJECT_ROOT, get_settings

    settings = get_settings()
    backup_dir = (PROJECT_ROOT / "data" / "backups").resolve()
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            report = collect_status(
                ss,
                database_url=settings.database_url,
                log_dir=settings.log_dir,
                backup_dir=backup_dir,
            )
    finally:
        engine.dispose()

    typer.echo(render_status(report, verbose=verbose))


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@app.command("verify")
def verify_cmd(
    player: str | None = typer.Option(
        None,
        "--player",
        help="Player name (e.g., 'Lamar Jackson'). Omit + use --sweep for season sweep.",
    ),
    season: int = typer.Option(..., "--season", help="Season year."),
    week: int | None = typer.Option(
        None, "--week", help="Week number. Required with --player; ignored with --sweep."
    ),
    sweep: bool = typer.Option(
        False,
        "--sweep",
        help="Sweep mode: verify every starter on 3 named weeks for --season.",
    ),
    reconcile: bool = typer.Option(
        False,
        "--reconcile",
        help=(
            "Reconcile each team's summed scored starters (incl. DST) against the "
            "authoritative NFL.com team total. Offline; flags drift beyond tolerance."
        ),
    ),
) -> None:
    """Cross-check our scoring vs. NFL.com's stored point total."""
    _bootstrap_settings_and_logging()

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError
    from ff_pipeline.crawlers.nfl_com.league import build_default_client
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.scoring.verify import (
        VerifyReport,
        verify_player,
        verify_season_sweep,
    )
    from ff_pipeline.settings import get_settings

    # Reconcile mode is offline (uses DB-stored team totals) and is mutually
    # exclusive with the NFL.com-fetching player/sweep modes.
    if reconcile:
        if player is not None or sweep:
            typer.secho(
                "--reconcile cannot be combined with --player or --sweep.", fg="red", err=True
            )
            raise typer.Exit(code=2)
        _run_reconcile(season=season, weeks=[week] if week is not None else None)
        return

    if sweep and player is not None:
        typer.secho("--sweep and --player are mutually exclusive.", fg="red", err=True)
        raise typer.Exit(code=2)
    if not sweep and player is None:
        typer.secho("Provide --player + --week, or pass --sweep.", fg="red", err=True)
        raise typer.Exit(code=2)
    if not sweep and week is None:
        typer.secho("--week is required when --player is set.", fg="red", err=True)
        raise typer.Exit(code=2)

    settings = get_settings()
    cookie_value = settings.nfl_cookie.get_secret_value()
    tolerance = settings.scoring_verify_tolerance

    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            try:
                with build_default_client(cookie_value, settings.nfl_com_delay_seconds) as client:
                    if sweep:
                        report = verify_season_sweep(
                            ss,
                            league_id=settings.nfl_league_id,
                            season_year=season,
                            fetcher=client,
                            tolerance=tolerance,
                        )
                    else:
                        # mypy: player/week are guaranteed by the validations above
                        assert player is not None
                        assert week is not None
                        comparison = verify_player(
                            ss,
                            league_id=settings.nfl_league_id,
                            player_name=player,
                            season_year=season,
                            week=week,
                            fetcher=client,
                            tolerance=tolerance,
                        )
                        report = VerifyReport(comparisons=(comparison,), tolerance=tolerance)
            except AuthFailureError as exc:
                typer.secho(str(exc), fg="red", err=True)
                raise typer.Exit(code=77) from exc
    finally:
        engine.dispose()

    for c in report.comparisons:
        ours = "—" if c.our_points is None else f"{c.our_points:.2f}"
        theirs = "—" if c.nfl_com_points is None else f"{c.nfl_com_points:.2f}"
        delta = "—" if c.delta is None else f"{c.delta:+.2f}"
        status_color = "green" if c.passed else "red"
        status = "PASS" if c.passed else "FAIL"
        suffix = f" [{c.note}]" if c.note else ""
        typer.secho(
            f"  {c.season_year} W{c.week:>2} {c.player_name or '?':<28} "
            f"ours={ours:>7} nfl={theirs:>7} delta={delta:>7}  {status}{suffix}",
            fg=status_color,
        )
    if report.note:
        typer.secho(f"  {report.note}", fg="yellow", err=True)
    typer.echo(
        f"Verify: total={report.total} passed={report.passed} failed={report.failed} "
        f"(tolerance={tolerance})"
    )
    # A sweep that compared nothing isn't a clean pass — the season or its
    # scoring rules are missing (report.note explains which). Exit non-zero
    # so this can't read as success in a script.
    if report.failed > 0 or (sweep and report.total == 0):
        raise typer.Exit(code=1)


def _run_reconcile(*, season: int, weeks: list[int] | None) -> None:
    """Offline team-total reconciliation: flag scoring drift incl. DST."""
    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.scoring.verify import reconcile_team_totals
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    tolerance = settings.scoring_verify_tolerance

    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            report = reconcile_team_totals(
                ss,
                league_id=settings.nfl_league_id,
                season_year=season,
                weeks=weeks,
                tolerance=tolerance,
            )
    finally:
        engine.dispose()

    for c in report.comparisons:
        ours = "—" if c.our_total is None else f"{c.our_total:.2f}"
        theirs = "—" if c.nfl_com_total is None else f"{c.nfl_com_total:.2f}"
        delta = "—" if c.delta is None else f"{c.delta:+.2f}"
        status_color = "green" if c.passed else "red"
        status = "PASS" if c.passed else "FAIL"
        miss = f" missing={c.starters_missing_score}" if c.starters_missing_score else ""
        suffix = f" [{c.note}]" if c.note else ""
        typer.secho(
            f"  {c.season_year} W{c.week:>2} {c.team_name or f'team {c.team_id}':<24} "
            f"ours={ours:>7} nfl={theirs:>7} delta={delta:>7}  {status}{miss}{suffix}",
            fg=status_color,
        )
    if report.note:
        typer.secho(f"  {report.note}", fg="yellow", err=True)
    typer.echo(
        f"Reconcile: total={report.total} passed={report.passed} failed={report.failed} "
        f"(tolerance={tolerance})"
    )
    if report.failed > 0 or report.total == 0:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command("serve")
def serve_cmd(
    host: str | None = typer.Option(
        None, "--host", help="Bind address (default: API_HOST from settings)."
    ),
    port: int | None = typer.Option(
        None, "--port", help="Bind port (default: API_PORT from settings)."
    ),
    reload: bool = typer.Option(False, "--reload", help="Dev mode with auto-reload."),
) -> None:
    """Start the FastAPI read API via uvicorn."""
    _bootstrap_settings_and_logging()

    import uvicorn

    from ff_pipeline.settings import get_settings

    settings = get_settings()
    target_host = host or settings.api_host
    target_port = port if port is not None else settings.api_port

    if reload:
        # ``--reload`` requires an import string so uvicorn's reloader can
        # re-import the module. The non-reload path uses a built app
        # instance so tests / direct invocations share one engine.
        uvicorn.run(
            "ff_pipeline.api.main:create_app",
            host=target_host,
            port=target_port,
            reload=True,
            factory=True,
        )
        return

    from ff_pipeline.api.main import create_app

    uvicorn.run(create_app(), host=target_host, port=target_port)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


@app.command("backup")
def backup_cmd(
    backup_dir: Path | None = typer.Option(  # noqa: B008  (typer-idiomatic)
        None,
        "--backup-dir",
        help="Directory for backup files (default: <project>/data/backups).",
    ),
    keep_days: int = typer.Option(
        30,
        "--keep-days",
        help="Delete backups older than this many days (0 = keep all).",
        min=0,
    ),
) -> None:
    """Snapshot the SQLite database to ``data/backups/fantasy-YYYY-MM-DD.db``."""
    _bootstrap_settings_and_logging()

    from ff_pipeline.observability import BackupError, perform_backup
    from ff_pipeline.settings import PROJECT_ROOT, get_settings

    settings = get_settings()
    target_dir = (backup_dir or PROJECT_ROOT / "data" / "backups").resolve()
    try:
        result = perform_backup(
            database_url=settings.database_url,
            backup_dir=target_dir,
            keep_days=keep_days if keep_days > 0 else None,
        )
    except BackupError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"Backup: wrote {result.backup_path} ({result.bytes_written} bytes); "
        f"pruned {len(result.pruned_files)}."
    )


# ---------------------------------------------------------------------------
# prune-players
# ---------------------------------------------------------------------------


@app.command("prune-players")
def prune_players_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be deleted; touch nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for non-interactive use)."
    ),
    no_backup: bool = typer.Option(
        False, "--no-backup", help="Skip the automatic pre-delete backup (not recommended)."
    ),
) -> None:
    """Delete players this IDP-less league can never roster.

    nflverse ships the entire NFL player universe; this removes the noise in
    two complementary passes:

    * **Irrelevant position** — players whose position is outside
      ``RELEVANT_POSITIONS`` and that no roster / transaction / availability /
      override row references. Their incidental stat and projection rows are
      cascade-deleted. Anything the league actually rostered is protected,
      regardless of its (often mislabeled) position string.
    * **Fully orphaned** — leftover rows no other table references at all
      (e.g. pre-league-era skill players).

    Always previews first: run with ``--dry-run`` to see both breakdowns and
    the cascade blast radius, then re-run to delete. A timestamped backup is
    taken before any delete unless ``--no-backup`` is given.
    """
    _bootstrap_settings_and_logging()

    from sqlalchemy.orm import Session

    from ff_pipeline.observability import BackupError, perform_backup
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.prune import (
        prune_irrelevant_position_players,
        prune_orphan_players,
    )
    from ff_pipeline.settings import PROJECT_ROOT, get_settings

    settings = get_settings()
    relevant = settings.relevant_positions_set
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            # Preview both passes first so the operator sees the full blast
            # radius before any destructive choice — even in delete mode.
            pos_preview = prune_irrelevant_position_players(
                ss, relevant_positions=relevant, dry_run=True
            )
            orphan_preview = prune_orphan_players(ss, dry_run=True)

            if pos_preview.players_found:
                typer.echo(
                    f"Irrelevant-position players (not rostered): {pos_preview.players_found}"
                )
                for pos, n in sorted(
                    pos_preview.by_position.items(), key=lambda kv: kv[1], reverse=True
                ):
                    typer.echo(f"  {pos:<8} {n}")
                if pos_preview.cascade_deleted:
                    cascade = ", ".join(
                        f"{tbl}={n}" for tbl, n in pos_preview.cascade_deleted.items() if n
                    )
                    if cascade:
                        typer.echo(f"  + cascade rows: {cascade}")
            else:
                typer.echo("No prunable irrelevant-position players found.")

            if orphan_preview.orphans_found:
                typer.echo(f"Fully-orphaned players: {orphan_preview.orphans_found}")
                for pos, n in sorted(
                    orphan_preview.by_position.items(), key=lambda kv: kv[1], reverse=True
                ):
                    typer.echo(f"  {pos:<8} {n}")
            else:
                typer.echo("No fully-orphaned players found.")

            total = pos_preview.players_found + orphan_preview.orphans_found
            if dry_run or not total:
                return

            if not yes:
                confirmed = typer.confirm(
                    f"Delete {total} players ({pos_preview.players_found} "
                    f"irrelevant-position + {orphan_preview.orphans_found} orphaned)?",
                    default=False,
                )
                if not confirmed:
                    typer.echo("Aborted; nothing deleted.")
                    return

            if not no_backup:
                backup_dir = (PROJECT_ROOT / "data" / "backups").resolve()
                try:
                    result = perform_backup(
                        database_url=settings.database_url, backup_dir=backup_dir
                    )
                    typer.echo(f"Backup written to {result.backup_path} before pruning.")
                except BackupError as exc:
                    typer.secho(
                        f"Pre-prune backup failed ({exc}); aborting. "
                        "Re-run with --no-backup to override.",
                        fg="red",
                        err=True,
                    )
                    raise typer.Exit(code=1) from exc

            pos_outcome = prune_irrelevant_position_players(
                ss, relevant_positions=relevant, dry_run=False
            )
            orphan_outcome = prune_orphan_players(ss, dry_run=False)
            ss.commit()
            cascade_total = sum(pos_outcome.cascade_deleted.values())
            typer.secho(
                f"Deleted {pos_outcome.players_deleted} irrelevant-position players "
                f"(+{cascade_total} cascade rows) and "
                f"{orphan_outcome.deleted} orphaned players.",
                fg="green",
            )
    finally:
        engine.dispose()


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
def cookie_set_cmd(
    cookie: str | None = typer.Option(
        None,
        "--cookie",
        help="Cookie value (omit to be prompted; pipe with --stdin for non-TTY use).",
    ),
    from_stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the cookie value from stdin (e.g., pipe from a file).",
    ),
) -> None:
    """Refresh the NFL.com cookie in ``.env`` after validating it.

    The new value replaces the existing ``NFL_COOKIE=...`` line in
    ``.env``; if ``.env`` doesn't exist it's created from scratch with
    just that one variable. The cookie is **validated** against NFL.com
    before being persisted — refusing to overwrite a working cookie with
    a broken one is the most important safety property of this command.
    """
    _bootstrap_settings_and_logging()

    import sys

    from ff_pipeline.crawlers.nfl_com.client import NflComClient, NflComClientError
    from ff_pipeline.crawlers.nfl_com.urls import league_home
    from ff_pipeline.settings import PROJECT_ROOT, get_settings

    settings = get_settings()

    if from_stdin and cookie is None:
        cookie = sys.stdin.read().strip()
    if cookie is None:
        cookie = typer.prompt("Paste NFL.com cookie", hide_input=True).strip()
    if not cookie:
        typer.secho("Refusing to save an empty cookie.", fg="red", err=True)
        raise typer.Exit(code=65)

    probe_url = league_home(settings.nfl_league_id)
    try:
        with NflComClient(cookie=cookie, delay_seconds=0.0) as client:
            ok = client.test_auth(probe_url)
    except NflComClientError as exc:
        typer.secho(f"Could not reach NFL.com: {exc}", fg="red", err=True)
        raise typer.Exit(code=69) from exc  # EX_UNAVAILABLE

    if not ok:
        typer.secho(
            "Cookie validation failed (login marker present). Not saving.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=77)  # EX_NOPERM

    env_path = PROJECT_ROOT / ".env"
    _write_env_value(env_path, "NFL_COOKIE", cookie)
    typer.echo(f"Cookie validated and saved to {env_path}.")


@cookie_app.command("test")
def cookie_test_cmd() -> None:
    """Verify the current cookie works (one auth-check request to NFL.com)."""
    _bootstrap_settings_and_logging()

    from ff_pipeline.crawlers.nfl_com.client import (
        AuthFailureError,
        NflComClient,
        NflComClientError,
    )
    from ff_pipeline.crawlers.nfl_com.urls import league_home
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    cookie = settings.nfl_cookie.get_secret_value()
    try:
        with NflComClient(cookie=cookie, delay_seconds=settings.nfl_com_delay_seconds) as client:
            ok = client.test_auth(league_home(settings.nfl_league_id))
    except AuthFailureError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=77) from exc
    except NflComClientError as exc:
        typer.secho(f"Could not reach NFL.com: {exc}", fg="red", err=True)
        raise typer.Exit(code=69) from exc

    if ok:
        typer.secho("Cookie is valid.", fg="green")
    else:
        typer.secho(
            "Cookie is invalid; refresh via `ff-pipeline cookie set`.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=77)


def _write_env_value(env_path: Path, key: str, value: str) -> None:
    """Idempotently set ``KEY=...`` in a ``.env`` file.

    Replaces the existing line if present, else appends. The value is
    wrapped in single quotes (matching the existing ``.env.example``
    convention so the cookie's ``;`` and ``=`` chars survive).
    """

    line = f"{key}='{value}'"
    if not env_path.exists():
        env_path.write_text(line + "\n", encoding="utf-8")
        return
    existing = env_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for entry in existing:
        if entry.startswith(f"{key}=") or entry.startswith(f"{key} ="):
            out.append(line)
            replaced = True
        else:
            out.append(entry)
    if not replaced:
        out.append(line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


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
    season: int | None = typer.Option(
        None,
        "--season",
        help=(
            "Season year to load these rules under, overriding the year inferred "
            "from the CSV. Use to apply unchanged settings to a historical season "
            "(e.g. --season 2024) so `verify --season 2024` has rules."
        ),
    ),
) -> None:
    """Parse a league settings export, upsert league/season/scoring_rules rows.

    Idempotent: re-running the same CSV updates ``points_per_unit`` etc.
    in place but never duplicates rules. The CSV is preserved in
    ``fixtures_dir`` so the M9 scoring verifier has a canonical copy.

    By default the season year comes from the CSV (its trade-deadline
    date); ``--season`` overrides it to load the same rules under another
    year.
    """

    _bootstrap_settings_and_logging()

    from dataclasses import replace

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

    if season is not None and season != parsed.season_year:
        typer.secho(
            f"Overriding CSV-inferred season {parsed.season_year} → {season}.",
            fg="yellow",
        )
        parsed = replace(parsed, season_year=season)

    if parsed.league_id != settings.nfl_league_id:
        typer.secho(
            f"League ID in CSV ({parsed.league_id}) != .env NFL_LEAGUE_ID "
            f"({settings.nfl_league_id}). Refusing to load.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=65)

    # Preserve a canonical fixture only for the season the CSV actually
    # represents. When --season overrides the year we're re-applying the
    # same settings to a different season, so a fixture copy named for that
    # year would falsely imply we captured its real export.
    target_fixtures: Path | None
    if season is not None:
        target_fixtures = None
    else:
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
