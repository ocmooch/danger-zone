"""Sleeper league request plan + lineage walk (S2)."""

from __future__ import annotations

from typing import Any

import pytest

from ff_pipeline.crawlers.sleeper_league.endpoints import (
    NFL_MAX_WEEK,
    Endpoint,
    draft_ids,
    league_requests,
)
from ff_pipeline.crawlers.sleeper_league.endpoints import (
    weeks_to_capture as wtc,
)
from ff_pipeline.crawlers.sleeper_league.lineage import (
    LineageError,
    league_for_season,
    walk_lineage,
)

IN_SEASON = {"season": "2026", "season_type": "regular", "week": 10}
LEAGUE_2026 = {"season": "2026", "status": "in_season"}


@pytest.mark.parametrize(
    ("state", "league", "expected"),
    [
        (IN_SEASON, LEAGUE_2026, 10),
        ({**IN_SEASON, "week": 1}, LEAGUE_2026, 1),
        ({**IN_SEASON, "week": 25}, LEAGUE_2026, NFL_MAX_WEEK),
        ({"season": "2026", "season_type": "pre", "week": 0}, LEAGUE_2026, 0),
        (IN_SEASON, {"season": "2026", "status": "pre_draft"}, 0),
        (IN_SEASON, {"season": "2026", "status": "drafting"}, 0),
        ({"season": "2026", "season_type": "post", "week": 19}, LEAGUE_2026, NFL_MAX_WEEK),
        (IN_SEASON, {"season": "2025", "status": "complete"}, NFL_MAX_WEEK),
        ({"season": "2027", "season_type": "off"}, LEAGUE_2026, NFL_MAX_WEEK),
    ],
)
def test_weeks_to_capture(state: dict[str, Any], league: dict[str, Any], expected: int) -> None:
    assert wtc(state, league) == expected


def test_league_requests_cover_every_endpoint_and_week() -> None:
    reqs = league_requests("L1", 2026, 3)
    by_ep: dict[Endpoint, list[int | None]] = {}
    for r in reqs:
        by_ep.setdefault(r.endpoint, []).append(r.week)
    assert by_ep[Endpoint.MATCHUPS] == [1, 2, 3]
    assert by_ep[Endpoint.TRANSACTIONS] == [1, 2, 3]
    assert by_ep[Endpoint.STATS_WEEK] == [1, 2, 3]
    for ep in (
        Endpoint.USERS,
        Endpoint.ROSTERS,
        Endpoint.TRADED_PICKS,
        Endpoint.WINNERS_BRACKET,
        Endpoint.LOSERS_BRACKET,
    ):
        assert by_ep[ep] == [None]
    stats = next(r for r in reqs if r.endpoint is Endpoint.STATS_WEEK)
    assert stats.url == "https://api.sleeper.com/stats/nfl/2026/1?season_type=regular"
    assert stats.league_id is None
    assert len({r.url for r in reqs}) == len(reqs)


def test_league_requests_zero_weeks() -> None:
    assert {r.endpoint for r in league_requests("L1", 2026, 0)} == {
        Endpoint.USERS,
        Endpoint.ROSTERS,
        Endpoint.TRADED_PICKS,
        Endpoint.WINNERS_BRACKET,
        Endpoint.LOSERS_BRACKET,
    }


def test_draft_ids_merge_and_dedupe() -> None:
    assert draft_ids({"draft_id": "d1"}, [{"draft_id": "d1"}, {"draft_id": "d0"}]) == ["d1", "d0"]
    assert draft_ids({}, None) == []
    assert draft_ids({"draft_id": "d9"}, "junk") == ["d9"]


# ----- lineage --------------------------------------------------------------

CHAIN = {
    "c": {"season": "2028", "previous_league_id": "b", "name": "N", "status": "in_season"},
    "b": {"season": "2027", "previous_league_id": "a", "status": "complete"},
    "a": {"season": "2026", "previous_league_id": "0", "status": "complete"},
}


def test_lineage_walks_three_seasons_newest_first() -> None:
    chain = walk_lineage(CHAIN.get, "c")
    assert [(link.league_id, link.season) for link in chain] == [
        ("c", 2028),
        ("b", 2027),
        ("a", 2026),
    ]
    assert chain[-1].previous_league_id is None
    assert league_for_season(chain, 2027).league_id == "b"
    with pytest.raises(LineageError, match="no Sleeper league for season 2025"):
        league_for_season(chain, 2025)


@pytest.mark.parametrize("prev", ["0", "", None])
def test_lineage_stops_on_empty_previous(prev: str | None) -> None:
    chain = walk_lineage({"x": {"season": 2026, "previous_league_id": prev}}.get, "x")
    assert len(chain) == 1


def test_lineage_detects_cycle_missing_and_depth() -> None:
    cyclic = {"a": {"season": 1, "previous_league_id": "b"}, "b": {"previous_league_id": "a"}}
    with pytest.raises(LineageError, match="cycle"):
        walk_lineage(cyclic.get, "a")
    with pytest.raises(LineageError, match="not found"):
        walk_lineage({"a": {"previous_league_id": "zzz"}}.get, "a")
    deep = {str(i): {"previous_league_id": str(i + 1)} for i in range(10)}
    with pytest.raises(LineageError, match="deeper than 5"):
        walk_lineage(deep.get, "0", max_depth=5)
