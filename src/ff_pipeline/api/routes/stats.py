"""Aggregated ``/stats/*`` routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.schemas import (
    Envelope,
    OwnerCareer,
    SeasonTotal,
    TopScorer,
)
from ff_pipeline.repository.queries import (
    owner_career_aggregates,
    season_totals,
    top_scorers,
)

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/players/top", response_model=Envelope[list[TopScorer]])
def players_top_endpoint(
    season: Annotated[int, Query(..., ge=1999, le=2100)],
    session: SessionDep,
    week: Annotated[int | None, Query(ge=1, le=18)] = None,
    position: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 25,
) -> Envelope[list[TopScorer]]:
    rows = top_scorers(
        session,
        season_year=season,
        week=week,
        position=position,
        limit=limit,
    )
    return Envelope(
        data=[TopScorer(**r) for r in rows],
        meta=build_meta(session),
    )


@router.get("/players/season-totals", response_model=Envelope[list[SeasonTotal]])
def players_season_totals_endpoint(
    season: Annotated[int, Query(..., ge=1999, le=2100)],
    session: SessionDep,
) -> Envelope[list[SeasonTotal]]:
    rows = season_totals(session, season)
    return Envelope(
        data=[SeasonTotal(**r) for r in rows],
        meta=build_meta(session),
    )


@router.get("/owners/career", response_model=Envelope[list[OwnerCareer]])
def owners_career_endpoint(
    session: SessionDep,
) -> Envelope[list[OwnerCareer]]:
    rows = owner_career_aggregates(session)
    return Envelope(
        data=[OwnerCareer(**r) for r in rows],
        meta=build_meta(session),
    )
