"""Sleeper brackets → bracket rows, postseason flags, places, season results (S7).

Pure. Follows the legacy (NFL.com 2010-2025) convention so the dashboard reads
2026 the same way (S7-D2):

* every bracket game is ``is_playoff``; losers-bracket games are also
  ``is_consolation`` (the winners bracket's 3rd/5th-place games are not);
* places: winners bracket 1..n, losers bracket n+1..2n. A decided match with
  placement ``p`` gives its winner ``offset + p`` and its loser ``offset + p + 1``;
* champion / runner-up = the winners ``p=1`` match; last place (the Sacko) = the
  highest place number.

Sleeper's ``w``/``l`` are trusted only after a cross-check against the week's
finalized scores (S7-D3): if any decided game's winner scored less, the
bracket may run "loser advances" (a toilet bowl) and no places are derived.
Team-weeks in the postseason window with no bracket game are ``bye`` (the team
plays later) or ``placed`` (its season is over) (S7-D4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ff_pipeline.canonical.dto import BracketMatchDTO, SeasonResultsDTO

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

BRACKETS = ("winners", "losers")


@dataclass(slots=True)
class Postseason:
    matches: list[BracketMatchDTO] = field(default_factory=list)
    # (week, team_key) -> {"is_playoff", "is_consolation", "role"}
    team_weeks: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    places: dict[str, int] = field(default_factory=dict)
    made_playoffs: dict[str, bool] = field(default_factory=dict)
    results: SeasonResultsDTO = field(default_factory=SeasonResultsDTO)
    warnings: list[str] = field(default_factory=list)


def derive_postseason(
    *,
    winners: list[dict[str, Any]],
    losers: list[dict[str, Any]],
    team_key: Callable[[Any], str],
    playoff_week_start: int | None,
    playoff_round_type: int | None,
    last_finalized_week: int,
    scores: Mapping[tuple[int, str], float | None],
) -> Postseason:
    """``scores`` = finalized team scores by (week, team_key)."""
    out = Postseason()
    raw = {"winners": winners, "losers": losers}
    for bracket in BRACKETS:
        for raw_m in raw[bracket]:
            r, n = _int(raw_m.get("r")), _int(raw_m.get("m"))
            if r is None or n is None:
                continue
            out.matches.append(
                BracketMatchDTO(
                    bracket=bracket,
                    round=r,
                    match_no=n,
                    team1_key=_key(raw_m.get("t1"), team_key),
                    team2_key=_key(raw_m.get("t2"), team_key),
                    winner_key=_key(raw_m.get("w"), team_key),
                    loser_key=_key(raw_m.get("l"), team_key),
                    placement=_int(raw_m.get("p")),
                    team1_from=raw_m.get("t1_from"),
                    team2_from=raw_m.get("t2_from"),
                )
            )
    if not out.matches or playoff_week_start is None:
        return out
    if playoff_round_type not in (None, 0):
        out.warnings.append(
            f"playoff_round_type={playoff_round_type}: multi-week rounds are not mapped; "
            "postseason flags and places not derived"
        )
        return out

    def week_of(r: int) -> int:
        return playoff_week_start + r - 1

    # bracket membership: made_playoffs = seeded into the winners bracket. Seeds
    # follow the standings until the regular season is final, so only then.
    if last_finalized_week >= playoff_week_start - 1:
        for m in out.matches:
            for k in (m.team1_key, m.team2_key):
                if k is not None:
                    out.made_playoffs[k] = out.made_playoffs.get(k, False) or (
                        m.bracket == "winners"
                    )

    # direction check (S7-D3) on finalized decided games
    inverted: list[str] = []
    decided = [m for m in out.matches if m.winner_key and m.loser_key]
    for m in decided:
        wk = week_of(m.round)
        if wk > last_finalized_week:
            continue
        ws, ls = scores.get((wk, str(m.winner_key))), scores.get((wk, str(m.loser_key)))
        if ws is not None and ls is not None and ws < ls:
            inverted.append(f"{m.bracket} r{m.round} m{m.match_no}")
    if inverted:
        out.warnings.append(
            "bracket winner scored less than loser in "
            + ", ".join(inverted)
            + " (loser-advances bracket?); places not derived"
        )

    # team-week flags
    last_round = max(m.round for m in out.matches)
    in_round: dict[tuple[int, str], BracketMatchDTO] = {}
    for m in out.matches:
        for k in (m.team1_key, m.team2_key):
            if k is not None:
                in_round[(m.round, k)] = m
    teams = {k for (_r, k) in in_round}
    for r in range(1, last_round + 1):
        wk = week_of(r)
        for k in teams:
            game = in_round.get((r, k))
            if game is not None:
                out.team_weeks[(wk, k)] = {
                    "is_playoff": True,
                    "is_consolation": game.bracket == "losers",
                    "role": "game",
                }
            elif any((later, k) in in_round for later in range(r + 1, last_round + 1)):
                out.team_weeks[(wk, k)] = {
                    "is_playoff": False,
                    "is_consolation": False,
                    "role": "bye",
                }
            elif any((earlier, k) in in_round for earlier in range(1, r)):
                out.team_weeks[(wk, k)] = {
                    "is_playoff": False,
                    "is_consolation": False,
                    "role": "placed",
                }

    if inverted:
        return out
    size = {
        b: len({k for m in out.matches if m.bracket == b for k in (m.team1_key, m.team2_key) if k})
        for b in BRACKETS
    }
    offset = {"winners": 0, "losers": size["winners"]}
    for m in decided:
        if m.placement is None or week_of(m.round) > last_finalized_week:
            continue
        base = offset[m.bracket] + m.placement
        out.places[str(m.winner_key)] = base
        out.places[str(m.loser_key)] = base + 1
    final = next(
        (
            m
            for m in decided
            if m.bracket == "winners"
            and m.placement == 1
            and week_of(m.round) <= last_finalized_week
        ),
        None,
    )
    total = size["winners"] + size["losers"]
    last = next((k for k, p in out.places.items() if p == total), None)
    out.results = SeasonResultsDTO(
        champion_key=final.winner_key if final else None,
        runner_up_key=final.loser_key if final else None,
        last_place_key=last,
    )
    return out


def _key(v: Any, team_key: Callable[[Any], str]) -> str | None:
    return team_key(v) if v is not None else None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["Postseason", "derive_postseason"]
