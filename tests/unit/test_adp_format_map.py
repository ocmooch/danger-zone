"""Unit tests for the per-season ADP format selection + fallback chain."""

from __future__ import annotations

from ff_pipeline.crawlers.adp.format_map import (
    FULL_PPR,
    HALF_PPR,
    STANDARD,
    fallback_chain,
    requested_format_for_year,
)


def test_2010_is_half_ppr() -> None:
    assert requested_format_for_year(2010) == HALF_PPR


def test_2011_onward_is_full_ppr() -> None:
    assert requested_format_for_year(2011) == FULL_PPR
    assert requested_format_for_year(2015) == FULL_PPR
    assert requested_format_for_year(2025) == FULL_PPR


def test_full_ppr_chain_prefers_half_then_standard() -> None:
    assert fallback_chain(FULL_PPR) == (FULL_PPR, HALF_PPR, STANDARD)


def test_half_ppr_chain_prefers_full_then_standard() -> None:
    # 2010's target; if half is unavailable fall to full, then (emergency) standard.
    assert fallback_chain(HALF_PPR) == (HALF_PPR, FULL_PPR, STANDARD)


def test_standard_always_trails_as_emergency() -> None:
    for requested in (FULL_PPR, HALF_PPR):
        assert fallback_chain(requested)[-1] == STANDARD
