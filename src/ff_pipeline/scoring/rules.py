"""Scoring-rule data structures.

Rules are data, not code: each ``ScoringRule`` describes how a single
stat key contributes to a fantasy point total. A ``ScoringRules``
bundles every rule that applies for one season. The engine in
``scoring.engine`` consumes these — see ``docs/05_SCORING_ENGINE.md``.

The dataclasses are frozen so a rules object can be safely cached and
shared across threads; the engine never mutates them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ScoringRule:
    """One scoring line as stored in a league's settings.

    ``flat_points`` and ``points_per_unit`` are mutually exclusive in
    practice — a rule is *either* an all-or-nothing bonus (e.g. "+3 at
    300 passing yards") *or* a per-unit accrual (e.g. "1 point per 25
    passing yards"). When ``flat_points`` is set, the engine ignores
    ``points_per_unit`` / ``unit_size``.

    Thresholds gate the rule:

    * ``threshold_min`` — required minimum stat value for the rule to
      contribute. For flat bonuses this is the trigger ("300+"); for
      per-unit rules it shifts the accrual window so only the portion
      *above* the threshold counts.
    * ``threshold_max`` — upper bound; for points-allowed brackets this
      is the inclusive top of the bracket ("7-13" → max=13).
    """

    category: str
    stat_key: str
    points_per_unit: float = 0.0
    unit_size: float = 1.0
    threshold_min: float | None = None
    threshold_max: float | None = None
    flat_points: float | None = None


@dataclass(frozen=True, slots=True)
class ScoringRules:
    """The full set of rules for one season.

    ``season_id`` is the FK back to ``seasons.season_id``; ``rules`` is
    an immutable tuple so the whole object hashes and can be cached.
    """

    season_id: int
    rules: tuple[ScoringRule, ...] = field(default_factory=tuple)


__all__ = ["ScoringRule", "ScoringRules"]
