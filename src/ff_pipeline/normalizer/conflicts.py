"""Cross-source precedence rules.

When two crawlers disagree about the same field on the same player /
week, the normalizer needs a deterministic tie-breaker. The matrix below
is lifted directly from ``docs/03_DATA_SOURCES.md`` — when that doc
changes, this module changes with it.

Two consumers today:

* ``normalizer.player_ids.PlayerResolver`` uses ``is_higher_precedence``
  to decide whether an incoming identity field (name, position, team)
  overwrites the existing ``players`` column.
* Future M9 verification + scoring will key off ``is_primary_for`` to
  pick which ``player_stats_raw`` row feeds the scoring engine.

The dimensions kept here intentionally don't cover *every* table on the
schema — only the ones where multiple sources can disagree about the
same value. League-only data (rosters / matchups / transactions) has
exactly one source (NFL.com), so no precedence list is needed.
"""

from __future__ import annotations

from typing import Final, Literal

# Sources known to the pipeline. Keeping this as a Literal so the type
# checker catches typos at call sites.
Source = Literal["nflverse", "nfl_com", "nfl_com_api", "sleeper"]

#: Dimensions where multiple sources can disagree.
Dimension = Literal["identity", "stats", "projections", "injury"]


# Ranked source lists per dimension. Index 0 is the primary; later entries
# are progressively lower priority. Sources not present in a list have
# *no* authority for that dimension and should be ignored.
_PRECEDENCE: Final[dict[Dimension, tuple[Source, ...]]] = {
    "identity": ("nflverse", "nfl_com", "sleeper"),
    "stats": ("nflverse", "nfl_com_api", "sleeper"),
    "projections": ("sleeper", "nfl_com_api"),
    "injury": ("sleeper", "nflverse"),
}


def precedence(dimension: Dimension) -> tuple[Source, ...]:
    """Return the ranked source tuple for ``dimension``.

    Higher-priority sources come first. Sources absent from the tuple
    have no authority over the dimension.
    """
    return _PRECEDENCE[dimension]


def priority(source: str, dimension: Dimension) -> int | None:
    """Return ``source``'s rank for ``dimension`` (0 = primary).

    Returns ``None`` if the source has no authority for the dimension —
    callers should treat that as "ignore this source's value here".
    """
    ranked = _PRECEDENCE[dimension]
    try:
        return ranked.index(source)
    except ValueError:
        return None


def is_higher_precedence(
    candidate: str,
    incumbent: str | None,
    dimension: Dimension,
) -> bool:
    """Should ``candidate``'s value replace ``incumbent``'s for ``dimension``?

    Rules:

    * If the incumbent has no recorded source (``None``), the candidate
      always wins (we're filling in a blank).
    * If the candidate has no authority for the dimension, it loses
      regardless of the incumbent.
    * If both are authoritative, the lower-ranked source wins.
    * Equal rank (same source) — return False; nothing changed.
    """
    cand_rank = priority(candidate, dimension)
    if cand_rank is None:
        return False
    if incumbent is None:
        return True
    inc_rank = priority(incumbent, dimension)
    if inc_rank is None:
        # Incumbent isn't authoritative — candidate wins by default.
        return True
    return cand_rank < inc_rank


def pick_primary(sources: list[str], dimension: Dimension) -> str | None:
    """Return whichever of ``sources`` has the highest precedence.

    Useful for "given that these N sources all have a row for this
    (player, week), which one feeds downstream?" Returns ``None`` if
    none of the inputs has any authority over the dimension.
    """
    ranked = [(priority(s, dimension), s) for s in sources]
    candidates = [(r, s) for r, s in ranked if r is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


__all__ = [
    "Dimension",
    "Source",
    "is_higher_precedence",
    "pick_primary",
    "precedence",
    "priority",
]
