"""Scoring engine — pure functions over rule data.

See ``docs/05_SCORING_ENGINE.md`` for the design rationale; the entry
point is ``apply_rules``.
"""

from ff_pipeline.scoring.engine import ScoredResult, apply_rules
from ff_pipeline.scoring.rules import ScoringRule, ScoringRules

__all__ = ["ScoredResult", "ScoringRule", "ScoringRules", "apply_rules"]
