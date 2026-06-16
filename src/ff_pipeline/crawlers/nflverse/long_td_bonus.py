"""Derive long-touchdown fantasy bonus counts from nflverse play-by-play."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class LongTdBonusKey:
    """One player's bonus-count lookup key."""

    gsis_id: str
    season_year: int
    week: int


LongTdBonusCounts = dict[LongTdBonusKey, dict[str, float]]

_PASSING_40 = "passing_yards_bonus_long_td_40"
_PASSING_50 = "passing_yards_bonus_long_td_50"
_RUSHING_40 = "rushing_yards_bonus_long_td_40"
_RUSHING_50 = "rushing_yards_bonus_long_td_50"
_RECEIVING_40 = "receiving_yards_bonus_long_td_40"
_RECEIVING_50 = "receiving_yards_bonus_long_td_50"

LONG_TD_BONUS_ZEROES: dict[str, float] = {
    _PASSING_40: 0.0,
    _PASSING_50: 0.0,
    _RUSHING_40: 0.0,
    _RUSHING_50: 0.0,
    _RECEIVING_40: 0.0,
    _RECEIVING_50: 0.0,
}


def derive_long_td_bonus_counts(
    play_by_play_rows: Iterable[Mapping[str, object]],
) -> LongTdBonusCounts:
    """Count 40+/50+ offensive TDs per ``(gsis_id, season, week)``.

    The 50-yard threshold intentionally increments both the 40+ and 50+
    keys because NFL.com's long-TD bonus tiers stack.
    """

    counts: dict[LongTdBonusKey, defaultdict[str, float]] = {}
    for row in play_by_play_rows:
        season = _as_int(row.get("season"))
        week = _as_int(row.get("week"))
        if season is None or week is None:
            continue

        if _is_truthy_number(row.get("pass_touchdown")):
            yards = _as_float(row.get("passing_yards"))
            passer = _non_empty_str(row.get("passer_player_id"))
            receiver = _non_empty_str(row.get("receiver_player_id"))
            if yards is not None:
                if passer is not None:
                    _increment(counts, passer, season, week, yards, _PASSING_40, _PASSING_50)
                if receiver is not None:
                    _increment(counts, receiver, season, week, yards, _RECEIVING_40, _RECEIVING_50)

        if _is_truthy_number(row.get("rush_touchdown")):
            yards = _as_float(row.get("rushing_yards"))
            rusher = _non_empty_str(row.get("rusher_player_id"))
            if yards is not None and rusher is not None:
                _increment(counts, rusher, season, week, yards, _RUSHING_40, _RUSHING_50)

    return {key: dict(value) for key, value in counts.items()}


def _increment(
    counts: dict[LongTdBonusKey, defaultdict[str, float]],
    gsis_id: str,
    season_year: int,
    week: int,
    yards: float,
    key_40: str,
    key_50: str,
) -> None:
    if yards < 40.0:
        return
    key = LongTdBonusKey(gsis_id=gsis_id, season_year=season_year, week=week)
    player_counts = counts.setdefault(key, defaultdict(float))
    player_counts[key_40] += 1.0
    if yards >= 50.0:
        player_counts[key_50] += 1.0


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    numeric = _as_float(value)
    return int(numeric) if numeric is not None else None


def _is_truthy_number(value: object) -> bool:
    numeric = _as_float(value)
    return numeric is not None and numeric > 0


def _non_empty_str(value: object) -> str | None:
    if value is None:
        return None
    out = str(value).strip()
    return out or None


__all__ = [
    "LONG_TD_BONUS_ZEROES",
    "LongTdBonusCounts",
    "LongTdBonusKey",
    "derive_long_td_bonus_counts",
]
