"""High-level "run the sleeper crawler" function.

Pulls Sleeper projections + trending data and writes them through the
repository upserter, mirroring the nflverse runner's pipeline_runs /
source_health bookkeeping. Called from ``ff-pipeline run --source
sleeper`` and reused in the M9 backfill.

Order of operations for one run:

1. Pull ``/v1/players/nfl`` once — used to populate ``players.sleeper_id``
   for any nflverse-known player we don't already have a Sleeper ID for.
2. Pull ``/projections/nfl/{year}/{week}``, map each Sleeper player_id to
   our internal ``player_id`` via ``players.sleeper_id``, score the
   projected stats through the engine using the season's
   ``scoring_rules``, and upsert into ``projections``.
3. Pull trending adds and drops, map IDs the same way, and upsert into
   ``trending_players``.

ID-mapping caveats:

* Players Sleeper knows about but nflverse hasn't surfaced yet are
  *skipped*, not stubbed. The full normalizer (M7) is responsible for
  reconciling Sleeper-only players against our identity table — adding
  another stub-creation path here would pollute ``players`` with
  duplicates the normalizer would then have to merge.
* Conversely, projections / trending rows for sleeper IDs we can't
  resolve are counted in ``unresolved_*`` so the user sees how much
  data was skipped (and the M9 verifier has a knob to chase).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ff_pipeline.crawlers.sleeper.client import LiveSleeperSource
from ff_pipeline.crawlers.sleeper.endpoints import (
    SleeperClient,
    SleeperPlayer,
    SleeperProjection,
    SleeperTrend,
)
from ff_pipeline.logging_config import get_logger
from ff_pipeline.normalizer.player_ids import PlayerIdentity, PlayerResolver
from ff_pipeline.repository.models import (
    PipelineRun,
    Player,
    PlayerIdentityLink,
    Projection,
    ScoringRule,
    Season,
    SourceHealth,
    TrendingPlayer,
)
from ff_pipeline.repository.upsert import UpsertCounts, upsert
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rules import ScoringRule as ScoringRuleDataclass
from ff_pipeline.scoring.rules import ScoringRules

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.sleeper.client import SleeperSource

log = get_logger(__name__)

SOURCE_NAME = "sleeper"
TRENDING_DEFAULT_LOOKBACK_HOURS = 24
TRENDING_DEFAULT_LIMIT = 25


@dataclass(frozen=True, slots=True)
class SleeperRunResult:
    """Outcome of one Sleeper crawler run.

    ``unresolved_projections`` / ``unresolved_trending`` count Sleeper
    rows whose ``player_id`` did not map to any internal ``player_id``.
    Non-zero counts here indicate work for the M7 normalizer (or an
    nflverse run that hasn't seen these players yet).
    """

    players_with_sleeper_id_updated: int
    projections_added: int
    projections_updated: int
    unresolved_projections: int
    trending_added: int
    trending_updated: int
    unresolved_trending: int
    scoring_rules_found: bool
    duration_ms: int


def run_sleeper(
    session: Session,
    *,
    league_id: str,
    year: int,
    week: int,
    source: SleeperSource | None = None,
    mode: str = "full_sync",
    lookback_hours: int = TRENDING_DEFAULT_LOOKBACK_HOURS,
    trending_limit: int = TRENDING_DEFAULT_LIMIT,
    season_type: str = "regular",
) -> SleeperRunResult:
    """Pull Sleeper projections + trending for one (year, week) and persist.

    ``league_id`` + ``year`` together determine which ``scoring_rules`` row
    set is applied to projected stats. Caller commits.
    """

    run = PipelineRun(status="running", mode=mode)
    session.add(run)
    session.flush()  # populate run.run_id for source_health FK
    start = time.perf_counter()

    try:
        if source is None:
            with LiveSleeperSource() as live:
                return _do_run(
                    session,
                    run,
                    start,
                    league_id=league_id,
                    year=year,
                    week=week,
                    source=live,
                    lookback_hours=lookback_hours,
                    trending_limit=trending_limit,
                    season_type=season_type,
                )
        return _do_run(
            session,
            run,
            start,
            league_id=league_id,
            year=year,
            week=week,
            source=source,
            lookback_hours=lookback_hours,
            trending_limit=trending_limit,
            season_type=season_type,
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        run.status = "failed"
        run.finished_at = datetime.now(tz=UTC)
        run.error_summary = f"{type(exc).__name__}: {exc}"
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source=SOURCE_NAME,
                status="failed",
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        )
        raise


def _do_run(
    session: Session,
    run: PipelineRun,
    start: float,
    *,
    league_id: str,
    year: int,
    week: int,
    source: SleeperSource,
    lookback_hours: int,
    trending_limit: int,
    season_type: str,
) -> SleeperRunResult:
    client = SleeperClient(source)

    sleeper_players = client.players()
    resolver = PlayerResolver(session)
    sleeper_id_updates = _sync_sleeper_ids(session, sleeper_players, resolver=resolver)
    session.flush()

    sleeper_to_player_id = _build_sleeper_to_player_id_map(session)

    scoring_rules = _load_scoring_rules(session, league_id=league_id, year=year)
    scoring_rules_found = scoring_rules is not None and bool(scoring_rules.rules)

    projections = client.projections(year, week, season_type=season_type)
    proj_counts, unresolved_proj = _upsert_projections(
        session,
        projections,
        sleeper_to_player_id=sleeper_to_player_id,
        scoring_rules=scoring_rules,
    )

    fetched_at = datetime.now(tz=UTC)
    adds = client.trending("add", lookback_hours=lookback_hours, limit=trending_limit)
    drops = client.trending("drop", lookback_hours=lookback_hours, limit=trending_limit)
    trend_counts, unresolved_trend = _upsert_trending(
        session,
        adds=adds,
        drops=drops,
        sleeper_to_player_id=sleeper_to_player_id,
        lookback_hours=lookback_hours,
        fetched_at=fetched_at,
    )

    duration_ms = int((time.perf_counter() - start) * 1000)
    run.status = "success"
    run.finished_at = datetime.now(tz=UTC)
    run.sources_summary = {
        SOURCE_NAME: {
            "year": year,
            "week": week,
            "players_with_sleeper_id_updated": sleeper_id_updates,
            "projections_added": proj_counts.rows_added,
            "projections_updated": proj_counts.rows_updated,
            "unresolved_projections": unresolved_proj,
            "trending_added": trend_counts.rows_added,
            "trending_updated": trend_counts.rows_updated,
            "unresolved_trending": unresolved_trend,
            "scoring_rules_found": scoring_rules_found,
        }
    }
    session.add(
        SourceHealth(
            run_id=run.run_id,
            source=SOURCE_NAME,
            status="success",
            rows_added=proj_counts.rows_added + trend_counts.rows_added,
            rows_updated=proj_counts.rows_updated + trend_counts.rows_updated,
            duration_ms=duration_ms,
        )
    )

    log.info(
        "sleeper run complete",
        year=year,
        week=week,
        projections_added=proj_counts.rows_added,
        projections_updated=proj_counts.rows_updated,
        unresolved_projections=unresolved_proj,
        trending_added=trend_counts.rows_added,
        trending_updated=trend_counts.rows_updated,
        unresolved_trending=unresolved_trend,
        scoring_rules_found=scoring_rules_found,
        duration_ms=duration_ms,
    )

    return SleeperRunResult(
        players_with_sleeper_id_updated=sleeper_id_updates,
        projections_added=proj_counts.rows_added,
        projections_updated=proj_counts.rows_updated,
        unresolved_projections=unresolved_proj,
        trending_added=trend_counts.rows_added,
        trending_updated=trend_counts.rows_updated,
        unresolved_trending=unresolved_trend,
        scoring_rules_found=scoring_rules_found,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# ID-mapping helpers
# ---------------------------------------------------------------------------


def _sync_sleeper_ids(
    session: Session,
    sleeper_players: list[SleeperPlayer],
    *,
    resolver: PlayerResolver,
) -> int:
    """Merge Sleeper-known IDs onto existing ``players`` rows.

    The resolver does the heavy lifting: direct gsis_id match first, then
    fuzzy name+position. Sleeper-only players (whom we've never seen via
    nflverse or NFL.com) are *not* stubbed — that would flood ``players``
    with thousands of rows the league will never join against. Returns
    the count of players whose Sleeper ID was newly stamped during this
    pass.
    """
    _ = session  # state lives on resolver / its session — kept for symmetry

    before = resolver.stats.merged_ids_by_kind.get("sleeper_id", 0)
    for sp in sleeper_players:
        if not sp.sleeper_id:
            continue
        identity = PlayerIdentity(
            name_full=sp.full_name or sp.sleeper_id,
            name_first=sp.first_name,
            name_last=sp.last_name,
            position=sp.position,
            nfl_team=sp.nfl_team,
            gsis_id=sp.gsis_id,
            sleeper_id=sp.sleeper_id,
            espn_id=sp.espn_id,
            yahoo_id=sp.yahoo_id,
        )
        resolver.try_match(identity, source="sleeper")
    return resolver.stats.merged_ids_by_kind.get("sleeper_id", 0) - before


def _build_sleeper_to_player_id_map(session: Session) -> dict[str, int]:
    stmt = (
        select(
            Player.sleeper_id,
            func.coalesce(PlayerIdentityLink.canonical_player_id, Player.player_id),
        )
        .outerjoin(PlayerIdentityLink, PlayerIdentityLink.member_player_id == Player.player_id)
        .where(Player.sleeper_id.isnot(None))
    )
    return {sid: pid for sid, pid in session.execute(stmt).all() if sid}


# ---------------------------------------------------------------------------
# Scoring-rules loader
# ---------------------------------------------------------------------------


def _load_scoring_rules(
    session: Session,
    *,
    league_id: str,
    year: int,
) -> ScoringRules | None:
    """Hydrate a ``ScoringRules`` value object from the DB for one season.

    Returns ``None`` if no season row exists (M5's scoring loader hasn't
    run yet, or the season is missing). Returns an *empty* ``ScoringRules``
    if the season exists but has no scoring rules attached — the engine
    will produce zero points in that case, which the caller surfaces as a
    warning.
    """

    season_id = session.execute(
        select(Season.season_id).where(Season.league_id == league_id, Season.year == year)
    ).scalar_one_or_none()
    if season_id is None:
        log.warning(
            "Sleeper run: no season row found for scoring; projected_points will be NULL",
            league_id=league_id,
            year=year,
        )
        return None

    rule_rows = session.execute(
        select(
            ScoringRule.category,
            ScoringRule.stat_key,
            ScoringRule.points_per_unit,
            ScoringRule.unit_size,
            ScoringRule.threshold_min,
            ScoringRule.threshold_max,
            ScoringRule.flat_points,
        ).where(ScoringRule.season_id == season_id)
    ).all()
    rules = tuple(
        ScoringRuleDataclass(
            category=str(r.category),
            stat_key=str(r.stat_key),
            points_per_unit=float(r.points_per_unit or 0.0),
            unit_size=float(r.unit_size or 1.0),
            threshold_min=(float(r.threshold_min) if r.threshold_min is not None else None),
            threshold_max=(float(r.threshold_max) if r.threshold_max is not None else None),
            flat_points=(float(r.flat_points) if r.flat_points is not None else None),
        )
        for r in rule_rows
    )
    return ScoringRules(season_id=season_id, rules=rules)


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


def _is_hollow_projection(stats: dict[str, float] | None, projected_points: float | None) -> bool:
    """True for a projection that carries no real forecast.

    Sleeper returns full rosters of all-zero rows for seasons before its
    projection coverage begins (~2018) and for players it didn't project in a
    covered week. Persisting them advertises coverage the source never had and
    renders downstream as a bogus ``0.0``. A projection is real when it has a
    nonzero ``projected_points`` or at least one nonzero stat; otherwise hollow.
    """
    if projected_points not in (None, 0, 0.0):
        return False
    return not (
        stats and any(isinstance(v, (int, float)) and v not in (0, 0.0) for v in stats.values())
    )


def _upsert_projections(
    session: Session,
    projections: list[SleeperProjection],
    *,
    sleeper_to_player_id: dict[str, int],
    scoring_rules: ScoringRules | None,
) -> tuple[UpsertCounts, int]:
    """Project Sleeper projections onto our schema and upsert them.

    Returns ``(counts, unresolved_count)``. ``unresolved_count`` is the
    number of Sleeper rows we skipped because no internal player_id was
    available — exposed in source_health for visibility.
    """

    now = datetime.now(tz=UTC)
    rows: list[dict[str, object]] = []
    unresolved = 0
    hollow = 0

    for proj in projections:
        pid = sleeper_to_player_id.get(proj.sleeper_id)
        if pid is None:
            unresolved += 1
            continue
        projected_points: float | None = None
        if scoring_rules is not None and scoring_rules.rules:
            projected_points = apply_rules(proj.stats, scoring_rules).total_points
        # Drop hollow rows at the source so the DB never advertises empty
        # projection coverage (e.g. all of 2010-2017, and unprojected players in
        # covered weeks). See ``_is_hollow_projection``.
        if _is_hollow_projection(proj.stats, projected_points):
            hollow += 1
            continue
        rows.append(
            {
                "player_id": pid,
                "season_year": proj.season_year,
                "week": proj.week,
                "source": SOURCE_NAME,
                "projected_stats": proj.stats,
                "projected_points": projected_points,
                "fetched_at": now,
            }
        )

    if hollow:
        log.info("Skipped %d hollow Sleeper projection rows", hollow)

    counts = upsert(
        session,
        Projection,
        rows,
        conflict_cols=("player_id", "season_year", "week", "source", "fetched_at"),
    )
    return counts, unresolved


def _upsert_trending(
    session: Session,
    *,
    adds: list[SleeperTrend],
    drops: list[SleeperTrend],
    sleeper_to_player_id: dict[str, int],
    lookback_hours: int,
    fetched_at: datetime,
) -> tuple[UpsertCounts, int]:
    rows: list[dict[str, object]] = []
    unresolved = 0

    for trend_type, trends in (("add", adds), ("drop", drops)):
        for t in trends:
            pid = sleeper_to_player_id.get(t.sleeper_id)
            if pid is None:
                unresolved += 1
                continue
            rows.append(
                {
                    "player_id": pid,
                    "trend_type": trend_type,
                    "count": t.count,
                    "lookback_hours": lookback_hours,
                    "fetched_at": fetched_at,
                }
            )

    counts = upsert(
        session,
        TrendingPlayer,
        rows,
        conflict_cols=("player_id", "trend_type", "lookback_hours", "fetched_at"),
    )
    return counts, unresolved


__all__ = ["SleeperRunResult", "run_sleeper"]
