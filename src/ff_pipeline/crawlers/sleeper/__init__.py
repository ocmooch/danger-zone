"""Sleeper crawler package.

Public entry points:

* ``SleeperClient`` — typed wrapper returning dataclasses
* ``LiveSleeperSource`` / ``LocalFixtureSource`` — HTTP source seam
* ``run_sleeper`` — high-level "pull projections + trending" pipeline
"""

from __future__ import annotations

from ff_pipeline.crawlers.sleeper.client import (
    BASE_APP,
    BASE_COM,
    LiveSleeperSource,
    LocalFixtureSource,
    SleeperClientError,
    SleeperSource,
    TransientHTTPError,
)
from ff_pipeline.crawlers.sleeper.endpoints import (
    SleeperClient,
    SleeperPlayer,
    SleeperProjection,
    SleeperTrend,
)
from ff_pipeline.crawlers.sleeper.runner import SleeperRunResult, run_sleeper

__all__ = [
    "BASE_APP",
    "BASE_COM",
    "LiveSleeperSource",
    "LocalFixtureSource",
    "SleeperClient",
    "SleeperClientError",
    "SleeperPlayer",
    "SleeperProjection",
    "SleeperRunResult",
    "SleeperSource",
    "SleeperTrend",
    "TransientHTTPError",
    "run_sleeper",
]
