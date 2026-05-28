"""Typed wrappers around the Sleeper endpoints we consume.

The HTTP layer in ``client.py`` returns raw decoded JSON. This module
parses those payloads into small frozen dataclasses keyed by Sleeper's
``player_id`` string. Downstream code (the runner) deals exclusively in
these typed shapes — keeps the projection-mapping logic and DB-write
logic readable and easy to test against a small in-memory fixture.

Why a dedicated layer rather than passing raw dicts straight through:

* The Sleeper response shapes are JSON-y but inconsistent (a numeric ID
  may arrive as ``int`` or ``str``; missing fields are sometimes absent,
  sometimes ``null``). Centralizing the coercion here means every caller
  gets the same hygienic shape.
* Tests can construct ``SleeperPlayer`` / ``SleeperProjection`` instances
  directly without hand-rolling the wire-format dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ff_pipeline.crawlers.sleeper.stat_keys import project_stats
from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from ff_pipeline.crawlers.sleeper.client import SleeperSource

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SleeperPlayer:
    """One row from ``/v1/players/nfl`` — the platform's master player list.

    The cross-platform IDs (``gsis_id`` / ``espn_id`` / ``yahoo_id``) are
    what makes this endpoint valuable: it's the cleanest way to populate
    ``players.sleeper_id`` for players we already know about by GSIS ID.
    """

    sleeper_id: str
    gsis_id: str | None
    espn_id: str | None
    yahoo_id: str | None
    full_name: str | None
    first_name: str | None
    last_name: str | None
    position: str | None
    nfl_team: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class SleeperProjection:
    """One row from ``/projections/nfl/{year}/{week}``.

    ``stats`` is already projected into the engine's stat-key vocabulary
    so the runner can pass it straight into ``apply_rules``.
    """

    sleeper_id: str
    season_year: int
    week: int
    season_type: str
    stats: dict[str, float]


@dataclass(frozen=True, slots=True)
class SleeperTrend:
    """One row from ``/v1/players/nfl/trending/{add,drop}``."""

    sleeper_id: str
    count: int


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SleeperClient:
    """High-level reader: typed dataclasses for every endpoint we use."""

    def __init__(self, source: SleeperSource) -> None:
        self._source = source

    def players(self) -> list[SleeperPlayer]:
        payload = self._source.players()
        out: list[SleeperPlayer] = []
        for raw in payload.values():
            sleeper_id = _opt_str(raw.get("player_id"))
            if sleeper_id is None:
                continue
            out.append(
                SleeperPlayer(
                    sleeper_id=sleeper_id,
                    gsis_id=_opt_str(raw.get("gsis_id")),
                    espn_id=_opt_str(raw.get("espn_id")),
                    yahoo_id=_opt_str(raw.get("yahoo_id")),
                    full_name=_opt_str(raw.get("full_name"))
                    or _join_name(raw.get("first_name"), raw.get("last_name")),
                    first_name=_opt_str(raw.get("first_name")),
                    last_name=_opt_str(raw.get("last_name")),
                    position=_opt_str(raw.get("position")),
                    nfl_team=_opt_str(raw.get("team")),
                    is_active=bool(raw.get("active", True)),
                )
            )
        log.info("Loaded Sleeper players", row_count=len(out))
        return out

    def projections(
        self,
        year: int,
        week: int,
        *,
        season_type: str = "regular",
    ) -> list[SleeperProjection]:
        payload = self._source.projections(year, week, season_type=season_type)
        out: list[SleeperProjection] = []
        for raw in payload:
            sleeper_id = _opt_str(raw.get("player_id"))
            if sleeper_id is None:
                continue
            stats_dict = raw.get("stats") or {}
            if not isinstance(stats_dict, dict):
                log.warning(
                    "Skipping Sleeper projection with non-dict stats",
                    sleeper_id=sleeper_id,
                    stats_type=type(stats_dict).__name__,
                )
                continue
            out.append(
                SleeperProjection(
                    sleeper_id=sleeper_id,
                    season_year=_coerce_int(raw.get("season"), default=year),
                    week=_coerce_int(raw.get("week"), default=week),
                    season_type=_opt_str(raw.get("season_type")) or season_type,
                    stats=project_stats(stats_dict),
                )
            )
        log.info(
            "Loaded Sleeper projections",
            year=year,
            week=week,
            row_count=len(out),
        )
        return out

    def trending(
        self,
        kind: str,
        *,
        lookback_hours: int = 24,
        limit: int = 25,
    ) -> list[SleeperTrend]:
        payload = self._source.trending(kind, lookback_hours=lookback_hours, limit=limit)
        out: list[SleeperTrend] = []
        for raw in payload:
            sleeper_id = _opt_str(raw.get("player_id"))
            if sleeper_id is None:
                continue
            out.append(
                SleeperTrend(
                    sleeper_id=sleeper_id,
                    count=_coerce_int(raw.get("count"), default=0),
                )
            )
        log.info(
            "Loaded Sleeper trending",
            kind=kind,
            lookback_hours=lookback_hours,
            row_count=len(out),
        )
        return out


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _coerce_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _join_name(first: Any, last: Any) -> str | None:
    f = _opt_str(first)
    last_str = _opt_str(last)
    parts = [p for p in (f, last_str) if p]
    return " ".join(parts) if parts else None


__all__ = [
    "SleeperClient",
    "SleeperPlayer",
    "SleeperProjection",
    "SleeperTrend",
]
