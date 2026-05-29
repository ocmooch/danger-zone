"""Scoring-rules routes.

* ``GET /leagues/{league_id}/seasons/{year}/scoring-rules`` —
  full rule list for that season.
* ``GET /leagues/{league_id}/scoring-rules/diff?from=&to=`` — set
  diff between two seasons (added / removed / modified).

Both routes 404 if either side isn't loaded.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    Envelope,
    ScoringRuleOut,
    ScoringRulesDiff,
    ScoringRulesDiffEntry,
)
from ff_pipeline.repository.queries import (
    get_league,
    get_season_by_year,
    list_scoring_rules,
)

router = APIRouter(prefix="/leagues", tags=["scoring-rules"])


@router.get(
    "/{league_id}/seasons/{year}/scoring-rules",
    response_model=Envelope[list[ScoringRuleOut]],
)
def list_scoring_rules_endpoint(
    league_id: str,
    year: int,
    session: SessionDep,
) -> Envelope[list[ScoringRuleOut]]:
    if get_league(session, league_id) is None:
        raise not_found(f"No league with id {league_id!r}")
    season = get_season_by_year(session, league_id, year)
    if season is None:
        raise not_found(f"No season {year} for league {league_id!r}")
    rules = list_scoring_rules(session, season.season_id)
    return Envelope(
        data=[ScoringRuleOut.model_validate(r) for r in rules],
        meta=build_meta(session),
    )


def _rule_key(rule: ScoringRuleOut) -> tuple[str, str]:
    return rule.category, rule.stat_key


def _rules_equivalent(a: ScoringRuleOut, b: ScoringRuleOut) -> bool:
    """Equality on the semantic fields — ignore IDs and raw_text."""
    return (
        a.points_per_unit == b.points_per_unit
        and a.unit_size == b.unit_size
        and a.threshold_min == b.threshold_min
        and a.threshold_max == b.threshold_max
        and a.flat_points == b.flat_points
    )


@router.get(
    "/{league_id}/scoring-rules/diff",
    response_model=Envelope[ScoringRulesDiff],
)
def diff_scoring_rules_endpoint(
    league_id: str,
    from_: Annotated[int, Query(..., alias="from")],
    to: Annotated[int, Query(...)],
    session: SessionDep,
) -> Envelope[ScoringRulesDiff]:
    if get_league(session, league_id) is None:
        raise not_found(f"No league with id {league_id!r}")
    season_from = get_season_by_year(session, league_id, from_)
    season_to = get_season_by_year(session, league_id, to)
    if season_from is None or season_to is None:
        missing = from_ if season_from is None else to
        raise not_found(f"No season {missing} for league {league_id!r}")

    rules_from = {
        _rule_key(ScoringRuleOut.model_validate(r)): ScoringRuleOut.model_validate(r)
        for r in list_scoring_rules(session, season_from.season_id)
    }
    rules_to = {
        _rule_key(ScoringRuleOut.model_validate(r)): ScoringRuleOut.model_validate(r)
        for r in list_scoring_rules(session, season_to.season_id)
    }

    changes: list[ScoringRulesDiffEntry] = []
    for key in sorted(rules_from.keys() | rules_to.keys()):
        a = rules_from.get(key)
        b = rules_to.get(key)
        category, stat_key = key
        if a is None and b is not None:
            changes.append(
                ScoringRulesDiffEntry(
                    stat_key=stat_key, category=category, to_value=b, change="added"
                )
            )
        elif b is None and a is not None:
            changes.append(
                ScoringRulesDiffEntry(
                    stat_key=stat_key, category=category, from_value=a, change="removed"
                )
            )
        elif a is not None and b is not None and not _rules_equivalent(a, b):
            changes.append(
                ScoringRulesDiffEntry(
                    stat_key=stat_key,
                    category=category,
                    from_value=a,
                    to_value=b,
                    change="modified",
                )
            )

    return Envelope(
        data=ScoringRulesDiff(league_id=league_id, from_year=from_, to_year=to, changes=changes),
        meta=build_meta(session),
    )
