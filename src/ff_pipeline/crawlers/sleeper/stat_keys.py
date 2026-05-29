"""Mapping from Sleeper projection keys to the scoring engine's vocabulary.

Sleeper's projection / stats payloads use short codes (``pass_yd``,
``rush_td``) that differ from both nflverse and our engine. This module
projects a Sleeper ``stats`` dict onto the engine's stat keys (documented
in ``docs/05_SCORING_ENGINE.md``) so the scoring engine can score
projections under our league's rules.

Scope: we map every Sleeper key the scoring engine knows how to consume.
Sleeper-specific keys with no engine mapping (e.g. their pre-baked bonus
flags ``bonus_pass_yd_300``) are dropped — the engine derives those
bonuses itself from the underlying ``passing_yards`` value, so we'd be
double-counting if we kept them.
"""

from __future__ import annotations

# Direct 1-to-1 renames: sleeper key → engine stat key.
_RENAMES: dict[str, str] = {
    # Passing
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "passing_interceptions",
    "pass_2pt": "passing_2pt_conversions",
    # Rushing
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rush_2pt": "rushing_2pt_conversions",
    # Receiving
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt_conversions",
    # Misc offense
    "fum_lost": "fumbles_lost",
    "st_td": "special_teams_tds",  # kickoff/punt return TDs (offense column)
    # Kicking
    "xpm": "extra_point_made",
    "xpmiss": "extra_point_missed",
    "fgm_0_19": "field_goal_made_0_19",
    "fgm_20_29": "field_goal_made_20_29",
    "fgm_30_39": "field_goal_made_30_39",
    "fgm_40_49": "field_goal_made_40_49",
    # Team defense (Sleeper prefixes with ``def_``)
    "def_sack": "sacks",
    "def_int": "interceptions",
    "def_fr": "fumbles_recovered",
    "def_safe": "safeties",
    "def_td": "defensive_tds",
    "def_st_td": "special_teams_tds",
}

# Sum-of-columns: engine key on the left, contributing Sleeper keys right.
# Sleeper splits the 50+ field-goal bracket into 50-59 / 60+; the engine
# carries a single combined bucket.
_SUMS: dict[str, tuple[str, ...]] = {
    "field_goal_made_50_plus": ("fgm_50_59", "fgm_60p"),
}


def project_stats(stats: dict[str, object]) -> dict[str, float]:
    """Return ``{engine_stat_key: numeric_value}`` for one Sleeper stats dict.

    Behaves like ``nflverse.stat_keys.project_stats``: missing keys become
    zeros (so the JSON payload is shape-stable across players), and any
    unrecognized Sleeper keys are dropped silently — they're either pre-
    baked bonuses we recompute ourselves or stats outside the engine's
    scope (e.g. punter / return-yards depth).
    """

    out: dict[str, float] = {}
    for src, dest in _RENAMES.items():
        out[dest] = _as_float(stats.get(src))
    for dest, srcs in _SUMS.items():
        out[dest] = sum(_as_float(stats.get(s)) for s in srcs)
    return out


def expected_sleeper_keys() -> frozenset[str]:
    """Every Sleeper key the projection consumes (renames + summands)."""

    cols: set[str] = set(_RENAMES)
    for srcs in _SUMS.values():
        cols.update(srcs)
    return frozenset(cols)


def _as_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):  # bool is an int subclass; treat as 0/1
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


__all__ = ["expected_sleeper_keys", "project_stats"]
