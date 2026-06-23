"""Unit tests for the 2022 Week-17 no-contest championship override.

Seeds a minimal 2022 league (title game + a few affected players, plus a
control player with a real wk17 stat row) and asserts the override:

* derives the affected set from public data (excludes the control)
* writes the ``hamlin_substitute`` provenance contract on every affected slot
* flips the champion and swaps final ranks from the corrected title score
* is idempotent (re-running produces the same result)
* no-ops when the 2022 season is absent
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from ff_pipeline.overrides import apply_hamlin_2022_wk17_override
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Matchup,
    Owner,
    Player,
    PlayerStatsRaw,
    ScoringRule,
    Season,
    Team,
    TeamRoster,
)

LEAGUE_ID = "36271"

# player_ids that match the override's hardcoded wk17-partial reconstruction.
BURROW = 4236
BASS = 2331
HIGGINS = 10930
MOSS = 16993  # control: a real wk17 row, must NOT be classified as affected
FILLER_CMC = 90001
FILLER_DOUBS = 90002


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'hamlin.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _rules(season_id: int) -> list[ScoringRule]:
    def rule(category: str, stat_key: str, ppu: float, unit: float = 1.0) -> ScoringRule:
        return ScoringRule(
            season_id=season_id,
            category=category,
            stat_key=stat_key,
            points_per_unit=ppu,
            unit_size=unit,
            threshold_min=0.0,
        )

    return [
        rule("passing", "passing_yards", 1.0, 25.0),
        rule("passing", "passing_tds", 4.0),
        rule("receiving", "receptions", 1.0),
        rule("receiving", "receiving_yards", 1.0, 10.0),
        rule("receiving", "receiving_tds", 6.0),
        rule("kicking", "field_goal_made_20_29", 3.0),
    ]


def _player(pid: int, name: str, pos: str) -> Player:
    return Player(player_id=pid, name_full=name, position=pos)


def _roster(team_id: int, pid: int, slot: str, *, starter: bool, points: float) -> TeamRoster:
    return TeamRoster(
        team_id=team_id,
        player_id=pid,
        season_year=2022,
        week=17,
        roster_slot=slot,
        is_starter=starter,
        extra_data={"nfl_com_points": points, "game_status": "CAN,0-0"},
    )


def _wk19(pid: int, team: str, stats: dict[str, float]) -> PlayerStatsRaw:
    return PlayerStatsRaw(
        player_id=pid,
        season_year=2022,
        week=19,
        season_type="POST",
        source="nflverse",
        nfl_team=team,
        is_primary=True,
        ingested_at=datetime.now(tz=UTC),
        stats=stats,
    )


def _seed(session: Session) -> tuple[int, int]:
    """Seed a 2022 title game: CMC (champ) vs Doubs (runner). Returns the team ids."""
    session.add(League(league_id=LEAGUE_ID, name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id=LEAGUE_ID, year=2022, status="completed")
    session.add(season)
    session.add(Owner(owner_id=1, league_id=LEAGUE_ID))
    session.flush()
    sid = season.season_id

    cmc = Team(season_id=sid, owner_id=1, team_name="CMC", final_rank=1, playoff_finish=1)
    doubs = Team(season_id=sid, owner_id=1, team_name="Doubs", final_rank=2, playoff_finish=2)
    session.add_all([cmc, doubs])
    session.add_all(_rules(sid))
    session.add_all(
        [
            _player(BURROW, "Joe Burrow", "QB"),
            _player(BASS, "Tyler Bass", "K"),
            _player(HIGGINS, "Tee Higgins", "WR"),
            _player(MOSS, "Zack Moss", "RB"),
            _player(FILLER_CMC, "Filler CMC", "WR"),
            _player(FILLER_DOUBS, "Filler Doubs", "WR"),
        ]
    )
    session.flush()

    # CMC starters: Higgins (affected) at 0 + a 50-pt filler -> base 50.
    session.add_all(
        [
            _roster(cmc.team_id, HIGGINS, "WR", starter=True, points=0.0),
            _roster(cmc.team_id, FILLER_CMC, "WR", starter=True, points=50.0),
        ]
    )
    # Doubs starters: Burrow + Bass (affected) at 0, Moss (control) 10, filler 30 -> base 40.
    session.add_all(
        [
            _roster(doubs.team_id, BURROW, "QB", starter=True, points=0.0),
            _roster(doubs.team_id, BASS, "K", starter=True, points=0.0),
            _roster(doubs.team_id, MOSS, "RB", starter=True, points=10.0),
            _roster(doubs.team_id, FILLER_DOUBS, "WR", starter=True, points=30.0),
        ]
    )

    # Affected players: a wk19 BUF/CIN row, no wk17 row.
    session.add_all(
        [
            _wk19(BURROW, "CIN", {"passing_yards": 250.0, "passing_tds": 2.0}),  # 18.0
            _wk19(BASS, "BUF", {"field_goal_made_20_29": 3.0}),  # 9.0
            _wk19(
                HIGGINS, "CIN", {"receptions": 1.0, "receiving_yards": 90.0, "receiving_tds": 1.0}
            ),  # 16.0
        ]
    )
    # Control: Moss has a REAL wk17 row (different game) AND a wk19 row, but the
    # wk17 row means he is not a no-contest player.
    session.add(
        PlayerStatsRaw(
            player_id=MOSS,
            season_year=2022,
            week=17,
            season_type="REG",
            source="nflverse",
            nfl_team="IND",
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
            stats={"rushing_yards": 50.0},
        )
    )

    # Title game (playoff, not consolation), mirrored: CMC 50 def. Doubs 40.
    session.add_all(
        [
            Matchup(
                season_id=sid,
                week=17,
                team_id=cmc.team_id,
                opponent_team_id=doubs.team_id,
                team_score=50.0,
                opponent_score=40.0,
                is_win=True,
                is_playoff=True,
                is_consolation=False,
            ),
            Matchup(
                season_id=sid,
                week=17,
                team_id=doubs.team_id,
                opponent_team_id=cmc.team_id,
                team_score=40.0,
                opponent_score=50.0,
                is_win=False,
                is_playoff=True,
                is_consolation=False,
            ),
        ]
    )
    season.champion_team_id = cmc.team_id
    season.runner_up_team_id = doubs.team_id
    session.commit()
    return cmc.team_id, doubs.team_id


def test_affected_set_excludes_real_wk17(session: Session) -> None:
    _seed(session)
    apply_hamlin_2022_wk17_override(session, league_id=LEAGUE_ID)
    session.commit()
    moss = session.query(TeamRoster).filter_by(season_year=2022, week=17, player_id=MOSS).one()
    assert "hamlin_substitute" not in (moss.extra_data or {})


def test_provenance_contract_written(session: Session) -> None:
    _seed(session)
    apply_hamlin_2022_wk17_override(session, league_id=LEAGUE_ID)
    session.commit()
    burrow = session.query(TeamRoster).filter_by(season_year=2022, week=17, player_id=BURROW).one()
    sub = burrow.extra_data["hamlin_substitute"]
    assert sub["basis"] == "no_contest_wk17partial_plus_wk19"
    # wk17 partial 52yd + 1 TD -> 2.08 + 4 = 6.08; wk19 18.0 -> league 24.08.
    assert sub["wk17_partial"]["points"] == pytest.approx(6.08)
    assert sub["wk19"]["points"] == pytest.approx(18.0)
    assert sub["league_points"] == pytest.approx(24.08)
    # nfl_com_points mirrors league_points so existing readers sum correctly.
    assert burrow.extra_data["nfl_com_points"] == pytest.approx(24.08)
    # combined breakdown sums to league_points.
    assert sum(sub["points_breakdown"].values()) == pytest.approx(24.08)


def test_champion_flips_and_ranks_swap(session: Session) -> None:
    cmc_id, doubs_id = _seed(session)
    result = apply_hamlin_2022_wk17_override(session, league_id=LEAGUE_ID)
    session.commit()

    # Doubs new total: Burrow 24.08 + Bass 12.0 + Moss 10 + filler 30 = 76.08.
    # CMC new total: Higgins 18.3 + filler 50 = 68.3. Doubs overtakes CMC.
    title = (
        session.query(Matchup).filter_by(season_id=result_season(session), team_id=doubs_id).one()
    )
    assert title.team_score == pytest.approx(76.08)
    assert title.opponent_score == pytest.approx(68.3)
    assert title.is_win is True

    season = session.query(Season).filter_by(year=2022).one()
    assert season.champion_team_id == doubs_id
    assert season.runner_up_team_id == cmc_id
    assert session.get(Team, doubs_id).final_rank == 1
    assert session.get(Team, doubs_id).playoff_finish == 1
    assert session.get(Team, cmc_id).final_rank == 2
    assert result.standings_swapped == (cmc_id, doubs_id)
    assert result.unexpected_flips == []


def test_idempotent(session: Session) -> None:
    _cmc_id, doubs_id = _seed(session)
    apply_hamlin_2022_wk17_override(session, league_id=LEAGUE_ID)
    session.commit()
    first_burrow = (
        session.query(TeamRoster).filter_by(season_year=2022, week=17, player_id=BURROW).one()
    ).extra_data["nfl_com_points"]

    result = apply_hamlin_2022_wk17_override(session, league_id=LEAGUE_ID)
    session.commit()
    second_burrow = (
        session.query(TeamRoster).filter_by(season_year=2022, week=17, player_id=BURROW).one()
    ).extra_data["nfl_com_points"]

    assert first_burrow == second_burrow == pytest.approx(24.08)
    # Re-deriving when already corrected does not swap again.
    assert result.standings_swapped is None
    assert session.query(Season).filter_by(year=2022).one().champion_team_id == doubs_id


def test_no_op_when_season_absent(session: Session) -> None:
    session.add(League(league_id=LEAGUE_ID, name="The Danger Zone", platform="nfl_com"))
    session.commit()
    result = apply_hamlin_2022_wk17_override(session, league_id=LEAGUE_ID)
    assert result.applied is False


def result_season(session: Session) -> int:
    return session.query(Season).filter_by(year=2022).one().season_id
