"""Unit tests for the M9 rescore module.

Seeds a tiny in-memory DB with one league + season + scoring rules +
raw stats, then asserts:

* The rescore writes one ``player_stats_scored`` row per raw row
* Re-running is idempotent (counts as updates, not inserts)
* ``dry_run=True`` surfaces diffs without writing
* Seasons with no scoring rules are flagged
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
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    ScoringRule,
    Season,
)
from ff_pipeline.scoring.rescore import rescore_seasons


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'rescore.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed(session: Session) -> tuple[Season, Player]:
    league = League(league_id="36271", name="The Danger Zone", platform="nfl_com")
    session.add(league)
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    player = Player(name_full="Test QB", position="QB", gsis_id="00-0000001")
    session.add(player)
    session.flush()
    # Standard NFL.com QB rules: 1pt / 25 passing yds + 4pt / TD + -1 / INT.
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
            ScoringRule(
                season_id=season.season_id,
                category="passing",
                stat_key="passing_interceptions",
                points_per_unit=-1.0,
                unit_size=1.0,
                threshold_min=0.0,
            ),
        ]
    )
    # One raw row: 250 yds + 2 TDs + 1 INT → 10 + 8 - 1 = 17.0
    session.add(
        PlayerStatsRaw(
            player_id=player.player_id,
            season_year=2024,
            week=1,
            season_type="REG",
            source="nflverse",
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
            stats={
                "passing_yards": 250.0,
                "passing_tds": 2.0,
                "passing_interceptions": 1.0,
            },
        )
    )
    session.commit()
    return season, player


def test_rescore_writes_one_row_per_raw_row(session: Session) -> None:
    season, player = _seed(session)
    result = rescore_seasons(session, league_id="36271")
    session.commit()
    assert result.rows_scored == 1
    assert result.rows_added == 1
    assert result.rows_updated == 0
    scored = session.query(PlayerStatsScored).filter_by(player_id=player.player_id).one()
    assert scored.total_points == pytest.approx(17.0)
    assert scored.season_id == season.season_id


def test_rescore_is_idempotent(session: Session) -> None:
    _seed(session)
    rescore_seasons(session, league_id="36271")
    session.commit()
    result = rescore_seasons(session, league_id="36271")
    session.commit()
    # Second pass: row already matches, so 0 are added and the engine
    # produces the same total → unchanged count is 1.
    assert result.rows_added == 0
    assert result.rows_scored == 1
    assert result.rows_unchanged == 1
    assert len(result.diffs) == 0


def test_rescore_dry_run_doesnt_write(session: Session) -> None:
    _seed(session)
    result = rescore_seasons(session, league_id="36271", dry_run=True)
    assert result.rows_added == 0
    assert result.rows_updated == 0
    # The diff captures the brand-new row (previous_total=None).
    assert len(result.diffs) == 1
    diff = result.diffs[0]
    assert diff.previous_total is None
    assert diff.new_total == pytest.approx(17.0)
    # No row materialized.
    assert session.query(PlayerStatsScored).count() == 0


def test_rescore_detects_changed_total_via_diff(session: Session) -> None:
    _seed(session)
    # First-pass write.
    rescore_seasons(session, league_id="36271")
    session.commit()
    # Bump passing_tds: 250 yds + 3 TDs + 1 INT → 10 + 12 - 1 = 21.0
    raw = session.query(PlayerStatsRaw).one()
    new_stats = dict(raw.stats or {})
    new_stats["passing_tds"] = 3.0
    raw.stats = new_stats
    session.commit()
    result = rescore_seasons(session, league_id="36271", dry_run=True)
    assert len(result.diffs) == 1
    assert result.diffs[0].previous_total == pytest.approx(17.0)
    assert result.diffs[0].new_total == pytest.approx(21.0)


def test_rescore_warns_on_season_without_rules(session: Session) -> None:
    league = League(league_id="36271", name="The Danger Zone", platform="nfl_com")
    session.add(league)
    session.add(Season(league_id="36271", year=2024, status="completed"))
    session.commit()
    result = rescore_seasons(session, league_id="36271")
    assert result.rows_scored == 0
    assert result.missing_rules_seasons == (2024,)


__all__ = [
    "test_rescore_detects_changed_total_via_diff",
    "test_rescore_dry_run_doesnt_write",
    "test_rescore_is_idempotent",
    "test_rescore_warns_on_season_without_rules",
    "test_rescore_writes_one_row_per_raw_row",
]
