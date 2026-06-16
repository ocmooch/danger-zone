"""Unit tests for ``ff_pipeline.crawlers.nflverse``.

Covers:

* Stat-key projection: every engine-known stat key is produced from a
  realistic nflverse-shaped row, including the renamed and summed cases.
* Missing columns: a row with sparse columns produces zeros (not KeyError).
* Source seam: ``LocalParquetSource`` reads the committed sample fixture
  cleanly and the client yields the expected count of dataclasses.

No network calls — tests run against fixtures or in-memory Polars frames.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from ff_pipeline.crawlers.nflverse import (
    LocalParquetSource,
    NflverseClient,
)
from ff_pipeline.crawlers.nflverse.long_td_bonus import derive_long_td_bonus_counts
from ff_pipeline.crawlers.nflverse.stat_keys import (
    expected_nflverse_columns,
    project_stats,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "sample_data"


# ---------------------------------------------------------------------------
# project_stats() — the column→stat-key mapping
# ---------------------------------------------------------------------------


def test_project_stats_direct_keys_pass_through() -> None:
    row = {
        "passing_yards": 312,
        "passing_tds": 2,
        "passing_interceptions": 1,
        "passing_2pt_conversions": 0,
        "rushing_yards": 18,
        "rushing_tds": 0,
        "rushing_2pt_conversions": 0,
        "receptions": 0,
        "receiving_yards": 0,
        "receiving_tds": 0,
        "receiving_2pt_conversions": 0,
        "special_teams_tds": 0,
    }
    out = project_stats(row)
    assert out["passing_yards"] == 312
    assert out["passing_tds"] == 2
    assert out["passing_interceptions"] == 1
    assert out["rushing_yards"] == 18


def test_project_stats_renames() -> None:
    row = {
        "fumble_recovery_tds": 1,
        "pat_made": 3,
        "pat_missed": 1,
        "fg_made_0_19": 1,
        "fg_made_20_29": 0,
        "fg_made_30_39": 2,
        "fg_made_40_49": 0,
    }
    out = project_stats(row)
    assert out["fumble_return_tds"] == 1
    assert out["extra_point_made"] == 3
    assert out["extra_point_missed"] == 1
    assert out["field_goal_made_0_19"] == 1
    assert out["field_goal_made_30_39"] == 2


def test_project_stats_sums_50_plus_bracket() -> None:
    out = project_stats({"fg_made_50_59": 2, "fg_made_60_": 1})
    assert out["field_goal_made_50_plus"] == 3


def test_project_stats_sums_fumbles_lost_across_sources() -> None:
    out = project_stats(
        {
            "sack_fumbles_lost": 1,
            "rushing_fumbles_lost": 1,
            "receiving_fumbles_lost": 0,
        }
    )
    assert out["fumbles_lost"] == 2


def test_project_stats_sums_field_goal_missed() -> None:
    out = project_stats(
        {
            "fg_missed_0_19": 0,
            "fg_missed_20_29": 1,
            "fg_missed_30_39": 0,
            "fg_missed_40_49": 1,
            "fg_missed_50_59": 0,
            "fg_missed_60_": 0,
        }
    )
    assert out["field_goal_missed"] == 2


def test_project_stats_missing_columns_default_to_zero() -> None:
    # A row with NO known columns — every stat key should be present and 0.
    out = project_stats({"unknown_column": 99})
    assert "passing_yards" in out
    assert out["passing_yards"] == 0.0
    # And the unknown column does NOT leak into the output.
    assert "unknown_column" not in out


def test_project_stats_handles_none_values() -> None:
    out = project_stats({"passing_yards": None, "rushing_yards": None})
    assert out["passing_yards"] == 0.0
    assert out["rushing_yards"] == 0.0


def test_expected_columns_includes_renames_and_sums() -> None:
    cols = expected_nflverse_columns()
    assert "passing_yards" in cols
    assert "fumble_recovery_tds" in cols  # rename source
    assert "fg_made_60_" in cols  # one of the 50+ summands


def test_long_td_bonus_counts_stack_from_pbp_fixture() -> None:
    pbp = pl.read_parquet(FIXTURE_DIR / "pbp_long_td_bonus_2024.parquet")

    out = derive_long_td_bonus_counts(pbp.iter_rows(named=True))

    passer = out[
        next(
            key
            for key in out
            if key.gsis_id == "00-PASSER" and key.season_year == 2024 and key.week == 1
        )
    ]
    assert passer["passing_yards_bonus_long_td_40"] == 2.0
    assert passer["passing_yards_bonus_long_td_50"] == 1.0

    receiver_40 = out[next(key for key in out if key.gsis_id == "00-RECEIVER40")]
    assert receiver_40["receiving_yards_bonus_long_td_40"] == 1.0
    assert "receiving_yards_bonus_long_td_50" not in receiver_40

    receiver_50 = out[next(key for key in out if key.gsis_id == "00-RECEIVER50")]
    assert receiver_50["receiving_yards_bonus_long_td_40"] == 1.0
    assert receiver_50["receiving_yards_bonus_long_td_50"] == 1.0

    rusher_50 = out[next(key for key in out if key.gsis_id == "00-RUSHER50")]
    assert rusher_50["rushing_yards_bonus_long_td_40"] == 1.0
    assert rusher_50["rushing_yards_bonus_long_td_50"] == 1.0
    assert all(key.gsis_id not in {"00-RECEIVER39", "00-RUSHER12"} for key in out)


# ---------------------------------------------------------------------------
# Client + LocalParquetSource — integration of source seam + projection
# ---------------------------------------------------------------------------


class _InMemorySource:
    """Source implementation backed by a hand-built Polars frame."""

    def __init__(self, stats_df: pl.DataFrame, players_df: pl.DataFrame) -> None:
        self.stats_df = stats_df
        self.players_df = players_df

    def load_player_stats(self, seasons):  # type: ignore[no-untyped-def]  # noqa: ARG002
        return self.stats_df

    def load_players(self):  # type: ignore[no-untyped-def]
        return self.players_df

    def load_rosters(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def load_schedules(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _empty_players_df() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "gsis_id": pl.String,
            "display_name": pl.String,
            "first_name": pl.String,
            "last_name": pl.String,
            "position": pl.String,
            "latest_team": pl.String,
            "birth_date": pl.String,
            "rookie_season": pl.Int32,
            "espn_id": pl.String,
            "status": pl.String,
        }
    )


def test_client_skips_rows_with_blank_gsis_id() -> None:
    stats_df = pl.DataFrame(
        {
            "player_id": ["", "00-0023459"],
            "player_display_name": ["aggregate", "Aaron Rodgers"],
            "position": ["", "QB"],
            "team": ["", "NYJ"],
            "season": [2024, 2024],
            "week": [1, 1],
            "season_type": ["REG", "REG"],
            "opponent_team": ["", "SF"],
            "passing_yards": [0, 200],
            "passing_tds": [0, 1],
        }
    )
    client = NflverseClient(source=_InMemorySource(stats_df, _empty_players_df()))
    out = client.player_stats(seasons=[2024])
    assert len(out) == 1
    assert out[0].gsis_id == "00-0023459"
    assert out[0].nfl_team == "NYJ"
    assert out[0].stats["passing_yards"] == 200


def test_client_reads_committed_fixture() -> None:
    """End-to-end: the parquet committed by scripts/generate_nflverse_fixture.py
    loads via LocalParquetSource and projects without warnings."""

    # Rename to satisfy LocalParquetSource's expected filename pattern.
    source = _RenamingLocalSource(FIXTURE_DIR)
    client = NflverseClient(source=source)
    stats = client.player_stats(seasons=[2024])
    players = client.players()
    assert len(stats) == 25
    assert len(players) == 25
    # Every projected row has every engine stat key.
    sample = stats[0]
    assert "passing_yards" in sample.stats
    assert "fumbles_lost" in sample.stats
    assert "field_goal_made_50_plus" in sample.stats
    assert "passing_yards_bonus_long_td_40" in sample.stats


def test_client_merges_long_td_bonus_counts_from_pbp_fixture() -> None:
    stats_df = pl.DataFrame(
        {
            "player_id": ["00-PASSER", "00-RECEIVER50", "00-RUSHER50", "00-NONE"],
            "player_display_name": ["Passer", "Receiver", "Rusher", "No Bonus"],
            "position": ["QB", "WR", "RB", "TE"],
            "team": ["BUF", "BUF", "BUF", "BUF"],
            "season": [2024, 2024, 2024, 2024],
            "week": [1, 1, 1, 1],
            "season_type": ["REG", "REG", "REG", "REG"],
            "opponent_team": ["MIA", "MIA", "MIA", "MIA"],
        }
    )

    class _PbpSource(_InMemorySource):
        def load_pbp(self, seasons):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return pl.read_parquet(FIXTURE_DIR / "pbp_long_td_bonus_2024.parquet")

    out = NflverseClient(source=_PbpSource(stats_df, _empty_players_df())).player_stats(
        seasons=[2024]
    )
    by_gsis = {row.gsis_id: row for row in out}

    assert by_gsis["00-PASSER"].stats["passing_yards_bonus_long_td_40"] == 2.0
    assert by_gsis["00-PASSER"].stats["passing_yards_bonus_long_td_50"] == 1.0
    assert by_gsis["00-RECEIVER50"].stats["receiving_yards_bonus_long_td_40"] == 1.0
    assert by_gsis["00-RECEIVER50"].stats["receiving_yards_bonus_long_td_50"] == 1.0
    assert by_gsis["00-RUSHER50"].stats["rushing_yards_bonus_long_td_40"] == 1.0
    assert by_gsis["00-RUSHER50"].stats["rushing_yards_bonus_long_td_50"] == 1.0
    assert by_gsis["00-NONE"].stats["passing_yards_bonus_long_td_40"] == 0.0


class _RenamingLocalSource:
    """LocalParquetSource variant that maps our committed fixture filenames
    onto the names LocalParquetSource expects."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def load_player_stats(self, seasons):  # type: ignore[no-untyped-def]
        path = self._directory / f"nflverse_player_stats_{seasons[0]}_w1.parquet"
        return pl.read_parquet(path)

    def load_players(self):  # type: ignore[no-untyped-def]
        return pl.read_parquet(self._directory / "nflverse_players_2024.parquet")

    def load_rosters(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def load_schedules(self, seasons):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_local_parquet_source_raises_on_missing_file(tmp_path: Path) -> None:
    src = LocalParquetSource(directory=tmp_path)
    with pytest.raises(FileNotFoundError):
        src.load_player_stats(seasons=[2024])
