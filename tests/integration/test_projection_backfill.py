"""Regression test for the playoff-week projection backfill.

Reproduces the upstream coverage defect: the original crawl stopped at the
fantasy regular-season boundary, so playoff/consolation weeks had no Sleeper
projections. Drives ``run_projection_backfill`` against an in-memory recording
source (no network) on a season with 14 regular-season weeks + 3 fantasy
playoff weeks and asserts the backfill:

* derives a week ceiling of 17 from the matchup schedule,
* requests every week 1..17 with ``season_type="regular"`` (never ``post``),
* stores real, non-hollow projection rows for the playoff weeks (15-17),
* is idempotent — a second run skips the now-populated weeks and adds nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.projection_backfill import fantasy_week_ceiling, run_projection_backfill
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Matchup,
    Owner,
    Player,
    Projection,
    ScoringRule,
    Season,
    Team,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

LEAGUE_ID = "TEST-LEAGUE"
SEASON_YEAR = 2024
REGULAR_SEASON_WEEKS = 14
PLAYOFF_WEEKS = 3
MAX_FANTASY_WEEK = REGULAR_SEASON_WEEKS + PLAYOFF_WEEKS  # 17
MAHOMES_SLEEPER_ID = "4034"
MAHOMES_GSIS_ID = "00-0033873"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_app_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade_to_head(engine=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    with Session(db_engine) as ss:
        yield ss


class RecordingSleeperSource:
    """In-memory ``SleeperSource`` that records every projection request.

    Returns one real (non-hollow) projection per week so the backfill stores
    a recognizable row, and records ``(year, week, season_type)`` so the test
    can assert the exact week grid and that the NFL postseason is never hit.
    """

    def __init__(self) -> None:
        self.requested: list[tuple[int, int, str]] = []

    def players(self) -> dict[str, dict[str, Any]]:
        return {
            MAHOMES_SLEEPER_ID: {
                "player_id": MAHOMES_SLEEPER_ID,
                "gsis_id": MAHOMES_GSIS_ID,
                "full_name": "Patrick Mahomes",
                "position": "QB",
                "team": "KC",
                "active": True,
            }
        }

    def projections(
        self, year: int, week: int, *, season_type: str = "regular"
    ) -> list[dict[str, Any]]:
        self.requested.append((year, week, season_type))
        return [
            {
                "player_id": MAHOMES_SLEEPER_ID,
                "season": str(year),
                "week": week,
                "season_type": season_type,
                "stats": {"pass_yd": 300.0, "pass_td": 2.0, "pass_int": 0.5, "rush_yd": 10.0},
            }
        ]

    def trending(
        self,
        kind: str,  # noqa: ARG002 - protocol shape; stub ignores the args
        *,
        lookback_hours: int = 24,  # noqa: ARG002
        limit: int = 25,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        return []


def _seed_season_with_full_schedule(session: Session) -> None:
    """Seed a league/season whose matchup schedule runs through week 17."""

    session.add(League(league_id=LEAGUE_ID, name="Test League", platform="nfl_com"))
    session.flush()

    season = Season(
        league_id=LEAGUE_ID,
        year=SEASON_YEAR,
        regular_season_weeks=REGULAR_SEASON_WEEKS,
        playoff_weeks=PLAYOFF_WEEKS,
    )
    session.add(season)
    session.flush()
    assert season.season_id is not None

    owner = Owner(league_id=LEAGUE_ID, display_name="Tester")
    session.add(owner)
    session.flush()
    teams = [
        Team(season_id=season.season_id, owner_id=owner.owner_id, team_name=f"Team {i}")
        for i in range(2)
    ]
    session.add_all(teams)
    session.flush()

    # One head-to-head per fantasy week, regular season + playoffs/consolation.
    for week in range(1, MAX_FANTASY_WEEK + 1):
        session.add(
            Matchup(
                season_id=season.season_id,
                week=week,
                team_id=teams[0].team_id,
                opponent_team_id=teams[1].team_id,
                is_playoff=week > REGULAR_SEASON_WEEKS,
            )
        )

    # A 1.0/25yd, 4pt-TD, -2 INT passing line so projected_points is non-zero.
    for stat_key, ppu, unit in (
        ("passing_yards", 1.0, 25.0),
        ("passing_tds", 4.0, 1.0),
        ("passing_interceptions", -2.0, 1.0),
        ("rushing_yards", 1.0, 10.0),
    ):
        session.add(
            ScoringRule(
                season_id=season.season_id,
                category="passing",
                stat_key=stat_key,
                points_per_unit=ppu,
                unit_size=unit,
                threshold_min=0.0,
            )
        )

    session.add(
        Player(
            name_full="Patrick Mahomes",
            position="QB",
            nfl_team="KC",
            gsis_id=MAHOMES_GSIS_ID,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ceiling_derived_from_matchup_schedule(session: Session) -> None:
    _seed_season_with_full_schedule(session)
    assert fantasy_week_ceiling(session, league_id=LEAGUE_ID, year=SEASON_YEAR) == MAX_FANTASY_WEEK


@pytest.mark.integration
def test_backfill_requests_and_stores_through_week_17(session: Session) -> None:
    _seed_season_with_full_schedule(session)
    src = RecordingSleeperSource()

    result = run_projection_backfill(
        session,
        league_id=LEAGUE_ID,
        start_year=SEASON_YEAR,
        end_year=SEASON_YEAR,
        source=src,
    )

    # Requested exactly weeks 1..17, all as NFL regular-season type.
    assert [w for _, w, _ in src.requested] == list(range(1, MAX_FANTASY_WEEK + 1))
    assert {st for _, _, st in src.requested} == {"regular"}
    assert "post" not in {st for _, _, st in src.requested}
    assert result.fetched == MAX_FANTASY_WEEK

    # Playoff weeks now carry real, non-hollow projection rows.
    for week in (15, 16, 17):
        row = session.execute(
            select(Projection).where(Projection.season_year == SEASON_YEAR, Projection.week == week)
        ).scalar_one()
        assert row.projected_stats  # non-empty stat dict
        assert row.projected_points is not None and row.projected_points > 0


@pytest.mark.integration
def test_backfill_is_idempotent(session: Session) -> None:
    _seed_season_with_full_schedule(session)

    first = run_projection_backfill(
        session,
        league_id=LEAGUE_ID,
        start_year=SEASON_YEAR,
        end_year=SEASON_YEAR,
        source=RecordingSleeperSource(),
    )
    count_after_first = session.execute(select(func.count()).select_from(Projection)).scalar_one()

    # Second run with a fresh source: every week is already populated, so it
    # skips the network entirely and writes nothing new.
    src2 = RecordingSleeperSource()
    second = run_projection_backfill(
        session,
        league_id=LEAGUE_ID,
        start_year=SEASON_YEAR,
        end_year=SEASON_YEAR,
        source=src2,
    )

    count_after_second = session.execute(select(func.count()).select_from(Projection)).scalar_one()

    assert first.fetched == MAX_FANTASY_WEEK
    assert second.fetched == 0
    assert second.skipped == MAX_FANTASY_WEEK
    assert src2.requested == []
    assert count_after_first == count_after_second
