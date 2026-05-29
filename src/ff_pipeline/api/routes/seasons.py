"""``/seasons/*`` routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    Envelope,
    SeasonOut,
    Standings,
    StandingsRow,
    TeamOut,
)
from ff_pipeline.repository.queries import (
    get_season,
    list_teams_for_season,
    standings_for_season,
)

router = APIRouter(prefix="/seasons", tags=["seasons"])


@router.get("/{season_id}", response_model=Envelope[SeasonOut])
def get_season_endpoint(
    season_id: int,
    session: SessionDep,
) -> Envelope[SeasonOut]:
    season = get_season(session, season_id)
    if season is None:
        raise not_found(f"No season with id {season_id}")
    return Envelope(
        data=SeasonOut.model_validate(season),
        meta=build_meta(session, entity_updated_at=season.updated_at),
    )


@router.get("/{season_id}/standings", response_model=Envelope[Standings])
def get_standings_endpoint(
    season_id: int,
    session: SessionDep,
    through_week: Annotated[int | None, Query(ge=1, le=18)] = None,
) -> Envelope[Standings]:
    season = get_season(session, season_id)
    if season is None:
        raise not_found(f"No season with id {season_id}")
    rows = standings_for_season(session, season_id, through_week=through_week)
    return Envelope(
        data=Standings(
            season_id=season_id,
            through_week=through_week,
            rows=[StandingsRow.model_validate(r) for r in rows],
        ),
        meta=build_meta(session),
    )


@router.get("/{season_id}/teams", response_model=Envelope[list[TeamOut]])
def list_teams_endpoint(
    season_id: int,
    session: SessionDep,
) -> Envelope[list[TeamOut]]:
    season = get_season(session, season_id)
    if season is None:
        raise not_found(f"No season with id {season_id}")
    teams = list_teams_for_season(session, season_id)
    return Envelope(
        data=[TeamOut.model_validate(t) for t in teams],
        meta=build_meta(session),
    )
