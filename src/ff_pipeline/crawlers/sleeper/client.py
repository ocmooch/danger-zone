"""HTTP layer for the Sleeper API.

Sleeper exposes two public hosts:

* ``api.sleeper.app`` — players directory, trending adds/drops.
* ``api.sleeper.com`` — projections and weekly stats.

Both are unauthenticated public read APIs; the only ToS-ish requirement is
to stay under **1000 requests / minute**. The pipeline's per-run footprint
is tiny (~5 calls) so this is a courtesy throttle, not a real constraint.

This module exposes the same test seam pattern as the nflverse client:

* ``SleeperSource`` — minimal protocol describing the three calls we need.
* ``LiveSleeperSource`` — production implementation backed by ``httpx``.
* ``LocalFixtureSource`` — reads pre-saved JSON files from a fixture dir
  so the test suite never touches the network.

The rate limit is enforced at the ``LiveSleeperSource`` layer by a simple
"sleep until last_request_t + min_interval" check — the same shape as
``NflComClient``. A more elaborate token bucket isn't worth its weight for
our request volume.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

# Path is used as a runtime annotation on a dataclass field; keep eager.
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from types import TracebackType

log = get_logger(__name__)

BASE_APP = "https://api.sleeper.app/v1"
BASE_COM = "https://api.sleeper.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = "ff-pipeline/0.1 (+personal use)"

# Sleeper's documented hard ceiling is 1000 req/min. Settings default to
# 120 — we lean conservative since our runs aren't latency sensitive.
_MIN_REQUESTS_PER_MIN = 1
_MAX_REQUESTS_PER_MIN = 1000


class SleeperClientError(RuntimeError):
    """Base class for Sleeper HTTP failures."""


class TransientHTTPError(SleeperClientError):
    """A 5xx or network error that survived the retry budget."""


class SleeperSource(Protocol):
    """Test seam between the HTTP layer and the typed endpoint wrappers.

    Each method returns the raw decoded JSON for one Sleeper endpoint;
    ``SleeperClient`` is responsible for parsing into dataclasses.
    """

    def players(self) -> dict[str, dict[str, Any]]: ...
    def projections(
        self, year: int, week: int, *, season_type: str = "regular"
    ) -> list[dict[str, Any]]: ...
    def trending(
        self, kind: str, *, lookback_hours: int = 24, limit: int = 25
    ) -> list[dict[str, Any]]: ...


class LiveSleeperSource:
    """Production source — httpx + polite throttle.

    Not threadsafe (one ``httpx.Client``, one ``_last_request_t`` slot).
    Sleeper runs are single-threaded and tiny (~5 calls) so this is fine.
    """

    def __init__(
        self,
        *,
        requests_per_min: int = 120,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        rpm = max(_MIN_REQUESTS_PER_MIN, min(_MAX_REQUESTS_PER_MIN, int(requests_per_min)))
        self._min_interval = 60.0 / rpm
        self._last_request_t = 0.0
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
            timeout=timeout_seconds,
            transport=transport,
        )

    # ----- context manager -----

    def __enter__(self) -> LiveSleeperSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ----- SleeperSource protocol -----

    def players(self) -> dict[str, dict[str, Any]]:
        url = f"{BASE_APP}/players/nfl"
        return cast("dict[str, dict[str, Any]]", self._get_json(url, expected_type=dict))

    def projections(
        self, year: int, week: int, *, season_type: str = "regular"
    ) -> list[dict[str, Any]]:
        url = f"{BASE_COM}/projections/nfl/{year}/{week}?season_type={season_type}"
        return cast("list[dict[str, Any]]", self._get_json(url, expected_type=list))

    def trending(
        self, kind: str, *, lookback_hours: int = 24, limit: int = 25
    ) -> list[dict[str, Any]]:
        if kind not in {"add", "drop"}:
            raise SleeperClientError(f"trending kind must be 'add' or 'drop'; got {kind!r}")
        url = (
            f"{BASE_APP}/players/nfl/trending/{kind}?lookback_hours={lookback_hours}&limit={limit}"
        )
        return cast("list[dict[str, Any]]", self._get_json(url, expected_type=list))

    # ----- internals -----

    def _get_json(self, url: str, *, expected_type: type) -> Any:
        self._sleep_for_rate_limit()
        try:
            response = self._fetch(url)
        except RetryError as exc:
            raise TransientHTTPError(f"GET {url} failed after retries: {exc}") from exc

        if response.status_code >= 400:
            raise SleeperClientError(f"GET {url} returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise SleeperClientError(f"GET {url} returned non-JSON body") from exc

        if not isinstance(payload, expected_type):
            raise SleeperClientError(
                f"GET {url} returned unexpected JSON type "
                f"({type(payload).__name__}, expected {expected_type.__name__})"
            )
        return payload

    def _sleep_for_rate_limit(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_t = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, TransientHTTPError)),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        stop=stop_after_attempt(3),
        reraise=False,
    )
    def _fetch(self, url: str) -> httpx.Response:
        log.debug("GET", url=url)
        response = self._client.get(url)
        if 500 <= response.status_code < 600:
            raise TransientHTTPError(f"Sleeper returned HTTP {response.status_code} for {url}")
        return response


@dataclass(frozen=True, slots=True)
class LocalFixtureSource:
    """Reads pre-saved JSON fixtures from a directory.

    Filenames (one per endpoint variant):

    * ``players_nfl.json``                — ``/v1/players/nfl``
    * ``projections_{year}_w{week}.json`` — ``/projections/nfl/{year}/{week}``
    * ``trending_{kind}.json``            — ``/v1/players/nfl/trending/{kind}``

    Missing files raise ``FileNotFoundError`` so tests fail loudly when a
    fixture is absent (matches ``LocalParquetSource`` semantics).
    """

    directory: Path

    def players(self) -> dict[str, dict[str, Any]]:
        return cast("dict[str, dict[str, Any]]", self._read("players_nfl.json"))

    def projections(
        self, year: int, week: int, *, season_type: str = "regular"
    ) -> list[dict[str, Any]]:
        _ = season_type  # fixture filename does not encode season_type
        return cast("list[dict[str, Any]]", self._read(f"projections_{year}_w{week}.json"))

    def trending(
        self, kind: str, *, lookback_hours: int = 24, limit: int = 25
    ) -> list[dict[str, Any]]:
        _ = (lookback_hours, limit)
        return cast("list[dict[str, Any]]", self._read(f"trending_{kind}.json"))

    def _read(self, filename: str) -> Any:
        path = self.directory / filename
        if not path.exists():
            raise FileNotFoundError(f"sleeper fixture missing: {path}")
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)


__all__ = [
    "BASE_APP",
    "BASE_COM",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_USER_AGENT",
    "LiveSleeperSource",
    "LocalFixtureSource",
    "SleeperClientError",
    "SleeperSource",
    "TransientHTTPError",
]
