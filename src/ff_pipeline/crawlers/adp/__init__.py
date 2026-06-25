"""Average Draft Position (ADP) ingestion.

Pulls consensus ADP from public sources (Fantasy Football Calculator, MyFantasy
League, and — deferred — Sleeper), resolves each source player to a canonical
``players.player_id``, and stores raw per-source rows in ``player_adp``. The
weighted multi-source blend and the reach/value delta are computed downstream in
the dashboard, so the weighting stays tunable without re-ingesting.

Mirrors the Sleeper crawler's structure: a thin HTTP/source seam, a typed entry
shape, and a runner that does ``pipeline_runs`` / ``source_health`` bookkeeping
and counts unresolved rows.
"""

from ff_pipeline.crawlers.adp.endpoints import AdpEntry, AdpSource, LocalFixtureAdpSource
from ff_pipeline.crawlers.adp.runner import AdpRunResult, run_adp

__all__ = [
    "AdpEntry",
    "AdpRunResult",
    "AdpSource",
    "LocalFixtureAdpSource",
    "run_adp",
]
