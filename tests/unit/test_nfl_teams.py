"""Unit coverage for canonical NFL franchise folding."""

from __future__ import annotations

import pytest

from ff_pipeline.nfl_teams import canonical_franchise


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AZ", "ARI"),
        ("ARI", "ARI"),
        ("JAC", "JAX"),
        ("JAX", "JAX"),
        ("LAR", "LA"),
        ("STL", "LA"),
        ("LA", "LA"),
        ("OAK", "LV"),
        ("LV", "LV"),
        ("SD", "LAC"),
        ("LAC", "LAC"),
        ("WSH", "WAS"),
        ("WAS", "WAS"),
        ("KC", "KC"),  # an ordinary code passes through unchanged
        ("  kc  ", "KC"),  # case + surrounding whitespace normalized
    ],
)
def test_canonical_franchise_folds_aliases(raw: str, expected: str) -> None:
    assert canonical_franchise(raw) == expected


def test_relocated_pair_folds_to_one_code() -> None:
    # The historical and current spelling of the same franchise must agree, so
    # a player's current team matches its opponent code in older seasons.
    assert canonical_franchise("OAK") == canonical_franchise("LV")
    assert canonical_franchise("SD") == canonical_franchise("LAC")
    assert canonical_franchise("STL") == canonical_franchise("LA")


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_input_returns_none(blank: str | None) -> None:
    assert canonical_franchise(blank) is None
