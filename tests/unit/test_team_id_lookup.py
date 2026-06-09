"""Franchise lookup must survive duplicate (season, abbrev) rows.

A legacy backfill left franchise-duplicate team rows sharing one NFL.com team
id (stashed in ``team_abbrev``). ``_team_id_lookup`` must deterministically
resolve such an abbrev to the *real* franchise row (the one carrying roster /
matchup data), so re-running the league crawl updates that row in place instead
of resurrecting a phantom.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nfl_com.league import _team_id_lookup
from ff_pipeline.repository.database import Base
from ff_pipeline.repository.models import League, Matchup, Owner, Season, Team

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as ss:
        ss.add(League(league_id="L1"))
        ss.add(Season(season_id=1, league_id="L1", year=2015))
        ss.add(Owner(owner_id=1, league_id="L1", is_active=True))
        ss.flush()
        yield ss
    engine.dispose()


def test_lookup_prefers_ref_rich_row_for_duplicate_abbrev(session: Session) -> None:
    real = Team(team_id=10, season_id=1, owner_id=1, team_name="Real", team_abbrev="7")
    phantom = Team(team_id=99, season_id=1, owner_id=1, team_name="Phantom", team_abbrev="7")
    session.add_all([real, phantom])
    session.add(Matchup(season_id=1, week=1, team_id=10, opponent_score=1.0))
    session.flush()

    # The duplicated abbrev resolves to the row that actually holds data.
    assert _team_id_lookup(session, 1)[7] == 10


def test_lookup_maps_distinct_abbrevs(session: Session) -> None:
    session.add_all(
        [
            Team(team_id=10, season_id=1, owner_id=1, team_name="A", team_abbrev="3"),
            Team(team_id=11, season_id=1, owner_id=1, team_name="B", team_abbrev="7"),
        ]
    )
    session.flush()
    assert _team_id_lookup(session, 1) == {3: 10, 7: 11}


def test_lookup_skips_blank_and_nonnumeric_abbrevs(session: Session) -> None:
    session.add_all(
        [
            Team(team_id=10, season_id=1, owner_id=1, team_name="A", team_abbrev=None),
            Team(team_id=11, season_id=1, owner_id=1, team_name="B", team_abbrev="x"),
            Team(team_id=12, season_id=1, owner_id=1, team_name="C", team_abbrev="5"),
        ]
    )
    session.flush()
    assert _team_id_lookup(session, 1) == {5: 12}
