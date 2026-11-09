"""Sleeper league sources: live (mocked transport), fixture, Retry-After (S2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from ff_pipeline.crawlers.sleeper_league.client import (
    MAX_RETRY_AFTER_SECONDS,
    FixtureSleeperLeagueSource,
    LiveSleeperLeagueSource,
    TransientHTTPError,
    retry_after_seconds,
)
from ff_pipeline.crawlers.sleeper_league.endpoints import (
    Endpoint,
    PayloadRequest,
    league_request,
    state_request,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _source(
    handler: httpx.MockTransport, sleeps: list[float], *, attempts: int = 4
) -> LiveSleeperLeagueSource:
    return LiveSleeperLeagueSource(
        requests_per_min=1000,
        transport=handler,
        sleep=sleeps.append,
        clock=lambda: NOW,
        attempts=attempts,
    )


def test_live_returns_raw_bytes_untouched() -> None:
    raw = b'{"week": 10,  "season": "2026"}'  # odd spacing must survive
    sleeps: list[float] = []
    with _source(httpx.MockTransport(lambda _r: httpx.Response(200, content=raw)), sleeps) as src:
        got = src.get(state_request())
    assert got.body == raw
    assert got.http_status == 200
    assert got.fetched_at == NOW
    assert got.json() == {"week": 10, "season": "2026"}


def test_429_honors_retry_after_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=b"{}")

    sleeps: list[float] = []
    with _source(httpx.MockTransport(handler), sleeps) as src:
        got = src.get(state_request())
    assert got.http_status == 200
    assert calls["n"] == 2
    assert 7.0 in sleeps


def test_5xx_retries_then_raises() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    sleeps: list[float] = []
    with (
        _source(httpx.MockTransport(handler), sleeps, attempts=3) as src,
        pytest.raises(TransientHTTPError, match="failed after retries"),
    ):
        src.get(state_request())
    assert calls["n"] == 3


def test_404_is_returned_with_status_not_raised() -> None:
    sleeps: list[float] = []
    transport = httpx.MockTransport(lambda _r: httpx.Response(404, content=b"not found"))
    with _source(transport, sleeps) as src:
        got = src.get(league_request("123"))
    assert got.http_status == 404
    assert got.body == b"not found"
    assert got.json() is None


def test_throttle_spaces_requests() -> None:
    sleeps: list[float] = []
    src = LiveSleeperLeagueSource(
        requests_per_min=1,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"{}")),
        sleep=sleeps.append,
    )
    with src:
        src.get(state_request())
        src.get(state_request())
    assert len(sleeps) == 1
    assert 59.0 < sleeps[0] <= 60.0


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("3", 3.0),
        ("-5", 0.0),
        ("99999", MAX_RETRY_AFTER_SECONDS),
        ("Mon, 09 Nov 2026 12:00:30 GMT", 30.0),
        ("garbage", None),
    ],
)
def test_retry_after_parsing(header: str | None, expected: float | None) -> None:
    assert retry_after_seconds(header, now=NOW) == expected


def test_fixture_source_reads_by_fixture_name(tmp_path: Path) -> None:
    req = PayloadRequest(
        Endpoint.MATCHUPS, "https://api.sleeper.app/v1/league/77/matchups/3", "77", 2026, 3
    )
    assert req.fixture_name == "v1_league_77_matchups_3.json"
    (tmp_path / req.fixture_name).write_bytes(b"[]")
    src = FixtureSleeperLeagueSource(tmp_path)
    assert src.get(req).body == b"[]"
    with pytest.raises(FileNotFoundError):
        src.get(league_request("78"))
