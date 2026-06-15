"""The scoring engine.

A single pure function — ``apply_rules(stats, rules)`` — turns a raw
stat dict (e.g. ``{"passing_yards": 312, "passing_tds": 2}``) into a
``ScoredResult`` (total + per-category breakdown). No I/O, no globals;
every input is explicit and every output is auditable.

See ``docs/05_SCORING_ENGINE.md`` for the full set of stat keys and the
verification procedure that gates this engine against NFL.com box
scores.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ff_pipeline.scoring.rules import ScoringRule, ScoringRules

log = get_logger(__name__)

# Totals are rounded to the cent so floating-point drift never shows up
# in user-facing scores. NFL.com reports to two decimals.
_POINTS_PRECISION = 2
_TEAM_DEFENSE_STAT_KEYS = frozenset(
    {
        "sacks",
        "interceptions",
        "fumbles_recovered",
        "safeties",
        "defensive_tds",
        "blocked_kicks",
        "points_allowed",
        "total_yards_allowed",
    }
)
_TEAM_DEFENSE_DUPLICATE_KEYS = frozenset({"special_teams_tds"})


@dataclass(frozen=True, slots=True)
class ScoredResult:
    """The output of ``apply_rules``.

    ``total_points`` is the rounded sum of ``breakdown`` values.
    ``breakdown`` maps each rule ``category`` to its contribution.
    ``unmapped_stats`` lists stat keys that had a non-zero value but no
    matching rule — zero-value unmapped stats are omitted as they cannot
    affect scoring. Callers can surface non-zero entries as data-quality
    alerts.
    ``absent_per_unit_stat_keys`` lists per-unit rule stat keys that were
    absent from the input stats dict (and therefore silently scored as 0).
    Unlike ``unmapped_stats`` (data → no rule), these are rules → no data:
    the scoring engine couldn't apply them because the source didn't supply
    the value.  A key appearing here does not mean the actual stat was zero —
    it means the source never provided it.  Callers that track known-missing
    sources (e.g. nflverse long-TD bonuses deferred to M7) can intersect this
    set with their known-gap list to surface accurate data-gap indicators.
    """

    total_points: float
    breakdown: dict[str, float]
    unmapped_stats: tuple[str, ...] = ()
    absent_per_unit_stat_keys: tuple[str, ...] = ()


def apply_rules(stats: Mapping[str, float], rules: ScoringRules) -> ScoredResult:
    """Apply ``rules`` to ``stats`` and return the scored result.

    Missing keys default to zero. Flat-bonus rules trigger when the
    stat falls inside ``[threshold_min, threshold_max]`` (either bound
    may be unset). Per-unit rules accrue
    ``(stat / unit_size) * points_per_unit``; if ``threshold_min`` is
    set, only the portion above the threshold accrues, capped by
    ``threshold_max`` if present.
    """

    breakdown: defaultdict[str, float] = defaultdict(float)
    is_team_defense = _is_team_defense_stats(stats)
    duplicate_context_keys = _duplicate_context_keys(rules)

    for rule in rules.rules:
        if _skip_duplicate_context_rule(stats, rule, is_team_defense, duplicate_context_keys):
            continue
        contribution = _score_rule(stats, rule)
        if contribution != 0.0:
            breakdown[rule.category] += contribution

    unmapped = _detect_unmapped_stats(stats, rules)
    for stat_key in unmapped:
        log.warning(
            "Unmapped stat in scoring",
            stat_key=stat_key,
            value=stats[stat_key],
            season_id=rules.season_id,
        )

    absent_per_unit = _detect_absent_per_unit_stats(stats, rules)

    total = round(sum(breakdown.values()), _POINTS_PRECISION)
    rounded_breakdown = {k: round(v, _POINTS_PRECISION) for k, v in breakdown.items()}
    return ScoredResult(
        total_points=total,
        breakdown=rounded_breakdown,
        unmapped_stats=unmapped,
        absent_per_unit_stat_keys=absent_per_unit,
    )


def _score_rule(stats: Mapping[str, float], rule: ScoringRule) -> float:
    if rule.flat_points is not None:
        # Flat bonuses require the stat to be explicitly reported — a
        # missing key means "this stat doesn't apply to this player"
        # (e.g. a QB has no points_allowed entry), and an absent stat
        # must not trigger a bonus.
        if rule.stat_key not in stats:
            return 0.0
        stat_value = stats[rule.stat_key]
        if rule.threshold_min is not None and stat_value < rule.threshold_min:
            return 0.0
        if rule.threshold_max is not None and stat_value > rule.threshold_max:
            return 0.0
        return rule.flat_points

    if rule.unit_size == 0:
        # A zero unit_size is a misconfigured rule (would divide by zero).
        # Skip it loudly rather than silently producing inf/NaN.
        log.warning(
            "Scoring rule has zero unit_size; skipping",
            stat_key=rule.stat_key,
            category=rule.category,
        )
        return 0.0

    stat_value = stats.get(rule.stat_key, 0)
    effective_value = float(stat_value)
    # threshold_min > 0 means a real lower-bound rule ("only yards above
    # 100 count"); clip negative remainders to zero. threshold_min == 0 (or
    # None) means "no floor" — negative stat values still accrue negative
    # points, which is correct for rushing/receiving yards where carries
    # for a loss DO subtract fantasy points (NFL.com awards -0.3 for a
    # -3 yard carry under a "1 pt / 10 yds" rule).
    if rule.threshold_min is not None and rule.threshold_min > 0:
        effective_value = max(0.0, effective_value - rule.threshold_min)
    if rule.threshold_max is not None:
        cap = rule.threshold_max - (rule.threshold_min or 0.0)
        effective_value = min(effective_value, cap)

    return (effective_value / rule.unit_size) * rule.points_per_unit


def _is_team_defense_stats(stats: Mapping[str, float]) -> bool:
    return any(key in stats for key in _TEAM_DEFENSE_STAT_KEYS)


def _duplicate_context_keys(rules: ScoringRules) -> frozenset[str]:
    categories_by_key: defaultdict[str, set[str]] = defaultdict(set)
    for rule in rules.rules:
        if rule.stat_key in _TEAM_DEFENSE_DUPLICATE_KEYS:
            categories_by_key[rule.stat_key].add(rule.category)
    return frozenset(
        key
        for key, categories in categories_by_key.items()
        if "defense" in categories and len(categories) > 1
    )


def _skip_duplicate_context_rule(
    stats: Mapping[str, float],
    rule: ScoringRule,
    is_team_defense: bool,
    duplicate_context_keys: frozenset[str],
) -> bool:
    """Avoid applying the wrong-context return-TD rule.

    NFL.com exposes "Kickoff and Punt Return Touchdowns" in both individual
    misc scoring and D/ST scoring. Both scrape to ``special_teams_tds`` because
    nflverse uses that stat name for both contexts. When both rules exist, the
    raw stat row decides which category consumes the shared key: D/ST rows carry
    defense-only keys such as ``points_allowed`` and ``sacks``; individual rows
    do not.
    """
    if rule.stat_key not in duplicate_context_keys or stats.get(rule.stat_key, 0) == 0:
        return False
    if is_team_defense:
        return rule.category != "defense"
    return rule.category == "defense"


def _detect_unmapped_stats(stats: Mapping[str, float], rules: ScoringRules) -> tuple[str, ...]:
    known = {rule.stat_key for rule in rules.rules}
    # Only surface unmapped stats that have a non-zero value — a zero-value
    # unmapped stat cannot affect scoring and warning on it is pure noise.
    return tuple(sorted(key for key in stats if key not in known and stats[key] != 0.0))


def _detect_absent_per_unit_stats(
    stats: Mapping[str, float], rules: ScoringRules
) -> tuple[str, ...]:
    """Per-unit rule stat keys that are absent from the input stats dict.

    Flat-bonus rules (``flat_points is not None``) are intentionally excluded:
    the engine already treats absent flat-bonus keys as "doesn't apply to this
    player type" (e.g. a WR has no ``points_allowed`` entry).  Per-unit rules
    are different — their absent keys default to 0 silently, and for stat keys
    that a data source *should* supply but doesn't (e.g. long-TD bonus counts
    that nflverse defers to play-by-play), that silent default understates the
    score without any indication that something is missing.
    """
    return tuple(
        sorted(
            {
                rule.stat_key
                for rule in rules.rules
                if rule.flat_points is None and rule.stat_key not in stats
            }
        )
    )


__all__ = ["ScoredResult", "apply_rules"]
