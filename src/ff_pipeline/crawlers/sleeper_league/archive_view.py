"""Read the newest archived payloads of one Sleeper league season (offline, S4).

Used by the identity commands (``owners map``, ``sleeper-league
resolve-players``) and later by the S5 loader: everything comes from the raw
archive, never from the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ff_pipeline.crawlers.sleeper_league.client import (
    ArchiveMissError,
    ArchiveSleeperLeagueSource,
)
from ff_pipeline.crawlers.sleeper_league.endpoints import (
    Endpoint,
    PayloadRequest,
    draft_ids,
    draft_requests,
    drafts_request,
    league_request,
    league_requests,
    players_request,
    state_request,
    weeks_to_capture,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore


@dataclass(slots=True)
class ArchivedLeague:
    league_id: str
    season: int
    weeks: int
    league: dict[str, Any]
    users: list[dict[str, Any]] = field(default_factory=list)
    rosters: list[dict[str, Any]] = field(default_factory=list)
    matchups: list[dict[str, Any]] = field(default_factory=list)  # all weeks, flattened
    transactions: list[dict[str, Any]] = field(default_factory=list)
    draft_picks: list[dict[str, Any]] = field(default_factory=list)
    players: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_archived_league(session: Session, store: RawStore, league_id: str) -> ArchivedLeague:
    """Newest revision of every archived payload for ``league_id``.

    Raises ``ArchiveMissError`` when the league itself was never captured.
    """
    src = ArchiveSleeperLeagueSource(session, store)

    def j(req: PayloadRequest) -> Any:
        try:
            return src.get(req).json()
        except ArchiveMissError:
            return None

    league = j(league_request(league_id))
    if not isinstance(league, dict):
        raise ArchiveMissError(f"league {league_id} is not in the archive; run capture first")
    season = int(league["season"])
    state = j(state_request())
    weeks = weeks_to_capture(state if isinstance(state, dict) else {}, league)
    by = {(r.endpoint, r.week): r for r in league_requests(league_id, season, weeks)}

    def lst(req: PayloadRequest) -> list[dict[str, Any]]:
        got = j(req)
        return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []

    picks: list[dict[str, Any]] = []
    for did in draft_ids(league, j(drafts_request(league_id, season))):
        picks.extend(lst(draft_requests(league_id, season, did)[1]))
    players = j(players_request())
    return ArchivedLeague(
        league_id=league_id,
        season=season,
        weeks=weeks,
        league=league,
        users=lst(by[(Endpoint.USERS, None)]),
        rosters=lst(by[(Endpoint.ROSTERS, None)]),
        matchups=[m for w in range(1, weeks + 1) for m in lst(by[(Endpoint.MATCHUPS, w)])],
        transactions=[t for w in range(1, weeks + 1) for t in lst(by[(Endpoint.TRANSACTIONS, w)])],
        draft_picks=picks,
        players=players if isinstance(players, dict) else {},
    )


__all__ = ["ArchivedLeague", "load_archived_league"]
