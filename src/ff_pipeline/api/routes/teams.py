"""``/teams/*`` routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    Envelope,
    MatchupOut,
    PlayerLite,
    RosterSlot,
    TeamOut,
    TeamRoster,
    TransactionOut,
)
from ff_pipeline.repository.queries import (
    get_team,
    matchups_for_team,
    roster_for_team_week,
    transactions_for_team,
)

router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("/{team_id}", response_model=Envelope[TeamOut])
def get_team_endpoint(
    team_id: int,
    session: SessionDep,
) -> Envelope[TeamOut]:
    team = get_team(session, team_id)
    if team is None:
        raise not_found(f"No team with id {team_id}")
    return Envelope(
        data=TeamOut.model_validate(team),
        meta=build_meta(session, entity_updated_at=team.updated_at),
    )


@router.get("/{team_id}/roster", response_model=Envelope[TeamRoster])
def get_team_roster_endpoint(
    team_id: int,
    session: SessionDep,
    week: Annotated[int | None, Query(ge=1, le=18)] = None,
) -> Envelope[TeamRoster]:
    team = get_team(session, team_id)
    if team is None:
        raise not_found(f"No team with id {team_id}")
    rows = roster_for_team_week(session, team_id, week)
    if not rows:
        # Empty roster is a valid response — return shell with an
        # explicit ``week`` value so consumers can tell which sweep
        # produced the empty list.
        return Envelope(
            data=TeamRoster(
                team_id=team_id,
                team_name=team.team_name,
                season_year=0,
                week=week or 0,
                slots=[],
            ),
            meta=build_meta(session),
        )
    # All rows share season_year and week (by query construction).
    first_roster = rows[0][0]
    slots = [
        RosterSlot(
            roster_slot=r.roster_slot,
            is_starter=r.is_starter,
            player=PlayerLite.model_validate(p),
            acquisition_type=r.acquisition_type,
            acquisition_week=r.acquisition_week,
        )
        for r, p in rows
    ]
    return Envelope(
        data=TeamRoster(
            team_id=team_id,
            team_name=team.team_name,
            season_year=first_roster.season_year,
            week=first_roster.week,
            slots=slots,
        ),
        meta=build_meta(session),
    )


@router.get("/{team_id}/matchups", response_model=Envelope[list[MatchupOut]])
def list_team_matchups_endpoint(
    team_id: int,
    session: SessionDep,
) -> Envelope[list[MatchupOut]]:
    team = get_team(session, team_id)
    if team is None:
        raise not_found(f"No team with id {team_id}")
    matchups = matchups_for_team(session, team_id)
    return Envelope(
        data=[MatchupOut.model_validate(m) for m in matchups],
        meta=build_meta(session),
    )


@router.get("/{team_id}/transactions", response_model=Envelope[list[TransactionOut]])
def list_team_transactions_endpoint(
    team_id: int,
    session: SessionDep,
) -> Envelope[list[TransactionOut]]:
    team = get_team(session, team_id)
    if team is None:
        raise not_found(f"No team with id {team_id}")
    txns = transactions_for_team(session, team_id)
    return Envelope(
        data=[TransactionOut.model_validate(t) for t in txns],
        meta=build_meta(session),
    )
