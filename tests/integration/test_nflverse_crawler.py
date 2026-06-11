"""Integration test for the nflverse crawler end-to-end.

Uses the committed sample parquet fixtures in ``tests/fixtures/sample_data/``
so the test never touches the network. Verifies:

* ``run_nflverse`` populates ``players`` with one row per unique gsis_id
  in the fixture
* ``player_stats_raw`` gets one row per (player, week) with source='nflverse'
* The stat JSON contains the expected engine-stat-keys
* The function is idempotent: re-running produces the same row count and
  records updates (not inserts) in the source_health bookkeeping
* A ``pipeline_runs`` row + ``source_health`` row are written

The runner module's stub-player logic is exercised because the fixture's
players parquet has the exact same gsis_ids as the stats parquet, so no
stubs are needed — but a separate test forces the stub path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nflverse.runner import run_nflverse
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    PipelineRun,
    Player,
    PlayerStatsRaw,
    SourceHealth,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ff_pipeline.crawlers.nflverse import NflverseSource

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "sample_data"


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


class _RenamingSource:
    """LocalParquetSource over the M4 fixtures whose names differ from the
    LocalParquetSource defaults (``player_stats_{year}.parquet``)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def load_player_stats(self, seasons):  # type: ignore[no-untyped-def]
        return pl.read_parquet(self._directory / f"nflverse_player_stats_{seasons[0]}_w1.parquet")

    def load_players(self):  # type: ignore[no-untyped-def]
        return pl.read_parquet(self._directory / "nflverse_players_2024.parquet")

    def load_rosters(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def load_schedules(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture
def source() -> NflverseSource:
    return _RenamingSource(FIXTURE_DIR)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_run_nflverse_populates_players_and_stats(
    session: Session,
    source: NflverseSource,
) -> None:
    result = run_nflverse(session, seasons=[2024], source=source)
    session.commit()

    player_rows = session.execute(select(Player)).all()
    stat_rows = session.execute(select(PlayerStatsRaw)).all()

    # The fixture has 25 unique gsis_ids in both files.
    assert len(player_rows) == 25
    assert len(stat_rows) == 25

    # Every stat row should be tagged source='nflverse' and is_primary=True.
    for (row,) in stat_rows:
        assert row.source == "nflverse"
        assert row.is_primary is True
        assert row.season_year == 2024
        assert row.week == 1
        # The player's own per-week NFL team is persisted (season-correct,
        # straight from nflverse's per-week ``team``).
        assert row.nfl_team is not None and row.nfl_team == row.nfl_team.upper()
        # Stats JSON has the engine keys.
        assert "passing_yards" in row.stats
        assert "rushing_yards" in row.stats

    assert result.players_added == 25
    assert result.stats_added == 25
    assert result.players_updated == 0


@pytest.mark.integration
def test_run_nflverse_writes_observability_rows(
    session: Session,
    source: NflverseSource,
) -> None:
    run_nflverse(session, seasons=[2024], source=source)
    session.commit()

    run_row = session.execute(select(PipelineRun)).scalar_one()
    health_row = session.execute(select(SourceHealth)).scalar_one()

    assert run_row.status == "success"
    assert run_row.mode == "full_sync"
    assert run_row.sources_summary is not None
    assert run_row.sources_summary["nflverse"]["stats_added"] == 25

    assert health_row.run_id == run_row.run_id
    assert health_row.source == "nflverse"
    assert health_row.status == "success"
    assert health_row.duration_ms is not None and health_row.duration_ms >= 0


@pytest.mark.integration
def test_run_nflverse_is_idempotent(
    session: Session,
    source: NflverseSource,
) -> None:
    run_nflverse(session, seasons=[2024], source=source)
    session.commit()
    second = run_nflverse(session, seasons=[2024], source=source)
    session.commit()

    assert second.players_added == 0
    assert second.players_updated == 25
    assert second.stats_added == 0
    assert second.stats_updated == 25
    # Still only 25 player rows total (no duplicates).
    assert len(session.execute(select(Player)).all()) == 25


@pytest.mark.integration
def test_run_nflverse_creates_stub_player_for_unknown_gsis_id(
    session: Session,
) -> None:
    """If load_player_stats returns a player not in load_players, the runner
    creates a stub players row using the stat row's display name."""

    stats_df = pl.DataFrame(
        {
            "player_id": ["00-MYSTERY"],
            "player_display_name": ["Mystery Man"],
            "position": ["WR"],
            "team": ["NYJ"],
            "season": [2024],
            "week": [1],
            "season_type": ["REG"],
            "opponent_team": ["BUF"],
            "passing_yards": [0],
            "rushing_yards": [12],
        }
    )
    players_df = pl.DataFrame(
        schema={
            "gsis_id": pl.String,
            "display_name": pl.String,
            "position": pl.String,
            "latest_team": pl.String,
            "birth_date": pl.String,
            "rookie_season": pl.Int32,
            "espn_id": pl.String,
            "first_name": pl.String,
            "last_name": pl.String,
            "status": pl.String,
        }
    )

    class _Src:
        def load_player_stats(self, seasons):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return stats_df

        def load_players(self):  # type: ignore[no-untyped-def]
            return players_df

        def load_rosters(self, seasons):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def load_schedules(self, seasons):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    result = run_nflverse(session, seasons=[2024], source=_Src())
    session.commit()

    player = session.execute(select(Player).where(Player.gsis_id == "00-MYSTERY")).scalar_one()
    assert player.name_full == "Mystery Man"
    assert result.players_added == 1
    assert result.stats_added == 1
