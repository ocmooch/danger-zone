"""Unit tests for ``ff_pipeline.crawlers.sleeper``.

Covers:

* Stat-key projection: renames + summed buckets produce the engine keys
  the runner needs; missing keys default to zero.
* ``LocalFixtureSource`` reads committed JSON fixtures by canonical
  filename and raises ``FileNotFoundError`` for absent files.
* ``SleeperClient`` parses the fixture JSON into typed dataclasses with
  correct coercion (numeric IDs → str, missing fields → None/0).
* ``LiveSleeperSource`` rate-limits between successive requests and
  serializes JSON correctly via a respx-mocked transport. No real network.

No live HTTP — the live source is tested via ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from ff_pipeline.crawlers.sleeper.client import (
    BASE_APP,
    BASE_COM,
    LiveSleeperSource,
    LocalFixtureSource,
    SleeperClientError,
)
from ff_pipeline.crawlers.sleeper.endpoints import SleeperClient
from ff_pipeline.crawlers.sleeper.stat_keys import (
    expected_sleeper_keys,
    project_stats,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "sleeper"


# ---------------------------------------------------------------------------
# project_stats() — the Sleeper → engine stat-key mapping
# ---------------------------------------------------------------------------


def test_project_stats_renames_basic_offense() -> None:
    out = project_stats(
        {
            "pass_yd": 312,
            "pass_td": 2,
            "pass_int": 1,
            "rush_yd": 18,
            "rush_td": 1,
            "rec": 6,
            "rec_yd": 72,
            "rec_td": 1,
            "fum_lost": 0,
        }
    )
    assert out["passing_yards"] == 312
    assert out["passing_tds"] == 2
    assert out["passing_interceptions"] == 1
    assert out["rushing_yards"] == 18
    assert out["receiving_yards"] == 72
    assert out["receptions"] == 6
    assert out["fumbles_lost"] == 0


def test_project_stats_sums_field_goal_50_plus_bracket() -> None:
    out = project_stats({"fgm_50_59": 2, "fgm_60p": 1})
    assert out["field_goal_made_50_plus"] == 3


def test_project_stats_handles_defense_keys() -> None:
    out = project_stats(
        {
            "def_sack": 3,
            "def_int": 1,
            "def_fr": 2,
            "def_safe": 0,
            "def_td": 1,
            "def_st_td": 1,
        }
    )
    assert out["sacks"] == 3
    assert out["interceptions"] == 1
    assert out["fumbles_recovered"] == 2
    assert out["defensive_tds"] == 1
    assert out["special_teams_tds"] == 1


def test_project_stats_unknown_keys_dropped() -> None:
    out = project_stats({"bonus_pass_yd_300": 1, "garbage": 99, "pass_yd": 10})
    assert "bonus_pass_yd_300" not in out
    assert "garbage" not in out
    assert out["passing_yards"] == 10


def test_project_stats_missing_keys_default_to_zero() -> None:
    out = project_stats({})
    # Every engine key shows up with a zero value (shape-stable JSON).
    assert out["passing_yards"] == 0.0
    assert out["rushing_yards"] == 0.0
    assert out["field_goal_made_50_plus"] == 0.0


def test_project_stats_handles_none_values() -> None:
    out = project_stats({"pass_yd": None, "rec_yd": None})
    assert out["passing_yards"] == 0.0
    assert out["receiving_yards"] == 0.0


def test_expected_sleeper_keys_includes_summands() -> None:
    keys = expected_sleeper_keys()
    assert "pass_yd" in keys
    assert "fgm_50_59" in keys
    assert "fgm_60p" in keys


# ---------------------------------------------------------------------------
# LocalFixtureSource
# ---------------------------------------------------------------------------


def test_local_fixture_source_reads_players() -> None:
    src = LocalFixtureSource(directory=FIXTURE_DIR)
    payload = src.players()
    assert isinstance(payload, dict)
    assert "4034" in payload
    assert payload["4034"]["full_name"] == "Patrick Mahomes"


def test_local_fixture_source_reads_projections() -> None:
    src = LocalFixtureSource(directory=FIXTURE_DIR)
    payload = src.projections(2024, 1)
    assert isinstance(payload, list)
    assert payload[0]["player_id"] == "4034"
    assert payload[0]["stats"]["pass_yd"] == 285.4


def test_local_fixture_source_reads_trending_add_and_drop() -> None:
    src = LocalFixtureSource(directory=FIXTURE_DIR)
    adds = src.trending("add")
    drops = src.trending("drop")
    assert adds[0]["player_id"] == "7553"
    assert drops[0]["player_id"] == "4034"


def test_local_fixture_source_raises_on_missing_file(tmp_path: Path) -> None:
    src = LocalFixtureSource(directory=tmp_path)
    with pytest.raises(FileNotFoundError):
        src.players()


# ---------------------------------------------------------------------------
# SleeperClient — typed parsing on top of a fixture source
# ---------------------------------------------------------------------------


def test_sleeper_client_players_coerces_ids_to_strings() -> None:
    client = SleeperClient(source=LocalFixtureSource(directory=FIXTURE_DIR))
    players = client.players()
    by_id = {p.sleeper_id: p for p in players}
    mahomes = by_id["4034"]
    assert mahomes.gsis_id == "00-0033873"
    # ESPN/Yahoo IDs were ints in the fixture; client coerces to string.
    assert mahomes.espn_id == "3139477"
    assert mahomes.yahoo_id == "30123"
    assert mahomes.position == "QB"
    assert mahomes.nfl_team == "KC"
    assert mahomes.is_active is True


def test_sleeper_client_players_handles_inactive_with_no_gsis() -> None:
    client = SleeperClient(source=LocalFixtureSource(directory=FIXTURE_DIR))
    players = client.players()
    phantom = next(p for p in players if p.sleeper_id == "99999")
    assert phantom.gsis_id is None
    assert phantom.espn_id is None
    assert phantom.is_active is False


def test_sleeper_client_projections_apply_stat_projection() -> None:
    client = SleeperClient(source=LocalFixtureSource(directory=FIXTURE_DIR))
    projections = client.projections(2024, 1)
    by_id = {p.sleeper_id: p for p in projections}
    mahomes = by_id["4034"]
    # Projected stats are mapped onto the engine vocabulary.
    assert mahomes.stats["passing_yards"] == pytest.approx(285.4)
    assert mahomes.stats["passing_tds"] == pytest.approx(1.9)
    assert mahomes.stats["rushing_yards"] == pytest.approx(14.2)
    # Engine-known keys absent from the projection still show up as 0.
    assert mahomes.stats["sacks"] == 0.0
    assert mahomes.season_year == 2024
    assert mahomes.week == 1
    assert mahomes.season_type == "regular"


def test_sleeper_client_skips_projection_with_non_dict_stats() -> None:
    class _Src:
        def players(self):  # type: ignore[no-untyped-def]
            return {}

        def projections(self, year, week, *, season_type="regular"):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return [{"player_id": "1", "stats": "oops"}]

        def trending(self, kind, *, lookback_hours=24, limit=25):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return []

    client = SleeperClient(source=_Src())
    out = client.projections(2024, 1)
    assert out == []


def test_sleeper_client_trending() -> None:
    client = SleeperClient(source=LocalFixtureSource(directory=FIXTURE_DIR))
    adds = client.trending("add")
    drops = client.trending("drop")
    assert adds[0].sleeper_id == "7553"
    assert adds[0].count == 18234
    assert drops[0].sleeper_id == "4034"


# ---------------------------------------------------------------------------
# LiveSleeperSource — verified via httpx.MockTransport
# ---------------------------------------------------------------------------


def _mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_live_source_hits_correct_urls_and_decodes_json() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url).startswith(f"{BASE_APP}/players/nfl/trending/"):
            return httpx.Response(200, json=[{"player_id": "1", "count": 5}])
        if str(request.url).startswith(f"{BASE_APP}/players/nfl"):
            return httpx.Response(200, json={"1": {"player_id": "1"}})
        if str(request.url).startswith(f"{BASE_COM}/projections/nfl"):
            return httpx.Response(200, json=[{"player_id": "1", "stats": {}}])
        return httpx.Response(404)

    src = LiveSleeperSource(requests_per_min=1000, transport=_mock_transport(handler))
    try:
        players = src.players()
        projections = src.projections(2025, 1)
        trending = src.trending("add")
    finally:
        src.close()

    assert players == {"1": {"player_id": "1"}}
    assert projections == [{"player_id": "1", "stats": {}}]
    assert trending == [{"player_id": "1", "count": 5}]
    assert any("trending/add" in u for u in seen)
    assert any("/projections/nfl/2025/1" in u for u in seen)


def test_live_source_raises_on_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(404, json={"error": "no"})

    src = LiveSleeperSource(requests_per_min=1000, transport=_mock_transport(handler))
    try:
        with pytest.raises(SleeperClientError):
            src.players()
    finally:
        src.close()


def test_live_source_rejects_unknown_trending_kind() -> None:
    src = LiveSleeperSource(requests_per_min=1000)
    try:
        with pytest.raises(SleeperClientError):
            src.trending("nope")
    finally:
        src.close()


def test_live_source_throttles_between_requests() -> None:
    """At 120 req/min the min interval is 0.5s — confirm it sleeps."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={})

    # 600 req/min → 100ms min interval; cheap to assert against.
    src = LiveSleeperSource(requests_per_min=600, transport=_mock_transport(handler))
    try:
        start = time.monotonic()
        src.players()
        src.players()
        elapsed = time.monotonic() - start
    finally:
        src.close()
    assert elapsed >= 0.1


def test_live_source_raises_on_unexpected_json_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json=["a", "list", "not a dict"])

    src = LiveSleeperSource(requests_per_min=1000, transport=_mock_transport(handler))
    try:
        # players() expects a dict; getting a list should error.
        with pytest.raises(SleeperClientError):
            src.players()
    finally:
        src.close()


def test_live_source_decodes_non_json_body_with_clean_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200, content=b"<html>oops</html>", headers={"content-type": "text/html"}
        )

    src = LiveSleeperSource(requests_per_min=1000, transport=_mock_transport(handler))
    try:
        with pytest.raises(SleeperClientError):
            src.players()
    finally:
        src.close()


def test_live_source_5xx_retries_then_raises_transient() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        attempts["n"] += 1
        return httpx.Response(503)

    src = LiveSleeperSource(requests_per_min=1000, transport=_mock_transport(handler))
    try:
        with pytest.raises(SleeperClientError):
            src.players()
    finally:
        src.close()
    # tenacity is configured with stop_after_attempt(3).
    assert attempts["n"] == 3


# ---------------------------------------------------------------------------
# Roundtrip: fixtures parse identically to the wire-format we'd see
# ---------------------------------------------------------------------------


def test_fixture_files_are_well_formed_json() -> None:
    for name in (
        "players_nfl.json",
        "projections_2024_w1.json",
        "trending_add.json",
        "trending_drop.json",
    ):
        with (FIXTURE_DIR / name).open(encoding="utf-8") as fh:
            json.load(fh)
