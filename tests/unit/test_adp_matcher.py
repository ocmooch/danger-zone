"""Unit tests for the pure helpers behind the ADP player matcher."""

from __future__ import annotations

import pytest

from ff_pipeline.crawlers.adp.matcher import fold_position, normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Antonio Brown", "antonio brown"),
        ("  Odell  Beckham  Jr. ", "odell beckham"),  # suffix + whitespace dropped
        ("Brown, Antonio", "antonio brown"),  # MFL "Last, First"
        ("D'Andre Swift", "dandre swift"),  # punctuation stripped
        ("José Reyes", "jose reyes"),  # accents folded
        ("Robert Griffin III", "robert griffin"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_name(raw: str | None, expected: str | None) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WR", "WR"),
        ("PK", "K"),  # placekicker → K
        ("FB", "RB"),  # fullback → RB
        ("DST", "DEF"),
        ("D/ST", "DEF"),
        ("LB", None),  # no fantasy home
        (None, None),
    ],
)
def test_fold_position(raw: str | None, expected: str | None) -> None:
    assert fold_position(raw) == expected
