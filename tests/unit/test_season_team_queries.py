"""Unit tests for season-correct NFL-team query helpers (F-54).

Seeds a tiny DB with per-week ``player_stats_raw`` rows carrying the team a
player was on that week (including a mid-season trade and a relocated
franchise), plus matching ``player_stats_scored`` rows, then asserts:

* ``player_season_teams`` / ``player_nfl_team`` resolve the modal team for a
  season (ties broken by latest week) and omit players with no stored team.
* ``season_totals`` renders the season team, not the current snapshot, and
  falls back to ``players.nfl_team`` when nothing is stored.
* ``top_scorers`` renders the team a player was on that *exact* week.
* A relocated franchise keeps its season-era code (a 2015 Raider reads "OAK").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    Season,
)
from ff_pipeline.repository.queries import (
    player_nfl_team,
    player_season_teams,
    season_totals,
    top_scorers,
)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'season_team.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _add_week(
    session: Session,
    *,
    player: Player,
    season: Season,
    week: int,
    nfl_team: str | None,
    points: float,
    is_primary: bool = True,
) -> None:
    """Add a matching (raw, scored) pair for one player-week."""
    raw = PlayerStatsRaw(
        player_id=player.player_id,
        season_year=season.year,
        week=week,
        season_type="REG",
        nfl_team=nfl_team,
        source="nflverse",
        stats={},
        is_primary=is_primary,
    )
    session.add(raw)
    session.flush()
    session.add(
        PlayerStatsScored(
            stat_id=raw.stat_id,
            season_id=season.season_id,
            player_id=player.player_id,
            week=week,
            total_points=points,
        )
    )


@pytest.fixture
def seeded(session: Session) -> tuple[Season, Player, Player, Player]:
    """Three players in a 2015 season:

    * ``traded`` — weeks 1-3 on "NYG", weeks 4-6 on "DAL" (3-3 tie → latest-week "DAL").
    * ``raider`` — every week stored as nflverse's current "LV"; the helpers
      render it as the season-era "OAK" (the Raiders moved in 2020).
    * ``snapshot_only`` — a scored row whose raw row stored no team, so the
      helpers fall back to the current ``players.nfl_team`` snapshot.
    """
    league = League(league_id="36271", name="The Danger Zone", platform="nfl_com")
    season = Season(league_id="36271", year=2015, status="completed")
    traded = Player(name_full="Traded WR", position="WR", nfl_team="DAL")
    raider = Player(name_full="Raider RB", position="RB", nfl_team="LV")
    snapshot_only = Player(name_full="Snapshot QB", position="QB", nfl_team="KC")
    session.add_all([league, season, traded, raider, snapshot_only])
    session.flush()

    for wk in (1, 2, 3):
        _add_week(session, player=traded, season=season, week=wk, nfl_team="NYG", points=10.0)
    for wk in (4, 5, 6):
        _add_week(session, player=traded, season=season, week=wk, nfl_team="DAL", points=20.0)
    for wk in (1, 2, 3, 4):
        _add_week(session, player=raider, season=season, week=wk, nfl_team="LV", points=15.0)
    # Stored team is NULL → leaderboard must fall back to the snapshot.
    _add_week(session, player=snapshot_only, season=season, week=1, nfl_team=None, points=30.0)

    session.commit()
    return season, traded, raider, snapshot_only


def test_player_season_teams_picks_modal_team(
    session: Session, seeded: tuple[Season, Player, Player, Player]
) -> None:
    _, traded, raider, snapshot_only = seeded
    teams = player_season_teams(
        session, [traded.player_id, raider.player_id, snapshot_only.player_id], 2015
    )
    # traded played 3 weeks on NYG vs 3 on DAL → tie broken by latest week (DAL).
    assert teams[traded.player_id] == "DAL"
    # Stored as current "LV"; rendered as the 2015-era "OAK".
    assert teams[raider.player_id] == "OAK"
    # snapshot_only has no stored team that season → absent from the map.
    assert snapshot_only.player_id not in teams


def test_player_season_teams_modal_beats_recency(session: Session, seeded: tuple) -> None:
    """When one team has strictly more weeks, it wins over a later cameo."""
    season, traded, *_ = seeded
    # One extra NYG week (now 4 NYG vs 3 DAL) flips the modal team back to NYG
    # even though the DAL weeks are more recent.
    _add_week(session, player=traded, season=season, week=7, nfl_team="NYG", points=5.0)
    session.commit()
    assert player_nfl_team(session, traded.player_id, 2015) == "NYG"


def test_player_season_teams_empty_input(session: Session) -> None:
    assert player_season_teams(session, [], 2015) == {}


def test_season_totals_uses_season_team_with_snapshot_fallback(
    session: Session, seeded: tuple[Season, Player, Player, Player]
) -> None:
    _, traded, raider, snapshot_only = seeded
    rows = {r["player_id"]: r for r in season_totals(session, 2015)}
    # Season-correct, not the current snapshot ("DAL"/"LV"/"KC" on players).
    assert rows[raider.player_id]["nfl_team"] == "OAK"
    assert rows[traded.player_id]["nfl_team"] == "DAL"
    # No stored team → falls back to the current snapshot.
    assert rows[snapshot_only.player_id]["nfl_team"] == "KC"


def test_top_scorers_uses_exact_week_team(
    session: Session, seeded: tuple[Season, Player, Player, Player]
) -> None:
    _, traded, *_ = seeded
    week2 = {
        r["player_id"]: r
        for r in top_scorers(session, season_year=2015, week=2, position=None, limit=50)
    }
    week5 = {
        r["player_id"]: r
        for r in top_scorers(session, season_year=2015, week=5, position=None, limit=50)
    }
    # Same player, different team pre/post-trade.
    assert week2[traded.player_id]["nfl_team"] == "NYG"
    assert week5[traded.player_id]["nfl_team"] == "DAL"


def test_season_for_unknown_year_is_empty(session: Session, seeded: tuple) -> None:
    _, traded, *_ = seeded
    assert player_nfl_team(session, traded.player_id, 1999) is None
