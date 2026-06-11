"""Mapping from nflverse ``load_player_stats`` columns to the scoring
engine's stat-key vocabulary.

The engine's stat keys are documented in ``docs/05_SCORING_ENGINE.md``.
nflverse uses similar names for most keys but differs on a few:

* ``fumble_recovery_tds`` → ``fumble_return_tds`` (engine name)
* ``fg_made_50_59`` + ``fg_made_60_`` → ``field_goal_made_50_plus`` (summed)
* ``fg_missed`` (total) → ``field_goal_missed``
* ``pat_made`` → ``extra_point_made``
* ``pat_missed`` → ``extra_point_missed``
* fumbles_lost is derived: sum of ``sack_fumbles_lost`` (passing) +
  ``rushing_fumbles_lost`` + ``receiving_fumbles_lost``.

Out of M4 scope (need play-by-play, land in M7):

* ``passing_yards_bonus_long_td_40`` / ``_50`` (count of TDs of 40+/50+ yds)
* ``rushing_yards_bonus_long_td_40`` / ``_50``
* ``receiving_yards_bonus_long_td_40`` / ``_50``

These keys are tracked in ``LONG_TD_BONUS_STAT_KEYS`` so callers can detect when
a scored total may be understated because the source data never provides them.

Team-defense keys (``sacks``, ``interceptions``, ``points_allowed``,
``total_yards_allowed``, etc.) are *not* projected here — they need
team-level derivation from ``load_team_stats`` + ``load_schedules`` rather
than the per-player weekly file. That rollup lives in ``team_defense.py``
and is keyed by NFL team rather than ``gsis_id``.
"""

from __future__ import annotations

# Direct passthroughs: nflverse column name == engine stat key.
_DIRECT_KEYS: frozenset[str] = frozenset(
    {
        "passing_yards",
        "passing_tds",
        "passing_interceptions",
        "passing_2pt_conversions",
        "rushing_yards",
        "rushing_tds",
        "rushing_2pt_conversions",
        "receptions",
        "receiving_yards",
        "receiving_tds",
        "receiving_2pt_conversions",
        "special_teams_tds",
    }
)

# 1-to-1 renames.
_RENAMES: dict[str, str] = {
    "fumble_recovery_tds": "fumble_return_tds",
    "pat_made": "extra_point_made",
    "pat_missed": "extra_point_missed",
    "fg_made_0_19": "field_goal_made_0_19",
    "fg_made_20_29": "field_goal_made_20_29",
    "fg_made_30_39": "field_goal_made_30_39",
    "fg_made_40_49": "field_goal_made_40_49",
}

# Sum-of-columns aggregations: engine key on the left, contributing
# nflverse columns on the right.
_SUMS: dict[str, tuple[str, ...]] = {
    "field_goal_made_50_plus": ("fg_made_50_59", "fg_made_60_"),
    "fumbles_lost": (
        "sack_fumbles_lost",
        "rushing_fumbles_lost",
        "receiving_fumbles_lost",
    ),
    "field_goal_missed": (
        "fg_missed_0_19",
        "fg_missed_20_29",
        "fg_missed_30_39",
        "fg_missed_40_49",
        "fg_missed_50_59",
        "fg_missed_60_",
    ),
}


def project_stats(row: dict[str, object]) -> dict[str, float]:
    """Return ``{engine_stat_key: numeric_value}`` for one nflverse row.

    Missing keys default to 0. Non-numeric values for an expected stat
    column are coerced to 0 with no error — nflverse occasionally emits
    ``null`` for entire categories on players who didn't participate.
    Zero-valued stats are kept in the dict (rather than dropped) so the
    JSON payload stored in ``player_stats_raw.stats`` is shape-stable
    across players and can be diffed.
    """

    result: dict[str, float] = {}
    for key in _DIRECT_KEYS:
        result[key] = _as_float(row.get(key))
    for src, dest in _RENAMES.items():
        result[dest] = _as_float(row.get(src))
    for dest, srcs in _SUMS.items():
        result[dest] = sum(_as_float(row.get(s)) for s in srcs)
    return result


def expected_nflverse_columns() -> frozenset[str]:
    """Every nflverse column the projection consumes.

    Used by the client to validate fixtures and to log a single warning if
    nflverse renames or removes a column we depend on.
    """

    cols: set[str] = set(_DIRECT_KEYS)
    cols.update(_RENAMES.keys())
    for srcs in _SUMS.values():
        cols.update(srcs)
    return frozenset(cols)


# Stat keys that require play-by-play analysis to populate (deferred to M7).
# nflverse's load_player_stats() never includes these columns, so they are
# absent from every player_stats_raw row ingested via this crawler.  When the
# scoring engine scores a row that lacks these keys it silently defaults them
# to 0, potentially understating the total by the long-TD bonus points.
# Downstream consumers (BFF, rescore warnings) reference this set to detect
# and surface the gap rather than silently displaying incomplete totals.
LONG_TD_BONUS_STAT_KEYS: frozenset[str] = frozenset(
    {
        "passing_yards_bonus_long_td_40",
        "passing_yards_bonus_long_td_50",
        "rushing_yards_bonus_long_td_40",
        "rushing_yards_bonus_long_td_50",
        "receiving_yards_bonus_long_td_40",
        "receiving_yards_bonus_long_td_50",
    }
)


def _as_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):  # bool is a subclass of int; treat as 0/1
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


__all__ = ["LONG_TD_BONUS_STAT_KEYS", "expected_nflverse_columns", "project_stats"]
