"""MyFantasyLeague ADP source.

MFL aggregates ADP across its own (large) league population. Two public,
unauthenticated JSON exports are needed and joined by MFL player id:

    GET https://api.myfantasyleague.com/{year}/export?TYPE=adp&PERIOD=DRAFT
        &FCOUNT={teams}&IS_PPR={0|1}&IS_KEEPER=N&IS_MOCK=-1&CUTOFF=5&JSON=1
    GET https://api.myfantasyleague.com/{year}/export?TYPE=players&JSON=1

The ADP export carries only ids + ``averagePick`` / ``minPick`` / ``maxPick`` /
``draftsSelectedIn``; the players export supplies name / position / team. MFL's
PPR flag is **binary** (``IS_PPR`` 0 or 1) — it has no half-PPR — so a half-PPR
request is unsupported here and returns ``[]``, letting the runner fall back
(loudly) to full-PPR. ``minPick``/``maxPick`` map to ADP high (earliest) / low
(latest); MFL provides no stdev.
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
from ff_pipeline.crawlers.adp.format_map import FULL_PPR, STANDARD
from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from types import TracebackType

log = get_logger(__name__)

SOURCE_NAME = "mfl"
BASE_URL = "https://api.myfantasyleague.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = "ff-pipeline/0.1 (+personal use; ADP via myfantasyleague.com)"

#: Internal format → MFL ``IS_PPR`` flag. Half-PPR has no MFL representation.
_FORMAT_PPR_FLAG: dict[str, str] = {FULL_PPR: "1", STANDARD: "0"}


class MflClientError(RuntimeError):
    """Base class for MFL HTTP failures."""


class MflTransientError(MflClientError):
    """A 5xx or network error that survived the retry budget."""


class MflHttp(Protocol):
    """Test seam: raw decoded JSON for the adp + players exports."""

    def adp(self, *, year: int, teams: int, ppr_flag: str) -> dict[str, Any]: ...
    def players(self, *, year: int) -> dict[str, Any]: ...


class LiveMflSource:
    """Production MFL source — joins the adp + players exports into AdpEntry."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        requests_per_min: int = 30,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        http: MflHttp | None = None,
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

    def __enter__(self) -> LiveMflSource:
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
        ppr_flag = _FORMAT_PPR_FLAG.get(fmt)
        if ppr_flag is None:  # half-PPR — MFL can't serve it
            return []
        adp_payload = self._adp(year=year, teams=teams, ppr_flag=ppr_flag)
        rows = _adp_rows(adp_payload)
        if not rows:
            return []
        directory = _player_directory(self._players(year=year))
        return _parse(rows, directory)

    # ----- internals -----

    def _adp(self, *, year: int, teams: int, ppr_flag: str) -> dict[str, Any]:
        if self._http is not None:
            return self._http.adp(year=year, teams=teams, ppr_flag=ppr_flag)
        url = (
            f"{BASE_URL}/{year}/export?TYPE=adp&PERIOD=DRAFT&FCOUNT={teams}"
            f"&IS_PPR={ppr_flag}&IS_KEEPER=N&IS_MOCK=-1&CUTOFF=5&JSON=1"
        )
        return self._get_json(url)

    def _players(self, *, year: int) -> dict[str, Any]:
        if self._http is not None:
            return self._http.players(year=year)
        return self._get_json(f"{BASE_URL}/{year}/export?TYPE=players&JSON=1")

    def _get_json(self, url: str) -> dict[str, Any]:
        self._sleep_for_rate_limit()
        try:
            response = self._get(url)
        except RetryError as exc:
            raise MflTransientError(f"GET {url} failed after retries: {exc}") from exc
        if response.status_code >= 400:
            raise MflClientError(f"GET {url} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise MflClientError(f"GET {url} returned non-JSON body") from exc
        if not isinstance(payload, dict):
            raise MflClientError(f"GET {url} returned unexpected JSON type")
        return cast("dict[str, Any]", payload)

    def _sleep_for_rate_limit(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_t
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_t = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, MflTransientError)),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        stop=stop_after_attempt(3),
        reraise=False,
    )
    def _get(self, url: str) -> httpx.Response:
        assert self._client is not None
        log.debug("GET", url=url)
        response = self._client.get(url)
        if 500 <= response.status_code < 600:
            raise MflTransientError(f"MFL returned HTTP {response.status_code} for {url}")
        return response


def _adp_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _as_list((payload.get("adp") or {}).get("player"))


def _player_directory(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = _as_list((payload.get("players") or {}).get("player"))
    return {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id") is not None}


def _parse(rows: list[dict[str, Any]], directory: dict[str, dict[str, Any]]) -> list[AdpEntry]:
    out: list[AdpEntry] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        mfl_id = _opt_str(raw.get("id"))
        adp = _opt_float(raw.get("averagePick"))
        if mfl_id is None or adp is None:
            continue
        info = directory.get(mfl_id, {})
        out.append(
            AdpEntry(
                source=SOURCE_NAME,
                source_player_key=mfl_id,
                name=_opt_str(info.get("name")),
                position=_opt_str(info.get("position")),
                nfl_team=_opt_str(info.get("team")),
                adp=adp,
                adp_stdev=None,  # MFL provides no spread
                adp_high=_opt_float(raw.get("minPick")),  # earliest pick taken
                adp_low=_opt_float(raw.get("maxPick")),  # latest pick taken
                times_drafted=_opt_int(raw.get("draftsSelectedIn")),
            )
        )
    log.info("Parsed MFL ADP", row_count=len(out))
    return out


def _as_list(value: Any) -> list[dict[str, Any]]:
    """MFL collapses a single-element array to a bare object; normalize to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


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


__all__ = ["BASE_URL", "SOURCE_NAME", "LiveMflSource", "MflClientError"]
