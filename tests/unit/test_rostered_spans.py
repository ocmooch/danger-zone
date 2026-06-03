"""Tests for the materialized league-relevance span.

``recompute_rostered_spans`` derives ``players.first/last_rostered_season``
from ``team_rosters`` (MIN/MAX season_year, NULL when never rostered), and
``search_players(league_relevant=...)`` filters on it. Together they let the
read API answer "was this player ever in THIS league?" — a historical fact
distinct from ``is_active`` (a current-NFL fact) — without dashboard-side
joins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Owner,
    Player,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.repository.queries import search_players

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'spans.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _team(ss: Session, *, season_year: int) -> int:
    """Build the minimal League -> Season -> Owner+Team chain; return team_id."""
    league = League(league_id=f"L{season_year}", name="L", platform="nfl_com")
    ss.add(league)
    ss.flush()
    season = Season(league_id=league.league_id, year=season_year)
    ss.add(season)
    ss.flush()
    owner = Owner(league_id=league.league_id, display_name=f"Owner {season_year}")
    ss.add(owner)
    ss.flush()
    team = Team(season_id=season.season_id, owner_id=owner.owner_id, team_name="T")
    ss.add(team)
    ss.flush()
    return team.team_id


def _roster(ss: Session, *, team_id: int, player_id: int, season_year: int, week: int) -> None:
    ss.add(
        TeamRoster(
            team_id=team_id,
            player_id=player_id,
            season_year=season_year,
            week=week,
            roster_slot="QB",
        )
    )


def test_recompute_sets_min_max_span_and_nulls_for_unrostered(session: Session) -> None:
    rostered = Player(name_full="Rostered Star", position="WR", is_active=True)
    ghost = Player(name_full="Never Here", position="QB", is_active=True)
    session.add_all([rostered, ghost])
    session.flush()

    team_2012 = _team(session, season_year=2012)
    team_2018 = _team(session, season_year=2018)
    # Same player rostered across two seasons -> span is the MIN..MAX.
    _roster(session, team_id=team_2012, player_id=rostered.player_id, season_year=2012, week=1)
    _roster(session, team_id=team_2018, player_id=rostered.player_id, season_year=2018, week=5)
    session.flush()

    recompute_rostered_spans(session)
    session.flush()

    session.refresh(rostered)
    session.refresh(ghost)
    assert rostered.first_rostered_season == 2012
    assert rostered.last_rostered_season == 2018
    # Never rostered -> NULL span (the league-irrelevant marker), not 0.
    assert ghost.first_rostered_season is None
    assert ghost.last_rostered_season is None


def test_recompute_is_idempotent_and_self_healing(session: Session) -> None:
    player = Player(name_full="Span Guy", position="RB", is_active=True)
    session.add(player)
    session.flush()
    # Seed a stale span that no roster row justifies; recompute must clear it.
    player.first_rostered_season = 1999
    player.last_rostered_season = 1999
    session.flush()

    recompute_rostered_spans(session)
    session.flush()
    session.refresh(player)
    assert player.first_rostered_season is None
    assert player.last_rostered_season is None

    # Running twice with no roster change leaves the result unchanged.
    recompute_rostered_spans(session)
    session.flush()
    session.refresh(player)
    assert player.last_rostered_season is None


def test_search_players_league_relevant_filter(session: Session) -> None:
    rostered = Player(name_full="Rostered Star", position="WR", is_active=True)
    ghost = Player(name_full="Never Here", position="QB", is_active=True)
    session.add_all([rostered, ghost])
    session.flush()
    team = _team(session, season_year=2015)
    _roster(session, team_id=team, player_id=rostered.player_id, season_year=2015, week=1)
    session.flush()
    recompute_rostered_spans(session)
    session.flush()

    relevant = search_players(session, league_relevant=True)
    assert [p.name_full for p in relevant] == ["Rostered Star"]

    ghosts = search_players(session, league_relevant=False)
    assert [p.name_full for p in ghosts] == ["Never Here"]

    # None (the default) applies no league-relevance filter.
    assert len(search_players(session, league_relevant=None)) == 2
