"""Decided 6 + 6 brackets (the live 2026 shape, outcomes filled in) and pick
ownership over two trades (S7)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ff_pipeline.normalizer.sleeper_brackets import derive_postseason
from ff_pipeline.normalizer.sleeper_league import _traded_picks, team_key
from tests.unit.test_sleeper_normalizer import fixture_league


def _bracket(seeds: list[int], outcome: dict[int, int]) -> list[dict[str, Any]]:
    """Sleeper's 6-team shape. ``seeds`` = rosters by seed 1..6; ``outcome``:
    match number -> winning roster."""
    s1, s2, s3, s4, s5, s6 = seeds
    ms: list[dict[str, Any]] = [
        {"r": 1, "m": 1, "t1": s3, "t2": s6},
        {"r": 1, "m": 2, "t1": s4, "t2": s5},
        {"r": 2, "m": 3, "t1": s1, "t2_from": {"w": 1}},
        {"r": 2, "m": 4, "t1": s2, "t2_from": {"w": 2}},
        {"r": 2, "m": 5, "p": 5, "t1_from": {"l": 1}, "t2_from": {"l": 2}},
        {"r": 3, "m": 6, "p": 1, "t1_from": {"w": 3}, "t2_from": {"w": 4}},
        {"r": 3, "m": 7, "p": 3, "t1_from": {"l": 3}, "t2_from": {"l": 4}},
    ]
    by = {m["m"]: m for m in ms}
    for m in ms:  # resolve feeders, then decide
        for side in ("t1", "t2"):
            frm = m.get(f"{side}_from")
            if frm:
                ((kind, src),) = frm.items()
                m[side] = by[src].get(kind)
        if m["m"] in outcome and m.get("t1") and m.get("t2"):
            w = outcome[m["m"]]
            m["w"], m["l"] = w, m["t2"] if w == m["t1"] else m["t1"]
    return ms


# seeds 1-6 = rosters 1-6, the higher seed wins every game except m2 (seed 5 wins)
WIN = _bracket([1, 2, 3, 4, 5, 6], {1: 3, 2: 5, 3: 1, 4: 2, 5: 4, 6: 1, 7: 5})
LOSE = _bracket([7, 8, 9, 10, 11, 12], {1: 9, 2: 10, 3: 7, 4: 8, 5: 12, 6: 7, 7: 10})


def _scores(*brackets: list[dict[str, Any]], invert: bool = False) -> dict[tuple[int, str], float]:
    out: dict[tuple[int, str], float] = {}
    for b in brackets:
        for m in b:
            if m.get("w"):
                wk = 14 + m["r"]
                out[(wk, team_key(m["w"]))] = 90.0 if invert else 110.0
                out[(wk, team_key(m["l"]))] = 110.0 if invert else 90.0
    return out


def _derive(last_week: int = 17, **kw: Any) -> Any:
    args: dict[str, Any] = {
        "winners": WIN,
        "losers": LOSE,
        "team_key": team_key,
        "playoff_week_start": 15,
        "playoff_round_type": 0,
        "last_finalized_week": last_week,
        "scores": _scores(WIN, LOSE),
    }
    args.update(kw)
    return derive_postseason(**args)


def test_places_results_and_flags_follow_the_legacy_convention() -> None:
    p = _derive()
    assert p.warnings == []
    assert {
        team_key(r): place
        for r, place in [
            (1, 1),
            (2, 2),
            (5, 3),
            (3, 4),
            (4, 5),
            (6, 6),
            (7, 7),
            (8, 8),
            (10, 9),
            (9, 10),
            (12, 11),
            (11, 12),
        ]
    } == p.places
    assert (p.results.champion_key, p.results.runner_up_key, p.results.last_place_key) == (
        team_key(1),
        team_key(2),
        team_key(11),
    )
    assert p.made_playoffs[team_key(1)] and p.made_playoffs[team_key(6)]
    assert not p.made_playoffs[team_key(7)]
    # 3rd-place game: playoff, not consolation; lower bracket: both
    assert p.team_weeks[(17, team_key(5))] == {
        "is_playoff": True,
        "is_consolation": False,
        "role": "game",
    }
    assert p.team_weeks[(17, team_key(7))]["is_consolation"] is True
    # top seeds bye in week 15; the 5th-place game is week 16, so week 17 is "placed"
    assert p.team_weeks[(15, team_key(1))]["role"] == "bye"
    assert p.team_weeks[(15, team_key(7))]["role"] == "bye"
    assert p.team_weeks[(17, team_key(4))]["role"] == "placed"
    assert p.team_weeks[(17, team_key(6))]["is_playoff"] is False
    assert sum(1 for tw in p.team_weeks.values() if tw["role"] == "game") == 8 + 12 + 8


def test_partial_postseason_places_only_what_is_decided() -> None:
    p = _derive(last_week=16)
    assert p.results.champion_key is None
    assert p.results.last_place_key == team_key(11)  # the 11th/12th game is in week 16
    assert p.places == {team_key(4): 5, team_key(6): 6, team_key(12): 11, team_key(11): 12}


def test_inverted_bracket_writes_no_places() -> None:
    p = _derive(scores=_scores(WIN, LOSE, invert=True))
    assert p.places == {} and p.results.champion_key is None
    assert any("loser-advances" in w for w in p.warnings)
    assert p.team_weeks  # flags still derived; only places are withheld


def test_multi_week_rounds_are_refused() -> None:
    p = _derive(playoff_round_type=1)
    assert p.places == {} and p.team_weeks == {} and p.warnings


def test_pick_ownership_over_two_trades() -> None:
    def trade(tid: str, when: int, prev: int, new: int) -> dict[str, Any]:
        return {
            "type": "trade",
            "status": "complete",
            "transaction_id": tid,
            "created": when,
            "roster_ids": [prev, new],
            "draft_picks": [
                {
                    "season": "2027",
                    "round": 3,
                    "roster_id": 1,
                    "previous_owner_id": prev,
                    "owner_id": new,
                }
            ],
        }

    data = replace(
        fixture_league(),
        transactions=[trade("T1", 1000, 1, 2), trade("T2", 2000, 2, 3)],
        traded_picks=[
            {"season": "2027", "round": 3, "roster_id": 1, "previous_owner_id": 2, "owner_id": 3},
            # a pick traded away and back again is not listed as traded
            {"season": "2027", "round": 5, "roster_id": 4, "previous_owner_id": 5, "owner_id": 4},
        ],
    )
    (p,) = _traded_picks(data)
    assert (p.pick_season, p.round, p.original_key, p.current_key, p.previous_key) == (
        2027,
        3,
        team_key(1),
        team_key(3),
        team_key(2),
    )
    assert p.source_transaction_id == "T2"
