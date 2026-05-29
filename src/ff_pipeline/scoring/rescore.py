"""Apply ``scoring_rules`` to ``player_stats_raw`` and persist results.

The rescore step is intentionally separable from the crawlers. Crawlers
land raw stats first; rescore picks up later (per-week or per-season)
and writes ``player_stats_scored`` rows the API serves. Decoupling means
a rules correction or engine fix can be applied retroactively without
re-fetching upstream data.

Idempotent. Re-running over a (player, week) pair updates
``total_points`` and ``points_breakdown`` in place via the
``(stat_id, season_id)`` natural key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import (
    PlayerStatsRaw,
    PlayerStatsScored,
    ScoringRule,
    Season,
)
from ff_pipeline.repository.upsert import upsert
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rules import ScoringRule as ScoringRuleDataclass
from ff_pipeline.scoring.rules import ScoringRules

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

log = get_logger(__name__)

# Sources whose raw stats represent the player's actual game-level
# performance. Projections (Sleeper) live in their own table and have
# their own pre-computed projected_points, so we never feed them through
# rescore.
_SCORABLE_SOURCES: tuple[str, ...] = ("nflverse",)


@dataclass(frozen=True, slots=True)
class RescoreDiff:
    """Per-row diff surfaced by ``--dry-run``."""

    player_id: int
    week: int
    season_id: int
    previous_total: float | None
    new_total: float


@dataclass(frozen=True, slots=True)
class RescoreResult:
    """Aggregate counts surfaced to the CLI."""

    seasons_processed: int
    rows_scored: int
    rows_added: int
    rows_updated: int
    rows_unchanged: int
    diffs: tuple[RescoreDiff, ...]
    missing_rules_seasons: tuple[int, ...]


def rescore_seasons(
    session: Session,
    *,
    season_years: Sequence[int] | None = None,
    league_id: str | None = None,
    dry_run: bool = False,
    max_diffs_reported: int = 50,
) -> RescoreResult:
    """Recompute scored points for every ``player_stats_raw`` row in scope.

    ``season_years=None`` rescores every season that has at least one raw
    row. Passing ``league_id`` scopes to that league's seasons only —
    important when more than one league shares the database.

    ``dry_run=True`` computes diffs without writing; the caller can render
    them. Diffs are also returned (capped to ``max_diffs_reported`` newest
    entries) for the write path so the CLI can surface "moved more than
    SCORING_VERIFY_TOLERANCE points" rows.
    """

    seasons = _seasons_in_scope(session, season_years=season_years, league_id=league_id)

    rows_scored = 0
    rows_added = 0
    rows_updated = 0
    rows_unchanged = 0
    diffs: list[RescoreDiff] = []
    missing_rules: list[int] = []

    for season in seasons:
        rules = _load_rules(session, season.season_id)
        if not rules.rules:
            missing_rules.append(season.year)
            log.warning(
                "Skipping season with no scoring rules",
                season_id=season.season_id,
                year=season.year,
            )
            continue

        # Pre-load existing scored rows so we can compute deltas without
        # one SELECT per raw row.
        existing_by_stat_id = _existing_scored_for_season(session, season.season_id)

        scored_rows: list[dict[str, object]] = []
        raw_rows = list(
            session.execute(
                select(
                    PlayerStatsRaw.stat_id,
                    PlayerStatsRaw.player_id,
                    PlayerStatsRaw.week,
                    PlayerStatsRaw.stats,
                    PlayerStatsRaw.source,
                ).where(
                    PlayerStatsRaw.season_year == season.year,
                    PlayerStatsRaw.source.in_(_SCORABLE_SOURCES),
                )
            ).all()
        )
        for raw in raw_rows:
            stats = raw.stats or {}
            if not isinstance(stats, dict):
                continue
            try:
                # The engine expects {stat_key: float}; nflverse projector
                # gives that shape, but be defensive about non-numeric
                # values from older raw rows.
                numeric_stats = {k: float(v) for k, v in stats.items() if _is_number(v)}
            except (TypeError, ValueError):
                continue
            result = apply_rules(numeric_stats, rules)
            previous = existing_by_stat_id.get(raw.stat_id)
            previous_total = previous if previous is not None else None
            scored_rows.append(
                {
                    "stat_id": raw.stat_id,
                    "season_id": season.season_id,
                    "player_id": raw.player_id,
                    "week": raw.week,
                    "total_points": result.total_points,
                    "points_breakdown": result.breakdown,
                }
            )
            if previous_total is None or abs(previous_total - result.total_points) >= 0.001:
                diffs.append(
                    RescoreDiff(
                        player_id=raw.player_id,
                        week=raw.week,
                        season_id=season.season_id,
                        previous_total=previous_total,
                        new_total=result.total_points,
                    )
                )
            else:
                rows_unchanged += 1
            rows_scored += 1

        if not dry_run and scored_rows:
            counts = upsert(
                session,
                PlayerStatsScored,
                scored_rows,
                conflict_cols=("stat_id", "season_id"),
            )
            rows_added += counts.rows_added
            rows_updated += counts.rows_updated

    return RescoreResult(
        seasons_processed=len(seasons),
        rows_scored=rows_scored,
        rows_added=rows_added,
        rows_updated=rows_updated,
        rows_unchanged=rows_unchanged,
        diffs=tuple(diffs[:max_diffs_reported]),
        missing_rules_seasons=tuple(missing_rules),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seasons_in_scope(
    session: Session,
    *,
    season_years: Sequence[int] | None,
    league_id: str | None,
) -> list[Season]:
    stmt = select(Season)
    if league_id is not None:
        stmt = stmt.where(Season.league_id == league_id)
    if season_years is not None:
        stmt = stmt.where(Season.year.in_(list(season_years)))
    return list(session.execute(stmt.order_by(Season.year)).scalars().all())


def _load_rules(session: Session, season_id: int) -> ScoringRules:
    rows = session.execute(
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
        for r in rows
    )
    return ScoringRules(season_id=season_id, rules=rules)


def _existing_scored_for_season(session: Session, season_id: int) -> dict[int, float]:
    rows = session.execute(
        select(PlayerStatsScored.stat_id, PlayerStatsScored.total_points).where(
            PlayerStatsScored.season_id == season_id
        )
    ).all()
    return {sid: float(tp) for sid, tp in rows if tp is not None}


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


__all__ = ["RescoreDiff", "RescoreResult", "rescore_seasons"]
