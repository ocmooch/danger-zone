"""Unit tests for Sleeper ADP parsing from the projections payload."""

from __future__ import annotations

from typing import Any

from ff_pipeline.crawlers.adp.format_map import FULL_PPR, HALF_PPR, STANDARD
from ff_pipeline.crawlers.adp.sleeper import LiveSleeperAdpSource


class FakeSleeperAdpHttp:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.years: list[int] = []

    def projections(self, *, year: int) -> list[dict[str, Any]]:
        self.years.append(year)
        return self.rows


def test_sleeper_adp_source_maps_format_specific_adp_keys() -> None:
    http = FakeSleeperAdpHttp(
        [
            {
                "player_id": "6794",
                "team": "MIN",
                "player": {
                    "first_name": "Justin",
                    "last_name": "Jefferson",
                    "position": "WR",
                    "team": "MIN",
                },
                "stats": {"adp_ppr": 5.5, "adp_half_ppr": 5.0, "adp_std": 5.0},
            },
            {
                "player_id": "bad",
                "player": {"first_name": "No", "last_name": "Market", "position": "RB"},
                "stats": {"pts_ppr": 100.0},
            },
        ]
    )
    source = LiveSleeperAdpSource(http=http)

    ppr = source.fetch(year=2025, fmt=FULL_PPR, teams=12)
    half = source.fetch(year=2025, fmt=HALF_PPR, teams=12)
    standard = source.fetch(year=2025, fmt=STANDARD, teams=12)

    assert [e.source_player_key for e in ppr] == ["6794"]
    assert ppr[0].source == "sleeper"
    assert ppr[0].name == "Justin Jefferson"
    assert ppr[0].position == "WR"
    assert ppr[0].nfl_team == "MIN"
    assert ppr[0].adp == 5.5
    assert half[0].adp == 5.0
    assert standard[0].adp == 5.0
    assert http.years == [2025, 2025, 2025]


def test_sleeper_adp_source_returns_no_data_when_year_has_no_adp_keys() -> None:
    source = LiveSleeperAdpSource(
        http=FakeSleeperAdpHttp(
            [
                {
                    "player_id": "4034",
                    "player": {"first_name": "Christian", "last_name": "McCaffrey"},
                    "stats": {"pts_ppr": 250.0},
                }
            ]
        )
    )

    assert source.fetch(year=2018, fmt=FULL_PPR, teams=12) == []
