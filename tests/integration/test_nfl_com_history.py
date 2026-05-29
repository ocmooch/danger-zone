"""Integration tests for the NFL.com history reconstruction.

Covers the two pieces of logic that the parser unit tests can't reach:

* :func:`reconstruct_standings` mapping the parsed finish order onto the
  DB — including overwriting the (wrong, current-era) team names the
  earlier backfill stamped, and deriving the regular-season-week boundary
  from the champion's game count.
* :func:`derive_team_records` counting only regular-season weeks once
  :func:`reconstruct_matchups` has classified playoff weeks by that
  boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nfl_com.history import (
    derive_team_records,
    reconstruct_standings,
)
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import League, Matchup, Owner, Season, Team

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "nfl_com_html"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class _StandingsStub:
    def get_html(self, url: str) -> str:
        assert "standings" in url
        return _load("standings_2024.html")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed_league_and_teams(session: Session, *, year: int = 2024, n_teams: int = 12) -> int:
    """Seed a league/season/owners/teams the way the earlier backfill did.

    Team names are deliberately *wrong* (current-era placeholders) so the
    standings reconstruction has something to correct. ``team_abbrev``
    holds the NFL.com team id (1..n).
    """
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=year, status="in_progress")
    session.add(season)
    session.flush()
    for nfl_team_id in range(1, n_teams + 1):
        owner = Owner(league_id="36271", display_name=f"owner{nfl_team_id}", is_active=True)
        session.add(owner)
        session.flush()
        session.add(
            Team(
                season_id=season.season_id,
                owner_id=owner.owner_id,
                team_name=f"PLACEHOLDER NAME {nfl_team_id}",
                team_abbrev=str(nfl_team_id),
            )
        )
    session.flush()
    return season.season_id


@pytest.mark.integration
def test_reconstruct_standings_sets_finish_order_and_fixes_names(session: Session) -> None:
    season_id = _seed_league_and_teams(session)
    outcome = reconstruct_standings(
        session, league_id="36271", year=2024, fetcher=_StandingsStub()
    )
    session.commit()

    assert outcome.teams_ranked == 12

    season = session.get(Season, season_id)
    assert season is not None
    assert season.status == "completed"
    # Champion's record on the standings fixture is 8-6-0 → 14 reg weeks.
    assert season.regular_season_weeks == 14

    # NFL team id 6 is the 2024 champion "Putting the CAP in CHAMP".
    nfl6 = session.execute(
        select(Team).where(Team.season_id == season_id, Team.team_abbrev == "6")
    ).scalar_one()
    assert season.champion_team_id == nfl6.team_id
    assert nfl6.final_rank == 1
    assert nfl6.playoff_finish == 1
    # The placeholder name must have been replaced with the real per-season one.
    assert nfl6.team_name == "Putting the CAP in CHAMP"
    assert (nfl6.regular_season_wins, nfl6.regular_season_losses) == (8, 6)
    assert nfl6.regular_season_points_for == pytest.approx(1765.40)

    # Last place (12th) is NFL team id 11 on the fixture.
    nfl11 = session.execute(
        select(Team).where(Team.season_id == season_id, Team.team_abbrev == "11")
    ).scalar_one()
    assert season.last_place_team_id == nfl11.team_id
    assert nfl11.final_rank == 12


@pytest.mark.integration
def test_derive_team_records_counts_regular_season_only(session: Session) -> None:
    season_id = _seed_league_and_teams(session, n_teams=2)
    season = session.get(Season, season_id)
    assert season is not None
    season.regular_season_weeks = 2
    season.playoff_weeks = 1
    team_a, team_b = (
        session.execute(select(Team).where(Team.season_id == season_id))
        .scalars()
        .all()
    )

    # Two regular-season weeks: A wins both. One playoff week: B wins —
    # must NOT count toward A's regular-season record.
    def _pair(week: int, is_playoff: bool, a_score: float, b_score: float) -> list[Matchup]:
        return [
            Matchup(
                season_id=season_id,
                week=week,
                team_id=team_a.team_id,
                opponent_team_id=team_b.team_id,
                team_score=a_score,
                opponent_score=b_score,
                is_win=a_score > b_score,
                is_playoff=is_playoff,
                is_consolation=False,
            ),
            Matchup(
                season_id=season_id,
                week=week,
                team_id=team_b.team_id,
                opponent_team_id=team_a.team_id,
                team_score=b_score,
                opponent_score=a_score,
                is_win=b_score > a_score,
                is_playoff=is_playoff,
                is_consolation=False,
            ),
        ]

    for m in (
        *_pair(1, False, 100.0, 90.0),
        *_pair(2, False, 110.0, 95.0),
        *_pair(3, True, 50.0, 120.0),  # playoff — excluded
    ):
        session.add(m)
    session.flush()

    updated = derive_team_records(session, league_id="36271", year=2024)
    session.commit()
    assert updated == 2

    session.refresh(team_a)
    session.refresh(team_b)
    # A: 2-0 regular season, 210 PF / 185 PA (playoff week excluded).
    assert (team_a.regular_season_wins, team_a.regular_season_losses) == (2, 0)
    assert team_a.regular_season_points_for == pytest.approx(210.0)
    assert team_a.regular_season_points_against == pytest.approx(185.0)
    assert (team_b.regular_season_wins, team_b.regular_season_losses) == (0, 2)
