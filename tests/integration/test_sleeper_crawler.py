"""Integration test for the Sleeper crawler end-to-end.

Drives ``run_sleeper`` against ``LocalFixtureSource`` (no network) and a
fresh sqlite DB built via the alembic migrations. Verifies:

* ``players.sleeper_id`` is stamped on rows that already exist (matched
  by gsis_id from a prior nflverse run).
* ``projections`` rows are written, one per resolvable Sleeper player,
  with ``projected_points`` computed from the season's scoring rules.
* Sleeper IDs we can't resolve to internal player_ids are tracked in the
  ``unresolved_*`` counts and skipped — they do NOT create stubs.
* ``trending_players`` rows land for both ``add`` and ``drop``.
* ``pipeline_runs`` + ``source_health`` rows are written with the right
  ``status`` / ``sources_summary`` payload.
* Re-running is idempotent at the projection/trending grain when the
  ``fetched_at`` value is held constant; with a fresh ``fetched_at`` the
  rows are append-only (per the schema unique key).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.sleeper.client import LocalFixtureSource
from ff_pipeline.crawlers.sleeper.runner import run_sleeper
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    PipelineRun,
    Player,
    Projection,
    ScoringRule,
    Season,
    SourceHealth,
    TrendingPlayer,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "sleeper"
LEAGUE_ID = "TEST-LEAGUE"
SEASON_YEAR = 2024
WEEK = 1


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


@pytest.fixture
def source() -> LocalFixtureSource:
    return LocalFixtureSource(directory=FIXTURE_DIR)


def _seed_league_season_with_rules(session: Session) -> int:
    """Seed a league + season + minimal scoring rules.

    The rules below are a stripped 0.5-PPR equivalent so apply_rules
    produces a recognizable, non-zero ``projected_points`` for the
    fixture players.
    """

    session.add(League(league_id=LEAGUE_ID, name="Test League", platform="nfl_com"))
    session.flush()
    season = Season(league_id=LEAGUE_ID, year=SEASON_YEAR)
    session.add(season)
    session.flush()
    assert season.season_id is not None

    rules = [
        # Passing
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
            points_per_unit=-2.0,
            unit_size=1.0,
            threshold_min=0.0,
        ),
        # Rushing
        ScoringRule(
            season_id=season.season_id,
            category="rushing",
            stat_key="rushing_yards",
            points_per_unit=1.0,
            unit_size=10.0,
            threshold_min=0.0,
        ),
        ScoringRule(
            season_id=season.season_id,
            category="rushing",
            stat_key="rushing_tds",
            points_per_unit=6.0,
            unit_size=1.0,
            threshold_min=0.0,
        ),
        # Receiving
        ScoringRule(
            season_id=season.season_id,
            category="receiving",
            stat_key="receptions",
            points_per_unit=0.5,
            unit_size=1.0,
            threshold_min=0.0,
        ),
        ScoringRule(
            season_id=season.season_id,
            category="receiving",
            stat_key="receiving_yards",
            points_per_unit=1.0,
            unit_size=10.0,
            threshold_min=0.0,
        ),
        ScoringRule(
            season_id=season.season_id,
            category="receiving",
            stat_key="receiving_tds",
            points_per_unit=6.0,
            unit_size=1.0,
            threshold_min=0.0,
        ),
        # Misc
        ScoringRule(
            season_id=season.season_id,
            category="misc",
            stat_key="fumbles_lost",
            points_per_unit=-2.0,
            unit_size=1.0,
            threshold_min=0.0,
        ),
    ]
    session.add_all(rules)
    session.flush()
    return season.season_id


def _seed_known_players(session: Session) -> dict[str, int]:
    """Insert player rows for the gsis_ids that match our Sleeper fixture.

    Returns ``{gsis_id: player_id}`` so the tests can assert mapping.
    Mahomes' row is intentionally inserted WITHOUT a sleeper_id so we can
    assert ``players.sleeper_id`` gets stamped on first run.
    """

    rows = [
        Player(
            name_full="Patrick Mahomes",
            position="QB",
            nfl_team="KC",
            gsis_id="00-0033873",
        ),
        Player(
            name_full="Travis Kelce",
            position="TE",
            nfl_team="KC",
            gsis_id="00-0030506",
            sleeper_id="421",  # already correct — must not double-count
        ),
        Player(
            name_full="Justin Jefferson",
            position="WR",
            nfl_team="MIN",
            gsis_id="00-0036322",
        ),
    ]
    session.add_all(rows)
    session.flush()
    return {p.gsis_id: p.player_id for p in rows if p.gsis_id}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_run_sleeper_writes_projections_and_trending(
    session: Session,
    source: LocalFixtureSource,
) -> None:
    _seed_league_season_with_rules(session)
    gsis_to_pid = _seed_known_players(session)

    result = run_sleeper(
        session,
        league_id=LEAGUE_ID,
        year=SEASON_YEAR,
        week=WEEK,
        source=source,
    )
    session.commit()

    # Mahomes had no sleeper_id; Jefferson had no sleeper_id either; Kelce
    # already had the correct one — so exactly two players get stamped.
    assert result.players_with_sleeper_id_updated == 2

    # 3 of 4 fixture projections resolve (the 4th has no gsis_id and we
    # didn't seed a player row for it).
    assert result.projections_added == 3
    assert result.projections_updated == 0
    assert result.unresolved_projections == 1

    # Trending: adds=3 (Jefferson, Kelce, Phantom) + drops=2 (Mahomes,
    # Phantom). Phantom (sleeper 99999) has no internal player_id so its
    # two rows go to unresolved (one in add, one in drop).
    assert result.trending_added == 3  # Jefferson-add, Kelce-add, Mahomes-drop
    assert result.unresolved_trending == 2  # Phantom-add, Phantom-drop

    # Confirm rows actually exist in the DB.
    proj_rows = session.execute(select(Projection)).scalars().all()
    assert len(proj_rows) == 3
    mahomes_proj = next(p for p in proj_rows if p.player_id == gsis_to_pid["00-0033873"])
    # Sanity check: Mahomes' projected_points is computed via apply_rules.
    # passing_yards 285.4/25 + passing_tds 1.9*4 + passing_int 0.7*-2
    # + rushing 14.2/10 + rushing_tds 0.2*6 + fum_lost 0.1*-2.
    expected = round(
        285.4 / 25.0 + 1.9 * 4.0 + 0.7 * -2.0 + 14.2 / 10.0 + 0.2 * 6.0 + 0.1 * -2.0,
        2,
    )
    assert mahomes_proj.projected_points == pytest.approx(expected)
    assert mahomes_proj.projected_stats is not None
    assert mahomes_proj.projected_stats["passing_yards"] == pytest.approx(285.4)
    assert mahomes_proj.source == "sleeper"

    trending_rows = session.execute(select(TrendingPlayer)).scalars().all()
    assert len(trending_rows) == 3
    add_rows = [t for t in trending_rows if t.trend_type == "add"]
    drop_rows = [t for t in trending_rows if t.trend_type == "drop"]
    assert {t.player_id for t in add_rows} == {
        gsis_to_pid["00-0030506"],
        gsis_to_pid["00-0036322"],
    }
    assert {t.player_id for t in drop_rows} == {gsis_to_pid["00-0033873"]}
    assert all(t.lookback_hours == 24 for t in trending_rows)


@pytest.mark.integration
def test_run_sleeper_writes_observability_rows(
    session: Session,
    source: LocalFixtureSource,
) -> None:
    _seed_league_season_with_rules(session)
    _seed_known_players(session)

    run_sleeper(
        session,
        league_id=LEAGUE_ID,
        year=SEASON_YEAR,
        week=WEEK,
        source=source,
    )
    session.commit()

    run_row = session.execute(select(PipelineRun)).scalar_one()
    health_row = session.execute(select(SourceHealth)).scalar_one()

    assert run_row.status == "success"
    assert run_row.mode == "full_sync"
    assert run_row.sources_summary is not None
    sleeper_summary = run_row.sources_summary["sleeper"]
    assert sleeper_summary["year"] == SEASON_YEAR
    assert sleeper_summary["week"] == WEEK
    assert sleeper_summary["projections_added"] == 3
    assert sleeper_summary["scoring_rules_found"] is True

    assert health_row.run_id == run_row.run_id
    assert health_row.source == "sleeper"
    assert health_row.status == "success"
    assert health_row.duration_ms is not None and health_row.duration_ms >= 0


@pytest.mark.integration
def test_run_sleeper_without_scoring_rules_leaves_projected_points_null(
    session: Session,
    source: LocalFixtureSource,
) -> None:
    """When no scoring rules exist for the season, projections still land
    (with stats stored) but projected_points is left NULL — a downstream
    rescore can fill it in once the rules are loaded."""

    # Seed the season WITHOUT scoring rules.
    session.add(League(league_id=LEAGUE_ID, name="Test League", platform="nfl_com"))
    session.flush()
    session.add(Season(league_id=LEAGUE_ID, year=SEASON_YEAR))
    session.flush()
    _seed_known_players(session)

    result = run_sleeper(
        session,
        league_id=LEAGUE_ID,
        year=SEASON_YEAR,
        week=WEEK,
        source=source,
    )
    session.commit()

    assert result.scoring_rules_found is False
    proj_rows = session.execute(select(Projection)).scalars().all()
    assert len(proj_rows) == 3
    for row in proj_rows:
        assert row.projected_points is None
        # Stats are still preserved verbatim.
        assert row.projected_stats is not None


@pytest.mark.integration
def test_run_sleeper_does_not_stub_unknown_players(
    session: Session,
    source: LocalFixtureSource,
) -> None:
    """Phantom Rookie (sleeper 99999) has no gsis_id; we must not create a
    fresh players row for it. M7 is responsible for that."""

    _seed_league_season_with_rules(session)
    initial_pids = _seed_known_players(session)

    run_sleeper(
        session,
        league_id=LEAGUE_ID,
        year=SEASON_YEAR,
        week=WEEK,
        source=source,
    )
    session.commit()

    all_pids = {p.player_id for p in session.execute(select(Player)).scalars().all()}
    assert all_pids == set(initial_pids.values())


@pytest.mark.integration
def test_run_sleeper_is_idempotent_at_same_fetched_at(
    session: Session,
    source: LocalFixtureSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If two runs happen at the same wallclock (mocked), the second run
    updates the existing projection rows in place (matching on the
    (player, season, week, source, fetched_at) unique key)."""

    from datetime import UTC, datetime

    import ff_pipeline.crawlers.sleeper.runner as runner_mod

    _seed_league_season_with_rules(session)
    _seed_known_players(session)

    frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    class _FrozenDateTime:
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]  # noqa: ARG003
            return frozen

    monkeypatch.setattr(runner_mod, "datetime", _FrozenDateTime)

    run_sleeper(
        session,
        league_id=LEAGUE_ID,
        year=SEASON_YEAR,
        week=WEEK,
        source=source,
    )
    session.commit()
    second = run_sleeper(
        session,
        league_id=LEAGUE_ID,
        year=SEASON_YEAR,
        week=WEEK,
        source=source,
    )
    session.commit()

    # Second run finds the same rows by their natural key and updates them.
    assert second.projections_added == 0
    assert second.projections_updated == 3
    assert second.trending_added == 0
    assert second.trending_updated == 3

    # No duplicates in the DB.
    proj_count = len(session.execute(select(Projection)).scalars().all())
    assert proj_count == 3
