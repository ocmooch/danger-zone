"""Backfill Sleeper projections across every fantasy week, playoffs included.

The original projection crawl walked each season only as far as the
fantasy *regular-season* boundary (``seasons.regular_season_weeks``). Fantasy
playoff and consolation matchups continue past that boundary — weeks 15-17 in
a 14-week regular season — and Sleeper serves real, regular-season-type
projections for those NFL weeks. Capping the crawl at the regular-season
boundary therefore left every playoff week with no projections, which the
dashboard correctly reports as ``projections_not_captured``.

This orchestrator walks the *full* fantasy schedule per season. The week
ceiling is derived from the matchup schedule (the authoritative record of
which weeks actually had fantasy games) rather than the regular-season
boundary, so it automatically covers playoff and consolation weeks without
hardcoding week 17.

It reuses :func:`run_sleeper` for each ``(year, week)`` so all of its
guarantees are preserved unchanged: ``season_type="regular"`` (these are NFL
regular-season weeks — never the NFL postseason), hollow-projection filtering,
idempotent projection upserts, player identity-link resolution, and
season-scoped scoring-rule application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, or_, select

from ff_pipeline.crawlers.sleeper.client import LiveSleeperSource
from ff_pipeline.crawlers.sleeper.runner import run_sleeper
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import Matchup, Projection, Season

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.sleeper.client import SleeperSource

log = get_logger(__name__)

BACKFILL_MODE = "backfill"

# NFL regular-season weeks are 1..18 (and were 1..17 before 2021). Sleeper's
# ``season_type=regular`` projections only exist within that window, so a
# fantasy schedule should never imply a week beyond it. This is a sanity
# clamp, not the source of the ceiling — the ceiling comes from the schedule.
MAX_NFL_REGULAR_WEEK = 18


@dataclass(frozen=True, slots=True)
class WeekOutcome:
    """Outcome of backfilling one ``(year, week)`` projection cell."""

    year: int
    week: int
    status: Literal["fetched", "skipped", "failed"]
    projections_added: int = 0
    projections_updated: int = 0
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionBackfillResult:
    """Aggregate of a multi-season projection backfill, in chronological order."""

    per_week: tuple[WeekOutcome, ...] = field(default_factory=tuple)

    @property
    def fetched(self) -> int:
        return sum(1 for o in self.per_week if o.status == "fetched")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.per_week if o.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.per_week if o.status == "failed")

    @property
    def projections_added(self) -> int:
        return sum(o.projections_added for o in self.per_week)

    @property
    def projections_updated(self) -> int:
        return sum(o.projections_updated for o in self.per_week)


def fantasy_week_ceiling(
    session: Session, *, league_id: str, year: int, matchups_only: bool = False
) -> int | None:
    """Highest fantasy week that has matchups for ``(league_id, year)``.

    Prefers the matchup schedule — the authoritative record of which weeks
    actually hosted fantasy games, including playoff and consolation rounds.
    Falls back to the season's recorded shape
    (``regular_season_weeks + playoff_weeks``) when no matchups are stored yet,
    unless ``matchups_only`` is set. Returns ``None`` when neither is known, so
    the caller can skip the season rather than guess. The result is clamped to
    the NFL regular-season window.

    The weekly job passes ``matchups_only``: for a season in progress the
    fallback would reach weeks not yet played, and a preseason fetch of those
    weeks would then be skipped as "populated" for the rest of the season.
    """

    season = session.execute(
        select(Season).where(Season.league_id == league_id, Season.year == year)
    ).scalar_one_or_none()
    if season is None:
        return None

    max_matchup_week = session.execute(
        select(func.max(Matchup.week)).where(Matchup.season_id == season.season_id)
    ).scalar_one_or_none()
    if max_matchup_week is not None:
        return min(int(max_matchup_week), MAX_NFL_REGULAR_WEEK)

    if matchups_only:
        return None

    if season.regular_season_weeks is not None:
        ceiling = season.regular_season_weeks + (season.playoff_weeks or 0)
        return min(ceiling, MAX_NFL_REGULAR_WEEK)

    return None


def _week_has_projections(session: Session, *, year: int, week: int) -> bool:
    """True when at least one (non-hollow) projection row exists for the cell.

    Hollow projections are filtered before insert, so any stored row is a
    real forecast. Used to skip already-populated weeks on a resumed run.
    """

    return (
        session.execute(
            select(Projection.projection_id)
            .where(Projection.season_year == year, Projection.week == week)
            .limit(1)
        ).first()
        is not None
    )


def _week_is_unplayed(session: Session, *, league_id: str, year: int, week: int) -> bool:
    """True when the week has matchups and none of them has a score yet.

    Sleeper stores upcoming matchups with 0-0 scores, and a played week can't
    score 0 everywhere, so "no non-zero score" means the games haven't happened.
    """

    has_score = or_(
        func.coalesce(Matchup.team_score, 0) != 0,
        func.coalesce(Matchup.opponent_score, 0) != 0,
    )
    row = session.execute(
        select(
            func.count(Matchup.matchup_id).filter(has_score),
            func.count(Matchup.matchup_id),
        )
        .join(Season, Season.season_id == Matchup.season_id)
        .where(Season.league_id == league_id, Season.year == year, Matchup.week == week)
    ).one()
    scored, total = int(row[0]), int(row[1])
    return total > 0 and scored == 0


def run_projection_backfill(
    session: Session,
    *,
    league_id: str,
    start_year: int,
    end_year: int,
    source: SleeperSource | None = None,
    season_type: str = "regular",
    skip_populated_weeks: bool = True,
    first_week: int = 1,
    matchups_only: bool = False,
    refresh_latest_week: bool = False,
) -> ProjectionBackfillResult:
    """Backfill Sleeper projections for every fantasy week in each season.

    Walks ``[start_year, end_year]`` and, for each season, every week from
    ``first_week`` up to :func:`fantasy_week_ceiling` (matchup-derived, so
    playoff/consolation weeks are covered). Each week is delegated to
    :func:`run_sleeper`, which is committed individually so a later failure
    keeps the work already done and a resumed run picks up where it stopped.

    ``skip_populated_weeks`` (default) skips any week that already has
    projections, making a re-run cheap and idempotent and letting the live
    backfill touch only the missing playoff cells. ``refresh_latest_week``
    re-fetches the season's last week even when it is populated, as long as
    that week is still unplayed: in a season in progress it is the upcoming
    week, whose projections move until kickoff (readers take the newest
    ``fetched_at``). A finished season is never re-fetched. ``matchups_only`` is
    passed through to :func:`fantasy_week_ceiling`. Pass an open
    ``source`` to reuse one HTTP session; ``None`` opens a
    :class:`LiveSleeperSource` for the duration of the backfill.
    """

    if start_year > end_year:
        raise ValueError(
            f"start_year ({start_year}) must be <= end_year ({end_year}) for projection backfill"
        )

    if source is None:
        with LiveSleeperSource() as live:
            return _drive(
                session,
                league_id=league_id,
                start_year=start_year,
                end_year=end_year,
                source=live,
                season_type=season_type,
                skip_populated_weeks=skip_populated_weeks,
                first_week=first_week,
                matchups_only=matchups_only,
                refresh_latest_week=refresh_latest_week,
            )
    return _drive(
        session,
        league_id=league_id,
        start_year=start_year,
        end_year=end_year,
        source=source,
        season_type=season_type,
        skip_populated_weeks=skip_populated_weeks,
        first_week=first_week,
        matchups_only=matchups_only,
        refresh_latest_week=refresh_latest_week,
    )


def _drive(
    session: Session,
    *,
    league_id: str,
    start_year: int,
    end_year: int,
    source: SleeperSource,
    season_type: str,
    skip_populated_weeks: bool,
    first_week: int,
    matchups_only: bool,
    refresh_latest_week: bool,
) -> ProjectionBackfillResult:
    outcomes: list[WeekOutcome] = []

    for year in range(start_year, end_year + 1):
        ceiling = fantasy_week_ceiling(
            session, league_id=league_id, year=year, matchups_only=matchups_only
        )
        if ceiling is None:
            log.info("Projection backfill skipping season with unknown schedule", year=year)
            continue

        for week in range(first_week, ceiling + 1):
            refresh = (
                refresh_latest_week
                and week == ceiling
                and _week_is_unplayed(session, league_id=league_id, year=year, week=week)
            )
            if (
                skip_populated_weeks
                and not refresh
                and _week_has_projections(session, year=year, week=week)
            ):
                outcomes.append(
                    WeekOutcome(
                        year=year,
                        week=week,
                        status="skipped",
                        detail="week already has projections",
                    )
                )
                continue

            try:
                result = run_sleeper(
                    session,
                    league_id=league_id,
                    year=year,
                    week=week,
                    source=source,
                    mode=BACKFILL_MODE,
                    season_type=season_type,
                )
            except Exception as exc:  # record + continue; one bad week must not abort the grid
                session.rollback()
                outcomes.append(
                    WeekOutcome(
                        year=year,
                        week=week,
                        status="failed",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                log.error(
                    "Projection backfill week failed",
                    year=year,
                    week=week,
                    error=str(exc),
                )
                continue

            outcomes.append(
                WeekOutcome(
                    year=year,
                    week=week,
                    status="fetched",
                    projections_added=result.projections_added,
                    projections_updated=result.projections_updated,
                )
            )
            # Commit per week so a later failure preserves completed weeks.
            session.commit()

    return ProjectionBackfillResult(per_week=tuple(outcomes))


__all__ = [
    "BACKFILL_MODE",
    "MAX_NFL_REGULAR_WEEK",
    "ProjectionBackfillResult",
    "WeekOutcome",
    "fantasy_week_ceiling",
    "run_projection_backfill",
]
