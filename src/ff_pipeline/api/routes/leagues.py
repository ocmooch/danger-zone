"""``/leagues/*`` routes."""

from __future__ import annotations

from fastapi import APIRouter

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    Envelope,
    LeagueSummary,
    OwnerOut,
    SeasonOut,
)
from ff_pipeline.repository.queries import (
    count_owners_for_league,
    count_seasons_for_league,
    get_league,
    list_leagues,
    list_owners_for_league,
    list_seasons_for_league,
)

router = APIRouter(prefix="/leagues", tags=["leagues"])


def _league_to_summary(league_row: object, season_count: int, owner_count: int) -> LeagueSummary:
    summary = LeagueSummary.model_validate(league_row)
    return summary.model_copy(update={"season_count": season_count, "owner_count": owner_count})


@router.get("", response_model=Envelope[list[LeagueSummary]])
def list_leagues_endpoint(
    session: SessionDep,
) -> Envelope[list[LeagueSummary]]:
    leagues = list_leagues(session)
    data = [
        _league_to_summary(
            lg,
            count_seasons_for_league(session, lg.league_id),
            count_owners_for_league(session, lg.league_id),
        )
        for lg in leagues
    ]
    return Envelope(data=data, meta=build_meta(session))


@router.get("/{league_id}", response_model=Envelope[LeagueSummary])
def get_league_endpoint(
    league_id: str,
    session: SessionDep,
) -> Envelope[LeagueSummary]:
    league = get_league(session, league_id)
    if league is None:
        raise not_found(f"No league with id {league_id!r}")
    data = _league_to_summary(
        league,
        count_seasons_for_league(session, league_id),
        count_owners_for_league(session, league_id),
    )
    return Envelope(data=data, meta=build_meta(session, entity_updated_at=league.updated_at))


@router.get("/{league_id}/owners", response_model=Envelope[list[OwnerOut]])
def list_owners_endpoint(
    league_id: str,
    session: SessionDep,
) -> Envelope[list[OwnerOut]]:
    if get_league(session, league_id) is None:
        raise not_found(f"No league with id {league_id!r}")
    owners = list_owners_for_league(session, league_id)
    return Envelope(
        data=[OwnerOut.model_validate(o) for o in owners],
        meta=build_meta(session),
    )


@router.get("/{league_id}/seasons", response_model=Envelope[list[SeasonOut]])
def list_seasons_endpoint(
    league_id: str,
    session: SessionDep,
) -> Envelope[list[SeasonOut]]:
    if get_league(session, league_id) is None:
        raise not_found(f"No league with id {league_id!r}")
    seasons = list_seasons_for_league(session, league_id)
    return Envelope(
        data=[SeasonOut.model_validate(s) for s in seasons],
        meta=build_meta(session),
    )
