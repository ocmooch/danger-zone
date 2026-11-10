"""Fantasy Football Calculator ADP source.

FFC exposes a free, team-count-aware historical ADP REST API:

    GET https://fantasyfootballcalculator.com/api/v1/adp/{slug}?teams=12&year=YYYY

``slug`` is the scoring format (``standard`` / ``ppr`` / ``half-ppr``). The
response is ``{"status", "meta", "players": [...]}`` where each player carries a
numeric FFC ``player_id`` plus ``name`` / ``position`` / ``team`` / ``adp`` /
``stdev`` / ``high`` / ``low`` / ``times_drafted``. Data updates once a day and
historical years are immutable, so callers should cache aggressively and throttle
(attribution to FFC is requested).

Mirrors the Sleeper client's seam: ``FfcHttp`` protocol, ``LiveFfcSource``
(httpx + polite throttle + retry), and a parse step into :class:`AdpEntry`.
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

SOURCE_NAME = "ffc"
BASE_URL = "https://fantasyfootballcalculator.com/api/v1/adp"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = "ff-pipeline/0.1 (+personal use; ADP via fantasyfootballcalculator.com)"

#: Our internal format names → FFC's URL slug.
_FORMAT_SLUG: dict[str, str] = {
    FULL_PPR: "ppr",
    HALF_PPR: "half-ppr",
    STANDARD: "standard",
}


class FfcClientError(RuntimeError):
    """Base class for FFC HTTP failures."""


class FfcTransientError(FfcClientError):
    """A 5xx or network error that survived the retry budget."""


class FfcHttp(Protocol):
    """Test seam: returns raw decoded JSON for one ``(slug, year, teams)`` ask."""

    def adp(self, slug: str, *, year: int, teams: int) -> dict[str, Any]: ...


class LiveFfcSource:
    """Production FFC source — httpx + polite throttle, parsed into AdpEntry."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        requests_per_min: int = 30,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        http: FfcHttp | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        rpm = max(1, min(120, int(requests_per_min)))
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

    # ----- context manager -----

    def __enter__(self) -> LiveFfcSource:
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
        slug = _FORMAT_SLUG.get(fmt)
        if slug is None:
            return []
        payload = self._fetch_json(slug, year=year, teams=teams)
        return _parse(payload)

    # ----- internals -----

    def _fetch_json(self, slug: str, *, year: int, teams: int) -> dict[str, Any]:
        if self._http is not None:
            return self._http.adp(slug, year=year, teams=teams)
        url = f"{BASE_URL}/{slug}?teams={teams}&year={year}"
        self._sleep_for_rate_limit()
        try:
            response = self._get(url)
        except RetryError as exc:
            raise FfcTransientError(f"GET {url} failed after retries: {exc}") from exc
        if response.status_code >= 400:
            raise FfcClientError(f"GET {url} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise FfcClientError(f"GET {url} returned non-JSON body") from exc
        if not isinstance(payload, dict):
            raise FfcClientError(f"GET {url} returned unexpected JSON type")
        return cast("dict[str, Any]", payload)

    def _sleep_for_rate_limit(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_t = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, FfcTransientError)),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        stop=stop_after_attempt(3),
        reraise=False,
    )
    def _get(self, url: str) -> httpx.Response:
        assert self._client is not None  # only called on the live path
        log.debug("GET", url=url)
        response = self._client.get(url)
        if 500 <= response.status_code < 600:
            raise FfcTransientError(f"FFC returned HTTP {response.status_code} for {url}")
        return response


def _parse(payload: dict[str, Any]) -> list[AdpEntry]:
    players = payload.get("players")
    if not isinstance(players, list):
        return []
    out: list[AdpEntry] = []
    for raw in players:
        if not isinstance(raw, dict):
            continue
        adp = _opt_float(raw.get("adp"))
        if adp is None:
            continue
        name = _opt_str(raw.get("name"))
        position = _opt_str(raw.get("position"))
        team = _opt_str(raw.get("team"))
        key = _opt_str(raw.get("player_id")) or _synth_key(name, team, position)
        out.append(
            AdpEntry(
                source=SOURCE_NAME,
                source_player_key=key,
                name=name,
                position=position,
                nfl_team=team,
                adp=adp,
                adp_stdev=_opt_float(raw.get("stdev")),
                adp_high=_opt_float(raw.get("high")),
                adp_low=_opt_float(raw.get("low")),
                times_drafted=_opt_int(raw.get("times_drafted")),
            )
        )
    log.info("Parsed FFC ADP", row_count=len(out))
    return out


def _synth_key(name: str | None, team: str | None, position: str | None) -> str:
    return "|".join((name or "?", team or "?", position or "?")).lower()


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


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


__all__ = ["BASE_URL", "SOURCE_NAME", "FfcClientError", "LiveFfcSource"]
