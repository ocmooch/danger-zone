"""``/players/*`` routes — search index, profile, stats, availability.

The ``/players/availability`` sub-routes are defined here too so the
prefix routing stays consistent. FastAPI matches the longer literal
paths (``/players/availability``) before the parameterized
``/players/{player_id}`` only when they are registered first, so we
declare them in that order.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    AvailabilityRow,
    Envelope,
    OwnershipEvent,
    PlayerOut,
    PlayerStatsBreakdown,
    ProjectionOut,
    RawStatsEntry,
)
from ff_pipeline.repository.queries import (
    availability_snapshot,
    availability_timeline,
    get_player,
    player_availability_for_season,
    player_ownership,
    player_projections,
    player_raw_stats,
    player_scored_stats,
    search_players,
)

router = APIRouter(prefix="/players", tags=["players"])


# ---------------------------------------------------------------------------
# Listing / search — registered before parameterized routes
# ---------------------------------------------------------------------------


@router.get("", response_model=Envelope[list[PlayerOut]])
def list_players_endpoint(
    session: SessionDep,
    name: Annotated[str | None, Query()] = None,
    position: Annotated[str | None, Query()] = None,
    nfl_team: Annotated[str | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Envelope[list[PlayerOut]]:
    players = search_players(
        session,
        name=name,
        position=position,
        nfl_team=nfl_team,
        active=active,
        limit=limit,
        offset=offset,
    )
    return Envelope(
        data=[PlayerOut.model_validate(p) for p in players],
        meta=build_meta(session),
    )


# ---------------------------------------------------------------------------
# League-wide availability (must precede /{player_id} routes)
# ---------------------------------------------------------------------------


@router.get("/availability", response_model=Envelope[list[AvailabilityRow]])
def availability_snapshot_endpoint(
    season: Annotated[int, Query(..., ge=1999, le=2100)],
    week: Annotated[int, Query(..., ge=1, le=18)],
    session: SessionDep,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Envelope[list[AvailabilityRow]]:
    rows = availability_snapshot(
        session,
        season_year=season,
        week=week,
        status=status,
        limit=limit,
        offset=offset,
    )
    return Envelope(
        data=[AvailabilityRow.model_validate(r) for r in rows],
        meta=build_meta(session),
    )


@router.get("/availability/timeline", response_model=Envelope[list[AvailabilityRow]])
def availability_timeline_endpoint(
    player_id: Annotated[int, Query(...)],
    session: SessionDep,
) -> Envelope[list[AvailabilityRow]]:
    if get_player(session, player_id) is None:
        raise not_found(f"No player with id {player_id}")
    rows = availability_timeline(session, player_id)
    return Envelope(
        data=[AvailabilityRow.model_validate(r) for r in rows],
        meta=build_meta(session),
    )


# ---------------------------------------------------------------------------
# Per-player routes
# ---------------------------------------------------------------------------


@router.get("/{player_id}", response_model=Envelope[PlayerOut])
def get_player_endpoint(
    player_id: int,
    session: SessionDep,
) -> Envelope[PlayerOut]:
    player = get_player(session, player_id)
    if player is None:
        raise not_found(f"No player with id {player_id}")
    return Envelope(
        data=PlayerOut.model_validate(player),
        meta=build_meta(session, entity_updated_at=player.updated_at),
    )


@router.get("/{player_id}/stats", response_model=Envelope[PlayerStatsBreakdown])
def get_player_stats_endpoint(
    player_id: int,
    season: Annotated[int, Query(..., ge=1999, le=2100)],
    week: Annotated[int, Query(..., ge=1, le=18)],
    session: SessionDep,
) -> Envelope[PlayerStatsBreakdown]:
    if get_player(session, player_id) is None:
        raise not_found(f"No player with id {player_id}")
    raws = player_raw_stats(session, player_id, season, week)
    scored = player_scored_stats(session, player_id, season, week)
    primary_raw = next((r for r in raws if r.is_primary), raws[0] if raws else None)
    breakdown = PlayerStatsBreakdown(
        player_id=player_id,
        season_year=season,
        week=week,
        raw_stats=dict(primary_raw.stats or {}) if primary_raw else {},
        league_points=scored.total_points if scored else None,
        points_breakdown=dict(scored.points_breakdown or {}) if scored else {},
        all_sources=[RawStatsEntry(source=r.source, stats=dict(r.stats or {})) for r in raws],
    )
    return Envelope(data=breakdown, meta=build_meta(session))


@router.get("/{player_id}/ownership", response_model=Envelope[list[OwnershipEvent]])
def get_player_ownership_endpoint(
    player_id: int,
    session: SessionDep,
) -> Envelope[list[OwnershipEvent]]:
    if get_player(session, player_id) is None:
        raise not_found(f"No player with id {player_id}")
    pairs = player_ownership(session, player_id)
    events = [
        OwnershipEvent(
            team_id=team.team_id,
            team_name=team.team_name,
            season_year=roster.season_year,
            week=roster.week,
            roster_slot=roster.roster_slot,
            acquisition_type=roster.acquisition_type,
            acquisition_date=roster.acquisition_date,
            drop_date=roster.drop_date,
        )
        for roster, team in pairs
    ]
    return Envelope(data=events, meta=build_meta(session))


@router.get("/{player_id}/projections", response_model=Envelope[list[ProjectionOut]])
def get_player_projections_endpoint(
    player_id: int,
    session: SessionDep,
    season: Annotated[int | None, Query()] = None,
    week: Annotated[int | None, Query()] = None,
) -> Envelope[list[ProjectionOut]]:
    if get_player(session, player_id) is None:
        raise not_found(f"No player with id {player_id}")
    rows = player_projections(session, player_id, season, week)
    return Envelope(
        data=[ProjectionOut.model_validate(r) for r in rows],
        meta=build_meta(session),
    )


@router.get("/{player_id}/availability", response_model=Envelope[list[AvailabilityRow]])
def get_player_availability_endpoint(
    player_id: int,
    season: Annotated[int, Query(..., ge=1999, le=2100)],
    session: SessionDep,
) -> Envelope[list[AvailabilityRow]]:
    if get_player(session, player_id) is None:
        raise not_found(f"No player with id {player_id}")
    rows = player_availability_for_season(session, player_id, season)
    return Envelope(
        data=[AvailabilityRow.model_validate(r) for r in rows],
        meta=build_meta(session),
    )
