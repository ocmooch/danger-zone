"""Sources for Sleeper league payloads (S2).

``SleeperLeagueSource`` is the test seam. Every source returns the **raw
bytes** of a response, never re-serialized JSON, so the archive hash is the
hash of exactly what Sleeper sent.

* ``LiveSleeperLeagueSource``: httpx, the same polite throttle and tenacity
  retry shape as ``crawlers.sleeper.client``, plus ``429 Retry-After``.
* ``FixtureSleeperLeagueSource``: files named by ``PayloadRequest.fixture_name``.
* ``ArchiveSleeperLeagueSource``: replays the archived revision (newest, or
  as of a moment) from ``source_payloads`` + the raw store. No network.

HTTP 4xx responses are returned (with their status), not raised: Sleeper
answers some not-yet-meaningful requests (e.g. brackets before the playoffs)
with an error or ``null``, and the archive records that too. 5xx and
network errors are retried and then raised.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ff_pipeline.archive.raw_store import newest_payload
from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import TracebackType

    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.endpoints import PayloadRequest

log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = "ff-pipeline/0.1 (+personal use)"
MAX_RETRY_AFTER_SECONDS = 120.0
_MIN_REQUESTS_PER_MIN = 1
_MAX_REQUESTS_PER_MIN = 1000


class SleeperLeagueError(RuntimeError):
    """Base class for league capture failures."""


class TransientHTTPError(SleeperLeagueError):
    """A 5xx, 429 or network error (retried; raised once retries run out)."""


class ArchiveMissError(SleeperLeagueError):
    """The archive holds no payload for this request."""


@dataclass(frozen=True, slots=True)
class FetchedPayload:
    request: PayloadRequest
    body: bytes
    http_status: int
    fetched_at: datetime

    @property
    def ok(self) -> bool:
        return 200 <= self.http_status < 300

    def json(self) -> Any:
        """Decoded body, or None for a non-2xx / non-JSON response."""
        if not self.ok:
            return None
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


class SleeperLeagueSource(Protocol):
    def get(self, request: PayloadRequest) -> FetchedPayload: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP date), capped."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or _utcnow())).total_seconds()
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


class LiveSleeperLeagueSource:
    """Production source: httpx + throttle + retry. Not threadsafe."""

    def __init__(
        self,
        *,
        requests_per_min: int = 120,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
        attempts: int = 4,
    ) -> None:
        rpm = max(_MIN_REQUESTS_PER_MIN, min(_MAX_REQUESTS_PER_MIN, int(requests_per_min)))
        self._min_interval = 60.0 / rpm
        self._last_request_t: float | None = None
        self._sleep = sleep
        self._clock = clock
        self._attempts = attempts
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> LiveSleeperLeagueSource:
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

    def get(self, request: PayloadRequest) -> FetchedPayload:
        retrying = Retrying(
            retry=retry_if_exception_type((httpx.TransportError, TransientHTTPError)),
            wait=wait_exponential(multiplier=2, min=2, max=16),
            stop=stop_after_attempt(self._attempts),
            sleep=self._sleep,
            reraise=False,
        )
        try:
            response = retrying(self._fetch, request.url)
        except RetryError as exc:
            raise TransientHTTPError(f"GET {request.url} failed after retries: {exc}") from exc
        return FetchedPayload(
            request=request,
            body=response.content,
            http_status=response.status_code,
            fetched_at=self._clock(),
        )

    def _throttle(self) -> None:
        now = time.monotonic()
        if self._last_request_t is not None:
            wait = self._min_interval - (now - self._last_request_t)
            if wait > 0:
                self._sleep(wait)
        self._last_request_t = time.monotonic()

    def _fetch(self, url: str) -> httpx.Response:
        self._throttle()
        log.debug("GET", url=url)
        response = self._client.get(url)
        if response.status_code == 429:
            delay = retry_after_seconds(response.headers.get("Retry-After"), now=self._clock())
            if delay:
                log.warning("sleeper 429; honoring Retry-After", url=url, seconds=delay)
                self._sleep(delay)
            raise TransientHTTPError(f"Sleeper returned HTTP 429 for {url}")
        if 500 <= response.status_code < 600:
            raise TransientHTTPError(f"Sleeper returned HTTP {response.status_code} for {url}")
        return response


@dataclass(frozen=True, slots=True)
class FixtureSleeperLeagueSource:
    """Reads ``<directory>/<request.fixture_name>``; a missing file raises."""

    directory: Path
    fetched_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def get(self, request: PayloadRequest) -> FetchedPayload:
        path = self.directory / request.fixture_name
        if not path.exists():
            raise FileNotFoundError(f"missing Sleeper league fixture: {path}")
        return FetchedPayload(request, path.read_bytes(), 200, self.fetched_at)


@dataclass(frozen=True, slots=True)
class ArchiveSleeperLeagueSource:
    """Replays archived payloads: the newest revision, or the one as of ``as_of``."""

    session: Session
    store: RawStore
    as_of: datetime | None = None

    def get(self, request: PayloadRequest) -> FetchedPayload:
        row = newest_payload(self.session, request.url, as_of=self.as_of)
        if row is None:
            raise ArchiveMissError(f"no archived payload for {request.url}")
        return FetchedPayload(
            request=request,
            body=self.store.get(row.sha256),
            http_status=row.http_status,
            fetched_at=row.last_fetched_at,
        )


__all__ = [
    "ArchiveMissError",
    "ArchiveSleeperLeagueSource",
    "FetchedPayload",
    "FixtureSleeperLeagueSource",
    "LiveSleeperLeagueSource",
    "SleeperLeagueError",
    "SleeperLeagueSource",
    "TransientHTTPError",
    "retry_after_seconds",
]
