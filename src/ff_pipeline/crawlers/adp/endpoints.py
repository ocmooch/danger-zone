"""Typed ADP entry + the source seam shared by every ADP provider.

Every source (FFC / MFL / Sleeper / a fixture) yields the same flat
:class:`AdpEntry` list for one ``(year, format, teams)`` ask. ``fetch`` returns
an empty list when the source can't serve that format for that year — the runner
treats empty as "unavailable" and walks the fallback chain. Keeping the shape
uniform means the runner, the matcher, and the blend never special-case a source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Path is a runtime annotation on a dataclass field; keep eager.
from pathlib import Path  # noqa: TC003
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AdpEntry:
    """One consensus ADP observation for a player, normalized across sources.

    ``source_player_key`` is the source's own stable id when it has one (FFC and
    MFL numeric ids), else a synthesized ``name|team|pos`` slug — it is the
    natural upsert key together with ``(season, source)``.
    """

    source: str
    source_player_key: str
    name: str | None
    position: str | None
    nfl_team: str | None
    adp: float
    adp_stdev: float | None = None
    adp_high: float | None = None
    adp_low: float | None = None
    times_drafted: int | None = None


class AdpSource(Protocol):
    """A provider of ADP entries for one season/format.

    ``name`` is the persisted ``player_adp.source`` value (``'ffc'`` etc.).
    ``fetch`` returns ``[]`` for a format/year the source can't serve, which the
    runner reads as "try the next format in the fallback chain".
    """

    name: str

    def fetch(self, *, year: int, fmt: str, teams: int) -> list[AdpEntry]: ...


@dataclass(frozen=True, slots=True)
class LocalFixtureAdpSource:
    """Reads pre-saved ADP JSON fixtures, so tests never touch the network.

    Filenames: ``{source}_{year}_{fmt}.json`` (e.g. ``ffc_2015_full_ppr.json``),
    each an already-normalized list of :class:`AdpEntry` field dicts. A missing
    file means "format unavailable for that year" and returns ``[]`` — exactly
    the signal the runner uses to exercise the loud fallback chain.
    """

    name: str
    directory: Path

    def fetch(self, *, year: int, fmt: str, teams: int) -> list[AdpEntry]:
        _ = teams  # fixtures are keyed by (source, year, fmt) only
        path = self.directory / f"{self.name}_{year}_{fmt}.json"
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as fh:
            raw: list[dict[str, Any]] = json.load(fh)
        return [_entry_from_dict(self.name, row) for row in raw]


def _entry_from_dict(source: str, row: dict[str, Any]) -> AdpEntry:
    return AdpEntry(
        source=source,
        source_player_key=str(row["source_player_key"]),
        name=row.get("name"),
        position=row.get("position"),
        nfl_team=row.get("nfl_team"),
        adp=float(row["adp"]),
        adp_stdev=_opt_float(row.get("adp_stdev")),
        adp_high=_opt_float(row.get("adp_high")),
        adp_low=_opt_float(row.get("adp_low")),
        times_drafted=_opt_int(row.get("times_drafted")),
    )


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


__all__ = ["AdpEntry", "AdpSource", "LocalFixtureAdpSource"]
