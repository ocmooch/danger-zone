"""Integration test: team-defense ingest → rescore round-trip.

Seeds a DB with a DEF player + the season's defense scoring rules, runs
``run_team_defense`` over a stub source (in-memory polars frames, no
network), and asserts:

* a ``player_stats_raw`` row lands for the matched DEF player with
  ``source='nflverse'`` and the engine-keyed DST stat dict;
* a franchise this league didn't roster is counted as unmatched, not
  stubbed;
* the existing ``rescore`` step scores the DEF raw row to the
  hand-computed DST total;
* re-running the ingest is idempotent (updates, not inserts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nflverse.runner import run_team_defense
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Owner,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    ScoringRule,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.scoring.rescore import rescore_seasons

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'td.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


class _StubSource:
    """Minimal NflverseSource exposing only the two frames the rollup reads."""

    def load_team_stats(self, seasons):  # type: ignore[no-untyped-def]  # noqa: ARG002
        return pl.DataFrame(
            {
                "season": [2024, 2024],
                "week": [3, 3],
                "team": ["SF", "DAL"],
                "season_type": ["REG", "REG"],
                "passing_yards": [300, 180],
                "rushing_yards": [120, 70],
                # SF's DEF sacks come from DAL's offense-side sacks_suffered (4);
                # def_sacks is now only a fallback when the opponent row is absent.
                "sacks_suffered": [1, 4],
                "def_sacks": [4, 1],
                "def_interceptions": [2, 0],
                "fumble_recovery_opp": [1, 0],
                "def_safeties": [0, 0],
                "def_tds": [1, 0],
                "special_teams_tds": [0, 0],
            }
        )

    def load_schedules(self, seasons):  # type: ignore[no-untyped-def]  # noqa: ARG002
        return pl.DataFrame(
            {
                "season": [2024],
                "week": [3],
                "game_type": ["REG"],
                "home_team": ["SF"],
                "away_team": ["DAL"],
                "home_score": [27],
                "away_score": [0],
            }
        )

    # Unused by run_team_defense but part of the source protocol.
    def load_player_stats(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def load_players(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def load_rosters(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _seed(session: Session) -> Player:
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    sf_def = Player(name_full="San Francisco 49ers", position="DEF", nfl_team="SF")
    session.add(sf_def)
    session.flush()

    # The Danger Zone defense rules (counting + the two flat brackets we hit).
    session.add_all(
        [
            ScoringRule(
                season_id=season.season_id,
                category="defense",
                stat_key="sacks",
                points_per_unit=1.0,
                unit_size=1.0,
            ),
            ScoringRule(
                season_id=season.season_id,
                category="defense",
                stat_key="interceptions",
                points_per_unit=2.0,
                unit_size=1.0,
            ),
            ScoringRule(
                season_id=season.season_id,
                category="defense",
                stat_key="fumbles_recovered",
                points_per_unit=2.0,
                unit_size=1.0,
            ),
            ScoringRule(
                season_id=season.season_id,
                category="defense",
                stat_key="defensive_tds",
                points_per_unit=6.0,
                unit_size=1.0,
            ),
            ScoringRule(
                season_id=season.season_id,
                category="defense",
                stat_key="points_allowed",
                points_per_unit=0.0,
                flat_points=10.0,
                threshold_min=0.0,
                threshold_max=0.0,
            ),
            ScoringRule(
                season_id=season.season_id,
                category="defense",
                stat_key="total_yards_allowed",
                points_per_unit=0.0,
                flat_points=4.0,
                threshold_min=200.0,
                threshold_max=299.0,
            ),
        ]
    )
    session.flush()
    return sf_def


@pytest.mark.integration
def test_team_defense_ingest_and_rescore(session: Session) -> None:
    sf_def = _seed(session)

    result = run_team_defense(session, seasons=[2024], source=_StubSource())
    session.commit()

    # SF matched; DAL (not rostered as a DEF) is unmatched, not stubbed.
    assert result.teams_matched == 1
    assert result.teams_unmatched == 1
    assert result.stats_added == 1

    raw = session.execute(
        select(PlayerStatsRaw).where(PlayerStatsRaw.player_id == sf_def.player_id)
    ).scalar_one()
    assert raw.source == "nflverse"
    assert raw.nfl_opponent == "DAL"
    assert raw.stats["points_allowed"] == 0.0
    assert raw.stats["total_yards_allowed"] == 250.0
    assert raw.stats["sacks"] == 4.0

    # No stub player was created for DAL.
    assert session.execute(select(Player).where(Player.name_full == "DAL")).first() is None

    # Existing rescore scores the DEF row with the defense rules.
    rescore_seasons(session, season_years=[2024], league_id="36271")
    session.commit()
    scored = session.execute(
        select(PlayerStatsScored).where(PlayerStatsScored.player_id == sf_def.player_id)
    ).scalar_one()
    # 4 sacks + 2 INT*2 + 1 fum*2 + 1 TD*6 + shutout 10 + 250yds bracket 4 = 30
    assert scored.total_points == 30.0


@pytest.mark.integration
def test_matches_def_by_roster_slot_when_position_mislabeled(session: Session) -> None:
    """A team defense NFL.com tagged with a scrape-artifact position and a
    NULL nfl_team is still matched — identified by its DEF roster slot and
    resolved from its full team name."""
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    owner = Owner(league_id="36271", nfl_user_id="u1", display_name="Owner One")
    session.add(owner)
    session.flush()
    team = Team(season_id=season.season_id, owner_id=owner.owner_id, team_name="Team One")
    # Mislabeled position, NULL nfl_team — exactly how ~half the DEFs land.
    browns = Player(
        name_full="Cleveland Browns",
        position="Season is Over Add to Watch List",
        nfl_team=None,
    )
    session.add_all([team, browns])
    session.flush()
    session.add(
        TeamRoster(
            team_id=team.team_id,
            player_id=browns.player_id,
            season_year=2024,
            week=3,
            roster_slot="DEF",
            is_starter=True,
        )
    )
    session.flush()

    class _CleSource:
        def load_team_stats(self, seasons):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return pl.DataFrame(
                {
                    "season": [2024],
                    "week": [3],
                    "team": ["CLE"],
                    "season_type": ["REG"],
                    "passing_yards": [100],
                    "rushing_yards": [50],
                    "sack_yards_lost": [0],
                    # No PIT (opponent) row in this fixture, so CLE's DEF sacks
                    # fall back to its own def_sacks (2).
                    "sacks_suffered": [0],
                    "def_sacks": [2],
                    "def_interceptions": [1],
                    "fumble_recovery_opp": [0],
                    "def_safeties": [0],
                    "def_tds": [0],
                    "special_teams_tds": [0],
                }
            )

        def load_schedules(self, seasons):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return pl.DataFrame(
                {
                    "season": [2024],
                    "week": [3],
                    "game_type": ["REG"],
                    "home_team": ["CLE"],
                    "away_team": ["PIT"],
                    "home_score": [20],
                    "away_score": [10],
                }
            )

    result = run_team_defense(session, seasons=[2024], source=_CleSource())
    session.commit()

    assert result.teams_matched == 1
    raw = session.execute(
        select(PlayerStatsRaw).where(PlayerStatsRaw.player_id == browns.player_id)
    ).scalar_one()
    assert raw.stats["sacks"] == 2.0
    assert raw.stats["points_allowed"] == 10.0  # PIT scored 10


@pytest.mark.integration
def test_team_defense_ingest_is_idempotent(session: Session) -> None:
    _seed(session)
    run_team_defense(session, seasons=[2024], source=_StubSource())
    session.commit()
    second = run_team_defense(session, seasons=[2024], source=_StubSource())
    session.commit()

    assert second.stats_added == 0
    assert second.stats_updated == 1
    assert len(session.execute(select(PlayerStatsRaw)).all()) == 1
