"""nflverse crawler package.

Public entry points:

* ``NflverseClient`` — high-level reader returning dataclasses
* ``LiveNflverseSource`` — production source (talks to nflreadpy)
* ``LocalParquetSource`` — test source (reads from a fixtures directory)
"""

from __future__ import annotations

from ff_pipeline.crawlers.nflverse.client import (
    LiveNflverseSource,
    LocalParquetSource,
    NflverseClient,
    NflversePlayerMeta,
    NflversePlayerStat,
    NflverseSource,
)

__all__ = [
    "LiveNflverseSource",
    "LocalParquetSource",
    "NflverseClient",
    "NflversePlayerMeta",
    "NflversePlayerStat",
    "NflverseSource",
]
