"""One-off: build a tiny nflverse parquet pair for the M4 integration test.

Calls live nflreadpy once, slices ~25 players (a mix of QB/RB/WR/TE/K + a few
non-fantasy positions to exercise the position filter), and writes both:

* ``tests/fixtures/sample_data/nflverse_player_stats_2024_w1.parquet``
* ``tests/fixtures/sample_data/nflverse_players_2024.parquet``

The fixtures are committed to git so the integration test never touches the
network. Re-run this script if nflverse changes its column layout (rare).

Usage::

    uv run python scripts/generate_nflverse_fixture.py
"""

from __future__ import annotations

from pathlib import Path

import nflreadpy as nfl
import polars as pl

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample_data"
SEASON = 2024
WEEK = 1


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    stats = nfl.load_player_stats(seasons=[SEASON])
    week_stats = stats.filter((pl.col("season") == SEASON) & (pl.col("week") == WEEK))

    # Take a handful per position so every category in the scoring engine has at
    # least one player exercising it.
    sampled = (
        week_stats.filter(pl.col("position").is_in(["QB", "RB", "WR", "TE", "K"]))
        .group_by("position")
        .head(5)
        .sort(["position", "player_id"])
    )
    sampled_ids = sampled["player_id"].to_list()

    players = nfl.load_players()
    players_subset = players.filter(pl.col("gsis_id").is_in(sampled_ids))

    stats_out = FIXTURE_DIR / f"nflverse_player_stats_{SEASON}_w{WEEK}.parquet"
    players_out = FIXTURE_DIR / f"nflverse_players_{SEASON}.parquet"

    sampled.write_parquet(stats_out)
    players_subset.write_parquet(players_out)

    print(f"wrote {stats_out} ({sampled.height} rows)")
    print(f"wrote {players_out} ({players_subset.height} rows)")
    print("positions:", sampled["position"].value_counts().to_dict(as_series=False))


if __name__ == "__main__":
    main()
