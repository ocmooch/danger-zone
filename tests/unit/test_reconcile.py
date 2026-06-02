"""Unit tests for team-total reconciliation (DST drift detection).

Seeds a minimal league with one team, a matchup carrying the
authoritative NFL.com total, a starting lineup, and scored rows, then
asserts ``reconcile_team_totals``:

* passes when the summed scored starters match the NFL.com total;
* flags a team (and counts the missing starter) when a starter has no
  scored row — the shape a pre-DST-fix shortfall takes;
* never mutates a score (report-only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Matchup,
    Owner,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.scoring.verify import reconcile_team_totals

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'reconcile.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed(session: Session, *, score_dst: bool) -> None:
    """One team, two starters (a QB and the team DEF), one matchup.

    QB scores 17.0; DST scores 13.0. NFL.com total is 30.0. When
    ``score_dst`` is False the DST starter has no scored row, so our sum
    falls 13 short of the authoritative total.
    """
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    owner = Owner(league_id="36271", nfl_user_id="u1", display_name="Owner One")
    session.add(owner)
    session.flush()
    team = Team(season_id=season.season_id, owner_id=owner.owner_id, team_name="Team One")
    qb = Player(name_full="Test QB", position="QB", gsis_id="00-0000001")
    dst = Player(name_full="San Francisco 49ers", position="DEF", nfl_team="SF")
    session.add_all([team, qb, dst])
    session.flush()

    session.add(
        Matchup(
            season_id=season.season_id,
            week=1,
            team_id=team.team_id,
            team_score=30.0,
            opponent_score=20.0,
        )
    )
    for player in (qb, dst):
        session.add(
            TeamRoster(
                team_id=team.team_id,
                player_id=player.player_id,
                season_year=2024,
                week=1,
                is_starter=True,
            )
        )

    # Scored rows need a backing raw row (FK). Score the QB always; the DST
    # only when the scenario calls for it.
    scored_players = [(qb, 17.0)]
    if score_dst:
        scored_players.append((dst, 13.0))
    for player, pts in scored_players:
        raw = PlayerStatsRaw(
            player_id=player.player_id,
            season_year=2024,
            week=1,
            season_type="REG",
            source="nflverse",
            stats={},
            is_primary=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            PlayerStatsScored(
                stat_id=raw.stat_id,
                season_id=season.season_id,
                player_id=player.player_id,
                week=1,
                total_points=pts,
            )
        )
    session.flush()


def test_reconcile_passes_when_totals_match(session: Session) -> None:
    _seed(session, score_dst=True)
    report = reconcile_team_totals(session, league_id="36271", season_year=2024)
    assert report.total == 1
    c = report.comparisons[0]
    assert c.our_total == 30.0
    assert c.nfl_com_total == 30.0
    assert c.passed is True
    assert c.starters_missing_score == 0


def test_reconcile_flags_missing_dst_score(session: Session) -> None:
    _seed(session, score_dst=False)
    report = reconcile_team_totals(session, league_id="36271", season_year=2024)
    assert report.failed == 1
    c = report.comparisons[0]
    assert c.our_total == 17.0  # only the QB counted
    assert c.nfl_com_total == 30.0
    assert c.delta == pytest.approx(-13.0)
    assert c.passed is False
    assert c.starters_missing_score == 1


def test_reconcile_unknown_season_notes_and_no_rows(session: Session) -> None:
    report = reconcile_team_totals(session, league_id="36271", season_year=1999)
    assert report.total == 0
    assert report.note is not None and "season_not_found" in report.note
