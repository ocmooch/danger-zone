"""Unit tests for deriving a transaction's effective week from its date.

Calibrated against the real 2010 schedule and 2010's already-labeled
transactions: a move is effective for the next games to be played, so its week
is the one whose last gameday is the earliest on or after the move date.
"""

from __future__ import annotations

from datetime import date

from ff_pipeline.normalizer.transaction_weeks import (
    effective_week_for_date,
    week_last_gamedays,
)

# A slice of the 2010 regular-season schedule (first/last gameday per week).
_SCHEDULE_2010 = [
    {"game_type": "REG", "week": 1, "gameday": "2010-09-09"},
    {"game_type": "REG", "week": 1, "gameday": "2010-09-13"},
    {"game_type": "REG", "week": 2, "gameday": "2010-09-19"},
    {"game_type": "REG", "week": 2, "gameday": "2010-09-20"},
    {"game_type": "REG", "week": 6, "gameday": "2010-10-17"},
    {"game_type": "REG", "week": 6, "gameday": "2010-10-18"},
    {"game_type": "REG", "week": 7, "gameday": "2010-10-24"},
    {"game_type": "REG", "week": 7, "gameday": "2010-10-25"},
    # Postseason rows must never contribute a regular-season week.
    {"game_type": "WC", "week": 18, "gameday": "2011-01-08"},
]


def test_week_last_gamedays_keeps_latest_reg_gameday_only() -> None:
    last = week_last_gamedays(_SCHEDULE_2010)
    assert last[1] == date(2010, 9, 13)
    assert last[2] == date(2010, 9, 20)
    assert last[6] == date(2010, 10, 18)
    assert 18 not in last  # postseason excluded


def test_effective_week_matches_labeled_2010_transactions() -> None:
    last = week_last_gamedays(_SCHEDULE_2010)
    # Neil Rackers' real add: 2010-09-16 → W2 (exactly where the snapshot shows him).
    assert effective_week_for_date(date(2010, 9, 16), last) == 2
    # A pickup the Thursday/weekend of W6 games is effective W6.
    assert effective_week_for_date(date(2010, 10, 14), last) == 6
    assert effective_week_for_date(date(2010, 10, 18), last) == 6
    # The Monday after W6 games rolls to W7.
    assert effective_week_for_date(date(2010, 10, 19), last) == 7


def test_preseason_date_maps_to_week_one() -> None:
    last = week_last_gamedays(_SCHEDULE_2010)
    assert effective_week_for_date(date(2010, 8, 1), last) == 1


def test_postseason_date_returns_none() -> None:
    last = week_last_gamedays(_SCHEDULE_2010)
    # After the final regular-season gameday in the slice (W7, 2010-10-25).
    assert effective_week_for_date(date(2011, 1, 1), last) is None


def test_empty_schedule_returns_none() -> None:
    assert effective_week_for_date(date(2010, 9, 16), {}) is None
