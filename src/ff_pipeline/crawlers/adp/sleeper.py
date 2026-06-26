"""Sleeper ADP source.

Sleeper does not document a standalone ADP endpoint in its public API docs. The
season-level projections payload on ``api.sleeper.com`` does carry redraft ADP
fields, one row per player:

    GET https://api.sleeper.com/projections/nfl/{year}?season_type=regular

``stats.adp_ppr`` / ``adp_half_ppr`` / ``adp_std`` map to this pipeline's
``full_ppr`` / ``half_ppr`` / ``standard`` formats. Historical availability is
partial (older seasons can return projection rows but no ADP keys), so a year
with no usable ADP returns ``[]`` and the runner records ``no_data``.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ff_pipeline.crawlers.adp.endpoints import AdpEntry
from ff_pipeline.crawlers.adp.format_map import FULL_PPR, HALF_PPR, STANDARD
from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from types import TracebackType

log = get_logger(__name__)

SOURCE_NAME = "sleeper"
BASE_URL = "https://api.sleeper.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = "ff-pipeline/0.1 (+personal use; ADP via sleeper.com projections)"

_FORMAT_STAT_KEY: dict[str, str] = {
    FULL_PPR: "adp_ppr",
    HALF_PPR: "adp_half_ppr",
    STANDARD: "adp_std",
}


class SleeperAdpClientError(RuntimeError):
    """Base class for Sleeper ADP HTTP failures."""


class SleeperAdpTransientError(SleeperAdpClientError):
    """A 5xx or network error that survived the retry budget."""


class SleeperAdpHttp(Protocol):
    """Test seam: raw decoded season projections payload."""

    def projections(self, *, year: int) -> list[dict[str, Any]]: ...


class LiveSleeperAdpSource:
    """Production Sleeper ADP source, parsed into :class:`AdpEntry` rows."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        requests_per_min: int = 120,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        http: SleeperAdpHttp | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        rpm = max(1, min(1000, int(requests_per_min)))
        self._min_interval = 60.0 / rpm
        self._last_request_t = 0.0
        self._http = http
        self._client = (
            httpx.Client(
                headers={"User-Agent": user_agent, "Accept": "application/json"},
                timeout=timeout_seconds,
                transport=transport,
            )
            if http is None
            else None
        )

    def __enter__(self) -> LiveSleeperAdpSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ----- AdpSource -----

    def fetch(self, *, year: int, fmt: str, teams: int) -> list[AdpEntry]:
        _ = teams  # Sleeper's projection ADP payload is not team-count segmented.
        stat_key = _FORMAT_STAT_KEY.get(fmt)
        if stat_key is None:
            return []
        return _parse(self._projections(year=year), stat_key=stat_key)

    # ----- internals -----

    def _projections(self, *, year: int) -> list[dict[str, Any]]:
        if self._http is not None:
            return self._http.projections(year=year)
        url = f"{BASE_URL}/projections/nfl/{year}?season_type=regular"
        return self._get_json(url)

    def _get_json(self, url: str) -> list[dict[str, Any]]:
        self._sleep_for_rate_limit()
        try:
            response = self._get(url)
        except RetryError as exc:
            raise SleeperAdpTransientError(f"GET {url} failed after retries: {exc}") from exc
        if response.status_code >= 400:
            raise SleeperAdpClientError(f"GET {url} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise SleeperAdpClientError(f"GET {url} returned non-JSON body") from exc
        if not isinstance(payload, list):
            raise SleeperAdpClientError(f"GET {url} returned unexpected JSON type")
        return cast("list[dict[str, Any]]", [row for row in payload if isinstance(row, dict)])

    def _sleep_for_rate_limit(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_t = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, SleeperAdpTransientError)),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        stop=stop_after_attempt(3),
        reraise=False,
    )
    def _get(self, url: str) -> httpx.Response:
        assert self._client is not None
        log.debug("GET", url=url)
        response = self._client.get(url)
        if 500 <= response.status_code < 600:
            raise SleeperAdpTransientError(
                f"Sleeper projections returned HTTP {response.status_code} for {url}"
            )
        return response


def _parse(rows: list[dict[str, Any]], *, stat_key: str) -> list[AdpEntry]:
    out: list[AdpEntry] = []
    for raw in rows:
        sleeper_id = _opt_str(raw.get("player_id"))
        stats = raw.get("stats") or {}
        if sleeper_id is None or not isinstance(stats, dict):
            continue
        adp = _opt_float(stats.get(stat_key))
        if adp is None:
            continue
        player = raw.get("player") if isinstance(raw.get("player"), dict) else {}
        assert isinstance(player, dict)
        out.append(
            AdpEntry(
                source=SOURCE_NAME,
                source_player_key=sleeper_id,
                name=_opt_str(player.get("full_name")) or _join_name(
                    player.get("first_name"), player.get("last_name")
                ),
                position=_opt_str(player.get("position")),
                nfl_team=_opt_str(raw.get("team")) or _opt_str(player.get("team")),
                adp=adp,
                adp_stdev=None,
                adp_high=None,
                adp_low=None,
                times_drafted=None,
            )
        )
    log.info("Parsed Sleeper ADP", stat_key=stat_key, row_count=len(out))
    return out


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _join_name(first: Any, last: Any) -> str | None:
    parts = [p for p in (_opt_str(first), _opt_str(last)) if p]
    return " ".join(parts) if parts else None


__all__ = [
    "BASE_URL",
    "SOURCE_NAME",
    "LiveSleeperAdpSource",
    "SleeperAdpClientError",
]
