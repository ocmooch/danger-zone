"""Unit tests for the M9 verifier.

Seeds a minimal DB and a stub fetcher that returns hand-built gamecenter
HTML. Exercises:

* Single-player verification: pass within tolerance, fail outside,
  missing-data branches
* Sweep mode: walks teams once per week, compares every starter
* Looks up player via ``nfl_com_player_id`` only — fuzzy-matching by
  name is out of scope (the resolver owns that)
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    Matchup,
    Owner,
    Player,
    PlayerStatsRaw,
    ScoringRule,
    Season,
    Team,
)
from ff_pipeline.scoring.verify import (
    VerifyComparison,
    verify_player,
    verify_season_sweep,
)

# ---------------------------------------------------------------------------
# Stub gamecenter HTML
# ---------------------------------------------------------------------------


def _build_gamecenter_html(
    *,
    home_team_id: int,
    home_team_name: str,
    home_total: float,
    away_team_id: int,
    away_team_name: str,
    away_total: float,
    home_starters: list[tuple[str, str, float]],
    away_starters: list[tuple[str, str, float]],
) -> str:
    """Synthesize HTML the gamecenter parser accepts.

    Each starter is ``(slot, nfl_com_player_id, points)``.
    """

    def _player_row(slot: str, nfl_id: str, points: float) -> str:
        return f"""
        <tr>
          <td class="teamPosition"><span>{slot}</span></td>
          <td class="playerNameAndInfo">
            <a class="playerName playerNameId-{nfl_id}"
               href="/players/card?leagueId=1&playerId={nfl_id}">
               P {nfl_id}
            </a>
            <em>QB - CIN</em>
          </td>
          <td class="playerOpponent">@TST</td>
          <td class="playerGameStatus">Final</td>
          <td class="playerStats">stats</td>
          <td class="stat statTotal numeric last">
            <span class="playerTotal">{points:.2f}</span>
          </td>
        </tr>
        """

    home_rows = "".join(_player_row(s, pid, pts) for s, pid, pts in home_starters)
    away_rows = "".join(_player_row(s, pid, pts) for s, pid, pts in away_starters)

    return f"""
    <html><body>
      <div class="teamWrap teamWrap-1">
        <a class="teamName" href="/team/{home_team_id}">{home_team_name}</a>
        <span class="teamTotal">{home_total:.2f}</span>
        <table class="tableType-player"><tbody>{home_rows}</tbody></table>
      </div>
      <div class="teamWrap teamWrap-2">
        <a class="teamName" href="/team/{away_team_id}">{away_team_name}</a>
        <span class="teamTotal">{away_total:.2f}</span>
        <table class="tableType-player"><tbody>{away_rows}</tbody></table>
      </div>
    </body></html>
    """


class _Fetcher:
    """Tiny in-memory URL → HTML map used as the verify fetcher."""

    def __init__(self) -> None:
        self.responses: dict[str, str] = {}
        self.calls: list[str] = []

    def add(self, *, year: int, team_id: int, week: int, html: str) -> None:
        key = f"history/{year}/teamgamecenter?teamId={team_id}&week={week}"
        self.responses[key] = html

    def get_html(self, url: str) -> str:
        self.calls.append(url)
        for key, html in self.responses.items():
            if key in url:
                return html
        raise FileNotFoundError(f"no stub for url: {url}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'verify.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed(session: Session) -> tuple[Season, Player, Player, Team, Team]:
    league = League(league_id="36271", name="The Danger Zone", platform="nfl_com")
    session.add(league)
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    session.flush()
    session.add_all(
        [
            ScoringRule(
                season_id=season.season_id,
                category="passing",
                stat_key="passing_yards",
                points_per_unit=1.0,
                unit_size=25.0,
                threshold_min=0.0,
            ),
            ScoringRule(
                season_id=season.season_id,
                category="passing",
                stat_key="passing_tds",
                points_per_unit=4.0,
                unit_size=1.0,
                threshold_min=0.0,
            ),
        ]
    )
    owner_a = Owner(league_id="36271", display_name="Alpha")
    owner_b = Owner(league_id="36271", display_name="Bravo")
    session.add_all([owner_a, owner_b])
    session.flush()
    team_a = Team(
        season_id=season.season_id,
        owner_id=owner_a.owner_id,
        team_name="Alpha Team",
        team_abbrev="1",  # NFL.com team_id stashed as abbrev
    )
    team_b = Team(
        season_id=season.season_id,
        owner_id=owner_b.owner_id,
        team_name="Bravo Team",
        team_abbrev="2",
    )
    session.add_all([team_a, team_b])
    session.flush()

    player_a = Player(
        name_full="Player Alpha",
        position="QB",
        nfl_com_player_id="111",
        gsis_id="00-0000111",
    )
    player_b = Player(
        name_full="Player Bravo",
        position="QB",
        nfl_com_player_id="222",
        gsis_id="00-0000222",
    )
    session.add_all([player_a, player_b])
    session.flush()

    # Matchup wiring the two teams in week 1.
    session.add(
        Matchup(
            season_id=season.season_id,
            week=1,
            team_id=team_a.team_id,
            opponent_team_id=team_b.team_id,
            team_score=22.0,
            opponent_score=20.0,
            is_win=True,
        )
    )
    # Raw stats: A → 250 yds + 1 TD → 10 + 4 = 14.0; B → 200 yds + 0 TDs → 8.0
    session.add(
        PlayerStatsRaw(
            player_id=player_a.player_id,
            season_year=2024,
            week=1,
            season_type="REG",
            source="nflverse",
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
            stats={"passing_yards": 250.0, "passing_tds": 1.0},
        )
    )
    session.add(
        PlayerStatsRaw(
            player_id=player_b.player_id,
            season_year=2024,
            week=1,
            season_type="REG",
            source="nflverse",
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
            stats={"passing_yards": 200.0, "passing_tds": 0.0},
        )
    )
    session.commit()
    return season, player_a, player_b, team_a, team_b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_verify_player_pass_within_tolerance(session: Session) -> None:
    _seed(session)
    fetcher = _Fetcher()
    html = _build_gamecenter_html(
        home_team_id=1,
        home_team_name="Alpha",
        home_total=14.0,
        away_team_id=2,
        away_team_name="Bravo",
        away_total=8.0,
        # NFL.com reports 14.05 — within tolerance 0.1 of our 14.0.
        home_starters=[("QB", "111", 14.05)],
        away_starters=[("QB", "222", 8.0)],
    )
    fetcher.add(year=2024, team_id=1, week=1, html=html)
    fetcher.add(year=2024, team_id=2, week=1, html=html)

    result = verify_player(
        session,
        league_id="36271",
        player_name="Player Alpha",
        season_year=2024,
        week=1,
        fetcher=fetcher,
        tolerance=0.1,
    )
    assert isinstance(result, VerifyComparison)
    assert result.passed is True
    assert result.our_points == pytest.approx(14.0)
    assert result.nfl_com_points == pytest.approx(14.05)
    assert result.note is None


def test_verify_player_fail_outside_tolerance(session: Session) -> None:
    _seed(session)
    fetcher = _Fetcher()
    # NFL.com 14.5 vs ours 14.0 → delta 0.5 > tolerance 0.1.
    html = _build_gamecenter_html(
        home_team_id=1,
        home_team_name="Alpha",
        home_total=14.5,
        away_team_id=2,
        away_team_name="Bravo",
        away_total=8.0,
        home_starters=[("QB", "111", 14.5)],
        away_starters=[("QB", "222", 8.0)],
    )
    fetcher.add(year=2024, team_id=1, week=1, html=html)
    fetcher.add(year=2024, team_id=2, week=1, html=html)
    result = verify_player(
        session,
        league_id="36271",
        player_name="Player Alpha",
        season_year=2024,
        week=1,
        fetcher=fetcher,
    )
    assert result.passed is False
    assert result.delta is not None
    assert abs(result.delta) > 0.1


def test_verify_player_returns_clean_error_when_player_not_found(
    session: Session,
) -> None:
    _seed(session)
    result = verify_player(
        session,
        league_id="36271",
        player_name="No Such Player",
        season_year=2024,
        week=1,
        fetcher=_Fetcher(),
    )
    assert result.passed is False
    assert result.note is not None and "player_not_found" in result.note


def test_verify_player_handles_missing_raw_stats(session: Session) -> None:
    _seed(session)
    # Drop the raw row for player_b — we'll verify against player_b who
    # has no raw stats on file.
    session.query(PlayerStatsRaw).filter_by(player_id=2).delete()
    session.commit()
    fetcher = _Fetcher()
    html = _build_gamecenter_html(
        home_team_id=1,
        home_team_name="Alpha",
        home_total=14.0,
        away_team_id=2,
        away_team_name="Bravo",
        away_total=8.0,
        home_starters=[("QB", "111", 14.0)],
        away_starters=[("QB", "222", 8.0)],
    )
    fetcher.add(year=2024, team_id=1, week=1, html=html)
    fetcher.add(year=2024, team_id=2, week=1, html=html)
    result = verify_player(
        session,
        league_id="36271",
        player_name="Player Bravo",
        season_year=2024,
        week=1,
        fetcher=fetcher,
    )
    assert result.passed is False
    assert result.our_points is None
    assert result.note == "our_raw_stats_missing"


def test_verify_sweep_walks_starters_and_compares(session: Session) -> None:
    _seed(session)
    fetcher = _Fetcher()
    html = _build_gamecenter_html(
        home_team_id=1,
        home_team_name="Alpha",
        home_total=14.0,
        away_team_id=2,
        away_team_name="Bravo",
        away_total=8.0,
        home_starters=[("QB", "111", 14.0)],
        away_starters=[("QB", "222", 8.0)],
    )
    fetcher.add(year=2024, team_id=1, week=1, html=html)
    fetcher.add(year=2024, team_id=2, week=1, html=html)

    report = verify_season_sweep(
        session,
        league_id="36271",
        season_year=2024,
        fetcher=fetcher,
        weeks=(1,),  # only one week seeded
        tolerance=0.1,
    )
    assert report.total == 2
    assert report.passed == 2
    assert report.failed == 0


def test_verify_sweep_returns_empty_when_season_missing(
    session: Session,
) -> None:
    fetcher = _Fetcher()
    report = verify_season_sweep(
        session,
        league_id="nope",
        season_year=2099,
        fetcher=fetcher,
        weeks=(1,),
    )
    assert report.total == 0


__all__ = [
    "test_verify_player_fail_outside_tolerance",
    "test_verify_player_handles_missing_raw_stats",
    "test_verify_player_pass_within_tolerance",
    "test_verify_player_returns_clean_error_when_player_not_found",
    "test_verify_sweep_returns_empty_when_season_missing",
    "test_verify_sweep_walks_starters_and_compares",
]
