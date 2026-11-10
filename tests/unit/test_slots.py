"""Sleeper slot taxonomy (D-9, S4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ff_pipeline.normalizer.slots import (
    UnknownSlotError,
    map_slot,
    reserve_slot,
    starter_slots,
    taxi_slot,
)

FIXTURE_LEAGUE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "sleeper_league"
    / "season_2026"
    / "v1_league_900000000000000001.json"
)


@pytest.mark.parametrize(
    ("label", "slot", "starter"),
    [
        ("QB", "QB", True),
        ("RB", "RB", True),
        ("WR", "WR", True),
        ("TE", "TE", True),
        ("K", "K", True),
        ("DEF", "DEF", True),
        ("FLEX", "R/W/T", True),
        ("WRRB_FLEX", "W/R", True),
        ("REC_FLEX", "W/T", True),
        ("SUPER_FLEX", "Q/R/W/T", True),
        ("IDP_FLEX", "IDP_FLEX", True),
        ("DL", "DL", True),
        ("LB", "LB", True),
        ("DB", "DB", True),
        ("BN", "BN", False),
    ],
)
def test_map_slot(label: str, slot: str, starter: bool) -> None:
    s = map_slot(label)
    assert (s.roster_slot, s.is_starter, s.platform_slot) == (slot, starter, label)


def test_reserve_and_taxi() -> None:
    assert (reserve_slot().roster_slot, reserve_slot().is_starter) == ("RES", False)
    assert (taxi_slot().roster_slot, taxi_slot().is_starter) == ("TAXI", False)


def test_unknown_label_fails_loudly() -> None:
    with pytest.raises(UnknownSlotError, match="K_FLEX"):
        map_slot("K_FLEX")
    with pytest.raises(UnknownSlotError):
        starter_slots(["QB", "BN", "MYSTERY"])


def test_starter_slots_for_the_2026_league() -> None:
    positions = json.loads(FIXTURE_LEAGUE.read_text())["roster_positions"]
    assert [s.roster_slot for s in starter_slots(positions)] == [
        "QB",
        "RB",
        "RB",
        "WR",
        "WR",
        "TE",
        "R/W/T",
        "K",
        "DEF",
    ]
