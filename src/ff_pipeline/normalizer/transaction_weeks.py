"""Derive a transaction's effective NFL week from its execution date.

NFL.com renders each transaction's week in a ``.transactionWeek`` cell, which
the parser reads directly into ``effective_week``. For some historical seasons
(notably 2010's early weeks) that cell is blank, leaving ``effective_week``
NULL even though the transaction's ``executed_at`` date is present. This module
reconstructs the week from that date using the season's regular-season schedule.

A transaction takes effect for the next set of games to be played, so its
effective week is the regular-season week whose games are the first to occur on
or after the transaction date — i.e. the smallest week whose *last* gameday is
on or after the date. A date before the season's first games maps to week 1 (a
preseason add is effective week 1); a date after the final regular-season game
maps to ``None`` (a post-season move we do not fabricate a week for).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


def week_last_gamedays(schedule_rows: Iterable[Mapping[str, object]]) -> dict[int, date]:
    """Map each regular-season week to the date of its last game.

    ``schedule_rows`` are nflverse ``load_schedules`` records; only ``REG``
    games contribute. Rows missing a week or a parseable gameday are ignored.
    """
    last_seen: dict[int, date] = {}
    for row in schedule_rows:
        if row.get("game_type") != "REG":
            continue
        raw_week = row.get("week")
        gameday = _as_date(row.get("gameday"))
        if raw_week is None or gameday is None:
            continue
        week = int(raw_week)  # type: ignore[call-overload]
        if week not in last_seen or gameday > last_seen[week]:
            last_seen[week] = gameday
    return last_seen


def effective_week_for_date(d: date, last_gamedays: Mapping[int, date]) -> int | None:
    """The regular-season week a transaction executed on ``d`` takes effect for.

    The week whose last gameday is the earliest one on or after ``d``. Returns
    ``None`` when ``d`` falls after the final regular-season game (post-season)
    or when the schedule is empty.
    """
    candidates = [(gameday, week) for week, gameday in last_gamedays.items() if gameday >= d]
    if not candidates:
        return None
    return min(candidates)[1]


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
