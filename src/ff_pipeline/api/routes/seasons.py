"""``/seasons/*`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    BracketMatchOut,
    CapabilityOut,
    Envelope,
    SeasonOut,
    Standings,
    StandingsRow,
    TeamOut,
)
from ff_pipeline.repository.queries import (
    brackets_for_season,
    get_season,
    list_teams_for_season,
    season_capabilities,
    season_freshness,
    season_platform,
    standings_for_season,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.repository.models import Season

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
        data=season_out(session, season),
        meta=build_meta(session, entity_updated_at=season.updated_at),
    )


def season_out(session: Session, season: Season) -> SeasonOut:
    """``SeasonOut`` with provenance, freshness and capabilities (S8)."""
    season_id = season.season_id
    out = SeasonOut.model_validate(season)
    platform = season_platform(session, season_id)
    fresh = season_freshness(session, season_id)
    if platform is not None:
        out.platform, out.platform_label = platform.platform, platform.label
    if fresh is not None:
        out.is_final = fresh.is_final
        out.last_finalized_week = fresh.last_finalized_week
        out.provisional_weeks = list(fresh.provisional_weeks)
    out.capabilities = {
        k: CapabilityOut(state=c.state, note=c.note)
        for k, c in season_capabilities(session, season_id).items()
    }
    return out


@router.get("/{season_id}/brackets", response_model=Envelope[list[BracketMatchOut]])
def get_brackets_endpoint(
    season_id: int,
    session: SessionDep,
) -> Envelope[list[BracketMatchOut]]:
    """Official playoff brackets (Sleeper seasons; empty for NFL.com seasons)."""
    if get_season(session, season_id) is None:
        raise not_found(f"No season with id {season_id}")
    return Envelope(
        data=[BracketMatchOut.model_validate(b) for b in brackets_for_season(session, season_id)],
        meta=build_meta(session),
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
