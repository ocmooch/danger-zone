"""Deterministic, idempotent data overrides applied after normalize+score.

An override encodes a league ruling or a known upstream data defect that a
fresh scrape would otherwise revert. Each override is a pure function of the
DB state that re-applies cleanly on every ingest, mirroring the relocation/DST
resolver precedent (``crawlers/nflverse/franchises.py``).
"""

from __future__ import annotations

from ff_pipeline.overrides.hamlin_2022_wk17 import (
    HamlinOverrideResult,
    apply_hamlin_2022_wk17_override,
)

__all__ = ["HamlinOverrideResult", "apply_hamlin_2022_wk17_override"]
