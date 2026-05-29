"""``/owners/*`` routes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    Envelope,
    OwnerAggregate,
    OwnerHistory,
    OwnerOut,
    OwnerSeasonRecord,
)
from ff_pipeline.repository.models import Season
from ff_pipeline.repository.queries import get_owner, list_teams_for_owner

router = APIRouter(prefix="/owners", tags=["owners"])


@router.get("/{owner_id}", response_model=Envelope[OwnerOut])
def get_owner_endpoint(
    owner_id: int,
    session: SessionDep,
) -> Envelope[OwnerOut]:
    owner = get_owner(session, owner_id)
    if owner is None:
        raise not_found(f"No owner with id {owner_id}")
    return Envelope(
        data=OwnerOut.model_validate(owner),
        meta=build_meta(session, entity_updated_at=owner.updated_at),
    )


@router.get("/{owner_id}/history", response_model=Envelope[OwnerHistory])
def get_owner_history_endpoint(
    owner_id: int,
    session: SessionDep,
) -> Envelope[OwnerHistory]:
    owner = get_owner(session, owner_id)
    if owner is None:
        raise not_found(f"No owner with id {owner_id}")
    teams = list_teams_for_owner(session, owner_id)
    # Build a mapping of season_id -> year so we can decorate each team row.
    season_years = {
        s.season_id: s.year
        for s in session.execute(
            select(Season).where(Season.season_id.in_([t.season_id for t in teams] or [-1]))
        )
        .scalars()
        .all()
    }
    history = OwnerHistory(
        owner_id=owner_id,
        display_name=owner.display_name,
        seasons=[
            OwnerSeasonRecord(
                season_year=season_years.get(t.season_id, 0),
                team_id=t.team_id,
                team_name=t.team_name,
                wins=t.regular_season_wins,
                losses=t.regular_season_losses,
                ties=t.regular_season_ties,
                points_for=t.regular_season_points_for,
                final_rank=t.final_rank,
            )
            for t in teams
        ],
    )
    return Envelope(data=history, meta=build_meta(session))


@router.get("/{owner_id}/aggregate", response_model=Envelope[OwnerAggregate])
def get_owner_aggregate_endpoint(
    owner_id: int,
    session: SessionDep,
) -> Envelope[OwnerAggregate]:
    owner = get_owner(session, owner_id)
    if owner is None:
        raise not_found(f"No owner with id {owner_id}")
    teams = list_teams_for_owner(session, owner_id)
    wins = sum(t.regular_season_wins or 0 for t in teams)
    losses = sum(t.regular_season_losses or 0 for t in teams)
    ties = sum(t.regular_season_ties or 0 for t in teams)
    points = sum(t.regular_season_points_for or 0.0 for t in teams)
    team_ids = {t.team_id for t in teams}
    seasons_won = (
        session.execute(select(Season).where(Season.champion_team_id.in_(team_ids or [-1])))
        .scalars()
        .all()
    )
    aggregate = OwnerAggregate(
        owner_id=owner_id,
        display_name=owner.display_name,
        seasons_played=len(teams),
        total_wins=int(wins),
        total_losses=int(losses),
        total_ties=int(ties),
        total_points_for=round(points, 2),
        championships=len(list(seasons_won)),
    )
    return Envelope(data=aggregate, meta=build_meta(session))
