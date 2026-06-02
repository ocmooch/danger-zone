"""Canonical NFL franchise identity.

Different sources spell the same franchise differently: our ``players``
rows carry ``AZ``/``JAC``/``LAR`` while nflverse stat rows use
``ARI``/``JAX``/``LA``, and relocated franchises appear under their old code
in historical rows (``OAK`` before 2020, ``SD`` before 2017, ``STL`` before
2016). :func:`canonical_franchise` folds every known spelling — alias *and*
relocation — to one stable code per franchise so two abbreviations that name
the same team compare equal regardless of source or season.

This is deliberately spelling-only and season-agnostic: it answers "are these
two strings the same franchise?", not "what code did nflverse use that
year?". Season-aware resolution for team-DEF rollups lives in
``crawlers/nflverse/franchises.py``.
"""

from __future__ import annotations

# Alternate or historical abbreviation -> canonical current code. Covers the
# spelling variants observed across our sources plus the three franchises that
# relocated within the league era; any code not listed is its own canonical
# form (the common case).
_FRANCHISE_ALIASES: dict[str, str] = {
    "AZ": "ARI",  # Cardinals (ESPN-style)
    "JAC": "JAX",  # Jaguars
    "LAR": "LA",  # Rams (ESPN-style)
    "STL": "LA",  # Rams, pre-2016 St. Louis
    "OAK": "LV",  # Raiders, pre-2020 Oakland
    "SD": "LAC",  # Chargers, pre-2017 San Diego
    "WSH": "WAS",  # Commanders/Washington (ESPN-style)
}


def canonical_franchise(abbrev: str | None) -> str | None:
    """Fold an NFL team abbreviation to its canonical franchise code.

    Returns ``None`` for ``None``/blank input so callers can distinguish
    "unknown franchise" from a real code.
    """

    if not abbrev:
        return None
    code = abbrev.strip().upper()
    if not code:
        return None
    return _FRANCHISE_ALIASES.get(code, code)


__all__ = ["canonical_franchise"]
