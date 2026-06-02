"""``/matchups/*`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.api.schemas import (
    BoxScore,
    BoxScoreLineupEntry,
    BoxScoreSide,
    Envelope,
    MatchupOut,
)
from ff_pipeline.nfl_teams import canonical_franchise
from ff_pipeline.repository.models import (
    Owner,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.repository.queries import (
    get_matchup,
    list_matchups,
    nfl_franchises_that_played,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

router = APIRouter(prefix="/matchups", tags=["matchups"])

# Roster slots that park a player out of the active lineup on injured
# reserve. NFL.com labels the slot "RES"; we also accept any slot mentioning
# "IR" defensively in case a source uses that spelling.
_RESERVE_SLOTS = frozenset({"RES", "IR"})


def _is_reserve_slot(slot: str | None) -> bool:
    if not slot:
        return False
    upper = slot.upper()
    return upper in _RESERVE_SLOTS or "IR" in upper


def _lineup_status(
    *,
    scored: PlayerStatsScored | None,
    roster_slot: str | None,
    nfl_team: str | None,
    played_franchises: set[str],
) -> str:
    """Classify why a lineup entry has (or lacks) a score. See ``BoxScoreLineupEntry``."""
    if scored is not None:
        return "played"
    if _is_reserve_slot(roster_slot):
        return "ir"
    franchise = canonical_franchise(nfl_team)
    # Only call a bye when we actually know which teams played; an empty set
    # means the week isn't ingested yet, not that everyone is on a bye.
    if played_franchises and franchise is not None and franchise not in played_franchises:
        return "bye"
    return "did_not_play"


@router.get("", response_model=Envelope[list[MatchupOut]])
def list_matchups_endpoint(
    session: SessionDep,
    season: Annotated[int | None, Query()] = None,
    week: Annotated[int | None, Query(ge=1, le=18)] = None,
) -> Envelope[list[MatchupOut]]:
    rows = list_matchups(session, season_year=season, week=week)
    return Envelope(
        data=[MatchupOut.model_validate(m) for m in rows],
        meta=build_meta(session),
    )


@router.get("/{matchup_id}", response_model=Envelope[MatchupOut])
def get_matchup_endpoint(
    matchup_id: int,
    session: SessionDep,
) -> Envelope[MatchupOut]:
    matchup = get_matchup(session, matchup_id)
    if matchup is None:
        raise not_found(f"No matchup with id {matchup_id}")
    return Envelope(
        data=MatchupOut.model_validate(matchup),
        meta=build_meta(session, entity_updated_at=matchup.updated_at),
    )


def _build_side(
    session: Session,
    team_id: int,
    season_year: int,
    week: int,
    total_score: float | None,
    played_franchises: set[str],
) -> BoxScoreSide:
    team = session.get(Team, team_id)
    owner = session.get(Owner, team.owner_id) if team else None
    roster_rows = session.execute(
        select(TeamRoster, Player)
        .join(Player, Player.player_id == TeamRoster.player_id)
        .where(TeamRoster.team_id == team_id, TeamRoster.week == week)
    ).all()

    lineup: list[BoxScoreLineupEntry] = []
    for roster, player in roster_rows:
        raw = (
            session.execute(
                select(PlayerStatsRaw).where(
                    PlayerStatsRaw.player_id == player.player_id,
                    PlayerStatsRaw.season_year == season_year,
                    PlayerStatsRaw.week == week,
                )
            )
            .scalars()
            .first()
        )
        scored = (
            session.execute(
                select(PlayerStatsScored)
                .join(Season, Season.season_id == PlayerStatsScored.season_id)
                .where(
                    PlayerStatsScored.player_id == player.player_id,
                    Season.year == season_year,
                    PlayerStatsScored.week == week,
                )
            )
            .scalars()
            .first()
        )
        lineup.append(
            BoxScoreLineupEntry(
                roster_slot=roster.roster_slot,
                player_id=player.player_id,
                player_name=player.name_full,
                raw_stats=dict(raw.stats or {}) if raw else {},
                league_points=scored.total_points if scored else None,
                breakdown=dict(scored.points_breakdown or {}) if scored else {},
                status=_lineup_status(
                    scored=scored,
                    roster_slot=roster.roster_slot,
                    nfl_team=player.nfl_team,
                    played_franchises=played_franchises,
                ),
            )
        )
    return BoxScoreSide(
        team_id=team_id,
        team_name=team.team_name if team else None,
        owner_name=owner.display_name if owner else None,
        total_score=total_score,
        lineup=lineup,
    )


@router.get("/{matchup_id}/box-score", response_model=Envelope[BoxScore])
def get_box_score_endpoint(
    matchup_id: int,
    session: SessionDep,
) -> Envelope[BoxScore]:
    matchup = get_matchup(session, matchup_id)
    if matchup is None:
        raise not_found(f"No matchup with id {matchup_id}")
    season = session.get(Season, matchup.season_id)
    season_year = season.year if season else 0

    # Which NFL teams played this week — shared by both sides, so resolve once.
    played_franchises = nfl_franchises_that_played(session, season_year, matchup.week)

    home = _build_side(
        session, matchup.team_id, season_year, matchup.week, matchup.team_score, played_franchises
    )
    away: BoxScoreSide | None = None
    if matchup.opponent_team_id is not None:
        away = _build_side(
            session,
            matchup.opponent_team_id,
            season_year,
            matchup.week,
            matchup.opponent_score,
            played_franchises,
        )

    winner: int | None = None
    if matchup.is_win is True:
        winner = matchup.team_id
    elif matchup.is_win is False and matchup.opponent_team_id is not None:
        winner = matchup.opponent_team_id

    return Envelope(
        data=BoxScore(
            matchup_id=matchup_id,
            season_year=season_year,
            week=matchup.week,
            is_playoff=matchup.is_playoff,
            home=home,
            away=away,
            winner_team_id=winner,
        ),
        meta=build_meta(session, entity_updated_at=matchup.updated_at),
    )
