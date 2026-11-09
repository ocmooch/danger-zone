"""Walk a Sleeper league's ``previous_league_id`` chain (S2).

Sleeper creates a new ``league_id`` for every season on renewal and links
it back via ``previous_league_id``. Starting from the newest league id
(``SLEEPER_LEAGUE_ID``), the walk yields one link per season, newest first.
``previous_league_id`` of ``"0"``, empty or missing ends the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_DEPTH = 30


class LineageError(RuntimeError):
    """The chain is broken, cyclic, too deep, or lacks the requested season."""


@dataclass(frozen=True, slots=True)
class LeagueLink:
    league_id: str
    season: int | None
    previous_league_id: str | None
    name: str | None
    status: str | None


def _prev_id(league: dict[str, Any]) -> str | None:
    prev = league.get("previous_league_id")
    if prev is None:
        return None
    prev = str(prev).strip()
    return None if prev in {"", "0"} else prev


def _season(league: dict[str, Any]) -> int | None:
    try:
        return int(league["season"])
    except (KeyError, TypeError, ValueError):
        return None


def walk_lineage(
    fetch_league: Callable[[str], dict[str, Any] | None],
    start_league_id: str,
    *,
    max_depth: int = MAX_DEPTH,
) -> list[LeagueLink]:
    """Newest-first chain of leagues reachable from ``start_league_id``.

    ``fetch_league`` returns the decoded ``/league/{id}`` payload, or None
    when Sleeper has no such league.
    """
    chain: list[LeagueLink] = []
    seen: set[str] = set()
    league_id: str | None = start_league_id
    while league_id is not None:
        if league_id in seen:
            raise LineageError(f"previous_league_id cycle at league {league_id}")
        if len(chain) >= max_depth:
            raise LineageError(f"lineage deeper than {max_depth} seasons; stopping")
        seen.add(league_id)
        league = fetch_league(league_id)
        if not isinstance(league, dict):
            raise LineageError(f"league {league_id} not found on Sleeper")
        prev = _prev_id(league)
        chain.append(
            LeagueLink(
                league_id=league_id,
                season=_season(league),
                previous_league_id=prev,
                name=str(league["name"]) if league.get("name") is not None else None,
                status=str(league["status"]) if league.get("status") is not None else None,
            )
        )
        league_id = prev
    return chain


def league_for_season(chain: list[LeagueLink], season: int) -> LeagueLink:
    for link in chain:
        if link.season == season:
            return link
    have = ", ".join(str(link.season) for link in chain) or "none"
    raise LineageError(f"no Sleeper league for season {season} in the lineage (have: {have})")


__all__ = ["MAX_DEPTH", "LeagueLink", "LineageError", "league_for_season", "walk_lineage"]
