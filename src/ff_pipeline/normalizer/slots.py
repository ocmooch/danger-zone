"""Sleeper roster slot labels → the league's canonical ``roster_slot`` vocabulary (D-9).

Stored slots reuse the legacy NFL.com vocabulary (``QB RB WR TE K DEF R/W/T W/R BN
RES``) and add labels only for slot types NFL.com never had (``W/T``, ``Q/R/W/T``,
``TAXI``, IDP). The raw Sleeper label is kept separately (``extra_data.platform_slot``,
applied by the S5 loader). Unknown labels raise: a new slot type must be added here
on purpose, never silently stored as bench (S4-D6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# label -> (stored roster_slot, is_starter)
SLOT_MAP: dict[str, tuple[str, bool]] = {
    "QB": ("QB", True),
    "RB": ("RB", True),
    "WR": ("WR", True),
    "TE": ("TE", True),
    "K": ("K", True),
    "DEF": ("DEF", True),
    "FLEX": ("R/W/T", True),
    "WRRB_FLEX": ("W/R", True),
    "REC_FLEX": ("W/T", True),
    "SUPER_FLEX": ("Q/R/W/T", True),
    "IDP_FLEX": ("IDP_FLEX", True),
    "DL": ("DL", True),
    "LB": ("LB", True),
    "DB": ("DB", True),
    "BN": ("BN", False),
}

# Not ``roster_positions`` labels: where a player sits when listed in a roster's
# ``reserve`` / ``taxi`` arrays.
RESERVE = ("RES", False)
TAXI = ("TAXI", False)


class UnknownSlotError(ValueError):
    """A Sleeper slot label this module does not know."""


@dataclass(frozen=True, slots=True)
class Slot:
    roster_slot: str
    is_starter: bool
    platform_slot: str  # raw Sleeper label (or "reserve" / "taxi")


def map_slot(label: str) -> Slot:
    """Map one ``roster_positions`` label."""
    try:
        roster_slot, is_starter = SLOT_MAP[label]
    except KeyError:
        raise UnknownSlotError(
            f"unknown Sleeper slot label {label!r}; add it to normalizer/slots.py (D-9)"
        ) from None
    return Slot(roster_slot, is_starter, label)


def reserve_slot() -> Slot:
    return Slot(*RESERVE, "reserve")


def taxi_slot() -> Slot:
    return Slot(*TAXI, "taxi")


def starter_slots(roster_positions: Sequence[str]) -> list[Slot]:
    """The ordered starting slots that a matchup's ``starters[]`` aligns to.

    Sleeper lists ``starters`` in ``roster_positions`` order with bench labels
    left out; every label is validated, including the bench ones.
    """
    slots = [map_slot(label) for label in roster_positions]
    return [s for s in slots if s.is_starter]


__all__ = [
    "SLOT_MAP",
    "Slot",
    "UnknownSlotError",
    "map_slot",
    "reserve_slot",
    "starter_slots",
    "taxi_slot",
]
