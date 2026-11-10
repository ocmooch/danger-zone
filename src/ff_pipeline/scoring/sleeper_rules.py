"""Sleeper ``scoring_settings`` → ``scoring_rules`` rows, and Sleeper stats →
engine stat keys (S6, decision D-10).

Rules are written in the same shape the NFL.com-era rules use, so a 2026 rule
that means the same thing as a 2025 rule is the *same row*:

* a per-yard rate (0.04, 0.1) becomes "1 point per 25 / 10 yards";
* the yardage bonuses are **tiered** flat brackets (the S2 probe: a 400-yd
  passer gets ``bonus_pass_yd_400`` only); long-TD bonuses **stack** (counts);
* Sleeper's FG rule is ``fgm`` per make plus distance add-ons; it is folded
  into per-range rows (``field_goal_made_50_plus`` = ``fgm + fgm_50_59``,
  which needs ``fgm_50_59 == fgm_60p``);
* points-allowed / yards-allowed brackets are kept even when worth 0 (as in
  2025), and adjacent brackets worth the same merge
  (``yds_allow_300_349`` + ``_350_399`` → 300-399);
* nflverse's DST ``fumbles_recovered`` already includes special-teams
  recoveries, so ``def_st_fum_rec`` is written as the *difference* to
  ``fum_rec`` on ``defense_special_teams_fumble_recoveries`` (0 → no row).

Zero-valued keys write nothing. A **non-zero key this module doesn't know
raises** ``UnmappedScoringKeyError`` — a new Sleeper setting must be mapped
(and signed off) before rules are written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True, order=True)
class RuleRow:
    """One ``scoring_rules`` row (the DB shape; ``raw_text`` names the source key)."""

    category: str
    stat_key: str
    points_per_unit: float
    unit_size: float
    threshold_min: float | None
    threshold_max: float | None
    flat_points: float | None
    raw_text: str = ""

    @property
    def identity(self) -> tuple[Any, ...]:
        """Everything that scores (``raw_text`` excluded)."""
        return (
            self.category,
            self.stat_key,
            round(self.points_per_unit, 6),
            round(self.unit_size, 6),
            self.threshold_min,
            self.threshold_max,
            self.flat_points,
        )


class UnmappedScoringKeyError(ValueError):
    def __init__(self, keys: Iterable[str]) -> None:
        self.keys = tuple(sorted(keys))
        super().__init__(
            "Sleeper scoring keys with non-zero points that have no mapping: "
            + ", ".join(self.keys)
        )


# Per-unit keys: sleeper key → (category, engine stat key).
_PER_UNIT: dict[str, tuple[str, str]] = {
    "pass_yd": ("passing", "passing_yards"),
    "pass_td": ("passing", "passing_tds"),
    "pass_int": ("passing", "passing_interceptions"),
    "pass_2pt": ("passing", "passing_2pt_conversions"),
    "pass_td_40p": ("passing", "passing_yards_bonus_long_td_40"),
    "pass_td_50p": ("passing", "passing_yards_bonus_long_td_50"),
    "pass_cmp_40p": ("passing", "passing_completions_40_plus"),
    "rush_yd": ("rushing", "rushing_yards"),
    "rush_td": ("rushing", "rushing_tds"),
    "rush_2pt": ("rushing", "rushing_2pt_conversions"),
    "rush_td_40p": ("rushing", "rushing_yards_bonus_long_td_40"),
    "rush_td_50p": ("rushing", "rushing_yards_bonus_long_td_50"),
    "rush_40p": ("rushing", "rushing_attempts_40_plus"),
    "rec": ("receiving", "receptions"),
    "rec_yd": ("receiving", "receiving_yards"),
    "rec_td": ("receiving", "receiving_tds"),
    "rec_2pt": ("receiving", "receiving_2pt_conversions"),
    "rec_td_40p": ("receiving", "receiving_yards_bonus_long_td_40"),
    "rec_td_50p": ("receiving", "receiving_yards_bonus_long_td_50"),
    "rec_40p": ("receiving", "receptions_40_plus"),
    "fum_lost": ("misc", "fumbles_lost"),
    "st_td": ("misc", "special_teams_tds"),
    "fum_rec_td": ("misc", "fumble_return_tds"),
    "st_fum_rec": ("misc", "special_teams_fumble_recoveries"),
    "xpm": ("kicking", "extra_point_made"),
    "xpmiss": ("kicking", "extra_point_missed"),
    "fgmiss": ("kicking", "field_goal_missed"),
    "sack": ("defense", "sacks"),
    "int": ("defense", "interceptions"),
    "fum_rec": ("defense", "fumbles_recovered"),
    "safe": ("defense", "safeties"),
    "def_td": ("defense", "defensive_tds"),
    "def_st_td": ("defense", "special_teams_tds"),
    "blk_kick": ("defense", "blocked_kicks"),
    "def_2pt": ("defense", "defensive_2pt_returns"),
}

# Tiered yardage bonuses: (sleeper key, category, stat, lower, upper | None).
_TIERED_BONUSES: tuple[tuple[str, str, str, float, float | None], ...] = (
    ("bonus_pass_yd_300", "passing", "passing_yards", 300.0, 399.0),
    ("bonus_pass_yd_400", "passing", "passing_yards", 400.0, None),
    ("bonus_rush_yd_100", "rushing", "rushing_yards", 100.0, 199.0),
    ("bonus_rush_yd_200", "rushing", "rushing_yards", 200.0, None),
    ("bonus_rec_yd_100", "receiving", "receiving_yards", 100.0, 199.0),
    ("bonus_rec_yd_200", "receiving", "receiving_yards", 200.0, None),
)

# Bracket families: sleeper key → (lower, upper | None), per engine stat.
_BRACKETS: dict[str, tuple[str, dict[str, tuple[float, float | None]]]] = {
    "pts_allow": (
        "points_allowed",
        {
            "pts_allow_0": (0.0, 0.0),
            "pts_allow_1_6": (1.0, 6.0),
            "pts_allow_7_13": (7.0, 13.0),
            "pts_allow_14_20": (14.0, 20.0),
            "pts_allow_21_27": (21.0, 27.0),
            "pts_allow_28_34": (28.0, 34.0),
            "pts_allow_35p": (35.0, None),
        },
    ),
    "yds_allow": (
        "total_yards_allowed",
        {
            "yds_allow_0_100": (0.0, 99.0),  # league page: "less than 100"
            "yds_allow_100_199": (100.0, 199.0),
            "yds_allow_200_299": (200.0, 299.0),
            "yds_allow_300_349": (300.0, 349.0),
            "yds_allow_350_399": (350.0, 399.0),
            "yds_allow_400_449": (400.0, 449.0),
            "yds_allow_450_499": (450.0, 499.0),
            "yds_allow_500_549": (500.0, 549.0),
            "yds_allow_550p": (550.0, None),
        },
    ),
}

_FG_RANGES: tuple[tuple[str, str], ...] = (
    ("fgm_0_19", "field_goal_made_0_19"),
    ("fgm_20_29", "field_goal_made_20_29"),
    ("fgm_30_39", "field_goal_made_30_39"),
    ("fgm_40_49", "field_goal_made_40_49"),
)
_FG_KEYS = frozenset({"fgm", "fgm_50p", "fgm_50_59", "fgm_60p", *(k for k, _ in _FG_RANGES)})
_HANDLED = (
    frozenset(_PER_UNIT)
    | {k for k, *_ in _TIERED_BONUSES}
    | {k for _, b in _BRACKETS.values() for k in b}
    | _FG_KEYS
    | {"def_st_fum_rec"}
)


def derive_rules(scoring_settings: Mapping[str, Any]) -> list[RuleRow]:
    """The ``scoring_rules`` rows equivalent to a Sleeper league's settings."""
    pts = {k: float(v) for k, v in scoring_settings.items() if _is_number(v)}
    unknown = [k for k, v in pts.items() if v != 0 and k not in _HANDLED]
    if unknown:
        raise UnmappedScoringKeyError(unknown)

    rows: list[RuleRow] = []
    for key, (category, stat) in _PER_UNIT.items():
        value = pts.get(key, 0.0)
        if value:
            rows.append(_per_unit(category, stat, value, key))

    st_delta = pts.get("def_st_fum_rec", 0.0) - pts.get("fum_rec", 0.0)
    if st_delta:
        rows.append(
            _per_unit(
                "defense",
                "defense_special_teams_fumble_recoveries",
                st_delta,
                "def_st_fum_rec - fum_rec",
            )
        )

    fgm = pts.get("fgm", 0.0)
    for key, stat in _FG_RANGES:
        value = fgm + pts.get(key, 0.0)
        if value:
            rows.append(_per_unit("kicking", stat, value, f"fgm + {key}"))
    over_50 = fgm + pts.get("fgm_50p", 0.0)
    if pts.get("fgm_50_59", 0.0) != pts.get("fgm_60p", 0.0):
        raise UnmappedScoringKeyError(["fgm_50_59 != fgm_60p (no 60+ FG stat to score apart)"])
    over_50 += pts.get("fgm_50_59", 0.0)
    if over_50:
        rows.append(
            _per_unit("kicking", "field_goal_made_50_plus", over_50, "fgm + fgm_50p + fgm_50_59")
        )

    for key, category, stat, lo, hi in _TIERED_BONUSES:
        value = pts.get(key, 0.0)
        if value:
            rows.append(RuleRow(category, stat, 0.0, 1.0, lo, hi, value, key))

    for stat, brackets in _BRACKETS.values():
        if not any(k in pts for k in brackets):
            continue
        rows.extend(_merged_brackets(stat, brackets, pts))
    return sorted(rows)


def _per_unit(category: str, stat: str, value: float, raw: str) -> RuleRow:
    """1 point per N units when the rate is 1/N for an integer N (0.04 → 1/25)."""
    if 0 < abs(value) < 1:
        n = 1 / abs(value)
        if abs(n - round(n)) < 1e-9:
            return RuleRow(
                category, stat, 1.0 if value > 0 else -1.0, float(round(n)), 0.0, None, None, raw
            )
    return RuleRow(category, stat, value, 1.0, 0.0, None, None, raw)


def _merged_brackets(
    stat: str, brackets: dict[str, tuple[float, float | None]], pts: Mapping[str, float]
) -> list[RuleRow]:
    ordered = sorted(brackets.items(), key=lambda kv: kv[1][0])
    out: list[RuleRow] = []
    for key, (lo, hi) in ordered:
        value = pts.get(key, 0.0)
        prev = out[-1] if out else None
        if prev is not None and prev.flat_points == value and prev.threshold_max == lo - 1:
            out[-1] = RuleRow(
                "defense", stat, 0.0, 1.0, prev.threshold_min, hi, value, f"{prev.raw_text}+{key}"
            )
        else:
            out.append(RuleRow("defense", stat, 0.0, 1.0, lo, hi, value, key))
    return out


@dataclass(frozen=True, slots=True)
class RuleDiff:
    only_in_a: tuple[RuleRow, ...]
    only_in_b: tuple[RuleRow, ...]
    same: int

    @property
    def identical(self) -> bool:
        return not self.only_in_a and not self.only_in_b


def diff_rules(a: Iterable[RuleRow], b: Iterable[RuleRow]) -> RuleDiff:
    a_by = {r.identity: r for r in a}
    b_by = {r.identity: r for r in b}
    return RuleDiff(
        only_in_a=tuple(sorted(a_by[k] for k in a_by.keys() - b_by.keys())),
        only_in_b=tuple(sorted(b_by[k] for k in b_by.keys() - a_by.keys())),
        same=len(a_by.keys() & b_by.keys()),
    )


def describe(row: RuleRow) -> str:
    """One readable line, e.g. ``passing  passing_yards  1 per 25``."""
    if row.flat_points is not None:
        hi = "+" if row.threshold_max is None else f"-{row.threshold_max:g}"
        what = f"{row.flat_points:+g} at {row.threshold_min:g}{hi}"
    elif row.unit_size != 1:
        what = f"{row.points_per_unit:g} per {row.unit_size:g}"
    else:
        what = f"{row.points_per_unit:g} each"
    return f"{row.category:9s} {row.stat_key:42s} {what}"


def write_season_rules(session: Session, season_id: int, rows: Iterable[RuleRow]) -> int:
    """Replace one season's ``scoring_rules`` with ``rows`` (the caller commits).

    A sealed season refuses at the DB (S1 triggers)."""
    from sqlalchemy import delete

    from ff_pipeline.repository.models import ScoringRule

    session.execute(delete(ScoringRule).where(ScoringRule.season_id == season_id))
    out = [
        ScoringRule(
            season_id=season_id,
            category=r.category,
            stat_key=r.stat_key,
            points_per_unit=r.points_per_unit,
            unit_size=r.unit_size,
            threshold_min=r.threshold_min,
            threshold_max=r.threshold_max,
            flat_points=r.flat_points,
            raw_text=f"sleeper:{r.raw_text}",
        )
        for r in rows
    ]
    session.add_all(out)
    session.flush()
    return len(out)


# ----- Sleeper weekly stats → engine keys (S6-D3, and the engine parity test) -----

_STAT_DIRECT: dict[str, str] = {
    sleeper: stat for sleeper, (_cat, stat) in _PER_UNIT.items() if _cat != "defense"
}
_STAT_DIRECT.update(
    {
        "fgm_0_19": "field_goal_made_0_19",
        "fgm_20_29": "field_goal_made_20_29",
        "fgm_30_39": "field_goal_made_30_39",
        "fgm_40_49": "field_goal_made_40_49",
    }
)
_DEF_STATS: dict[str, str] = {
    "sack": "sacks",
    "int": "interceptions",
    "safe": "safeties",
    "def_td": "defensive_tds",
    "def_st_td": "special_teams_tds",
    "blk_kick": "blocked_kicks",
    "def_2pt": "defensive_2pt_returns",
    "def_st_fum_rec": "defense_special_teams_fumble_recoveries",
    "pts_allow": "points_allowed",
    "yds_allow": "total_yards_allowed",
}


def sleeper_stats_to_engine(stats: Mapping[str, Any], *, is_team_defense: bool) -> dict[str, float]:
    """Sleeper's stat line in the engine's vocabulary (only keys the rules use)."""
    s = {k: float(v) for k, v in stats.items() if _is_number(v)}
    out: dict[str, float] = {}
    if is_team_defense:
        for key, stat in _DEF_STATS.items():
            if key in s:
                out[stat] = s[key]
        # nflverse's fumbles_recovered includes special-teams recoveries
        out["fumbles_recovered"] = s.get("fum_rec", 0.0) + s.get("def_st_fum_rec", 0.0)
        # Sleeper omits a zero pts_allow / yds_allow (a shutout) on a team that played
        if s.get("gp"):
            out.setdefault("points_allowed", 0.0)
            out.setdefault("total_yards_allowed", 0.0)
        return out
    for key, stat in _STAT_DIRECT.items():
        if key in s:
            out[stat] = s[key]
    over_50 = s.get("fgm_50_59", 0.0) + s.get("fgm_60p", 0.0)
    if over_50 or "fgm_50_59" in s or "fgm_60p" in s:
        out["field_goal_made_50_plus"] = over_50
    return out


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


__all__ = [
    "RuleDiff",
    "RuleRow",
    "UnmappedScoringKeyError",
    "derive_rules",
    "describe",
    "diff_rules",
    "sleeper_stats_to_engine",
    "write_season_rules",
]
