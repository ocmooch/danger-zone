"""Per-season ADP format selection + the loud fallback chain.

This league has used PPR scoring throughout, with one wrinkle: **2010 was
half-PPR; every season from 2011 on is full-PPR**. ADP is format-specific, so we
pull the format that matches the season. No public source's scoring will match
this league's custom rules exactly — ADP is "closest public market", not this
league's own valuations — but matching the reception format keeps reach/value
honest.

When a source can't serve the requested format for a year, we walk a fallback
chain (closest format first), and the substitution is recorded on every row
(``format_fallback=True``) so it surfaces downstream rather than silently
degrading the signal. **Standard (0-PPR) is an emergency fallback only.**
"""

from __future__ import annotations

FULL_PPR = "full_ppr"
HALF_PPR = "half_ppr"
STANDARD = "standard"

#: The season where the league used half-PPR; everything else is full-PPR.
_HALF_PPR_YEARS = frozenset({2010})


def requested_format_for_year(year: int) -> str:
    """The league's target ADP format for a season (half-PPR in 2010, else full)."""
    return HALF_PPR if year in _HALF_PPR_YEARS else FULL_PPR


def fallback_chain(requested: str) -> tuple[str, ...]:
    """Ordered formats to try for ``requested`` — closest first, standard last.

    Full and half PPR are each other's nearest neighbour; standard always trails
    as the emergency option. The first format a source actually serves wins, and
    anything past index 0 is a (loud) fallback.
    """
    if requested == HALF_PPR:
        return (HALF_PPR, FULL_PPR, STANDARD)
    if requested == STANDARD:
        return (STANDARD, HALF_PPR, FULL_PPR)
    return (FULL_PPR, HALF_PPR, STANDARD)


__all__ = [
    "FULL_PPR",
    "HALF_PPR",
    "STANDARD",
    "fallback_chain",
    "requested_format_for_year",
]
