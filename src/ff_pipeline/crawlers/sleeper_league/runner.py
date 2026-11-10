"""Sleeper season load: archived payloads → identity maps → DTOs → canonical tables (S5).

``load_league_season`` is what ``ff-pipeline sleeper-league load`` runs. It
reads only the archive (or a fixture source in tests), never the network.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ff_pipeline.canonical.loader import LoadResult, load_season
from ff_pipeline.crawlers.sleeper_league.archive_view import ArchivedLeague, load_league
from ff_pipeline.crawlers.sleeper_league.endpoints import SOURCE, Endpoint
from ff_pipeline.normalizer.sleeper_league import IdentityMaps, normalize
from ff_pipeline.normalizer.sleeper_players import league_referenced_ids, resolve
from ff_pipeline.repository.models import (
    Owner,
    OwnerPlatformIdentity,
    Player,
    Season,
    SourcePayload,
    Team,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.client import SleeperLeagueSource

FRANCHISE_LEAGUE_ID = "36271"  # D-4: the permanent league key
STAMPABLE_METHODS = frozenset({"def_franchise", "def_name"})


@dataclass(slots=True)
class PlayerPrep:
    stamped: list[str] = field(default_factory=list)  # sleeper ids stamped on DEF rows
    stubbed: list[str] = field(default_factory=list)  # sleeper ids given a new player row


def build_identity_maps(
    session: Session,
    data: ArchivedLeague,
    *,
    league_id: str = FRANCHISE_LEAGUE_ID,
    reserve_by_week: dict[int, dict[int, set[str]]] | None = None,
) -> tuple[IdentityMaps, PlayerPrep]:
    """Owners from the S4 crosswalk; players resolved, DEF rows stamped, stubs created."""
    owner_by_user = {
        i.external_user_id: i.owner_id
        for i in session.scalars(
            select(OwnerPlatformIdentity).where(OwnerPlatformIdentity.platform == "sleeper")
        )
    }
    owner_names: dict[int, str] = {}
    owner_abbrev: dict[int, str | None] = {}
    rows = session.execute(
        select(Team.owner_id, Team.team_abbrev, Season.year)
        .join(Season, Season.season_id == Team.season_id)
        .where(Season.league_id == league_id, Season.year < data.season)
        .order_by(Season.year)
    ).all()
    for owner_id, abbrev, _year in rows:
        owner_abbrev[int(owner_id)] = abbrev  # ordered by year: latest wins
    for o in session.scalars(select(Owner)):
        if o.display_name:
            owner_names[o.owner_id] = o.display_name

    prep = PlayerPrep()
    referenced = league_referenced_ids(
        data.rosters, data.matchups, data.transactions, data.draft_picks
    )
    res = resolve(session, referenced, data.players, season=data.season)
    player_ids = dict(res.resolved)
    taken = set(session.scalars(select(Player.sleeper_id).where(Player.sleeper_id.is_not(None))))
    for sid, method in res.methods.items():
        if method in STAMPABLE_METHODS and sid not in taken:
            player = session.get(Player, player_ids[sid])
            if player is not None and player.sleeper_id is None:
                player.sleeper_id = sid
                taken.add(sid)
                prep.stamped.append(sid)
    for cand in res.stub_needed:  # league-referenced only (resolve() guarantees it)
        info = data.players.get(cand.sleeper_id) or {}
        player = Player(
            name_full=cand.name,
            name_first=info.get("first_name"),
            name_last=info.get("last_name"),
            position=cand.position,
            nfl_team=cand.nfl_team,
            sleeper_id=cand.sleeper_id,
            gsis_id=(info.get("gsis_id") or "").strip() or None,
            is_active=True,
        )
        session.add(player)
        session.flush()
        player_ids[cand.sleeper_id] = player.player_id
        prep.stubbed.append(cand.sleeper_id)
    return (
        IdentityMaps(
            owner_by_user=owner_by_user,
            owner_names=owner_names,
            owner_abbrev=owner_abbrev,
            player_ids=player_ids,
            reserve_by_week=reserve_by_week or {},
        ),
        prep,
    )


def reserve_by_week_from_archive(
    session: Session, store: RawStore, league_id: str, season: int
) -> dict[int, dict[int, set[str]]]:
    """IR lists known per week (S5-D3): each archived ``rosters`` revision is
    assigned to the NFL week Sleeper reported (``/state/nfl``) when it was last
    seen; the newest revision per week wins."""
    states = session.execute(
        select(SourcePayload.first_fetched_at, SourcePayload.sha256)
        .where(SourcePayload.source == SOURCE, SourcePayload.endpoint == Endpoint.STATE.value)
        .order_by(SourcePayload.first_fetched_at)
    ).all()
    state_weeks: list[tuple[Any, int]] = []
    for first, sha in states:
        body = _json(store, sha)
        if isinstance(body, dict) and str(body.get("season")) == str(season):
            try:
                state_weeks.append((first, int(body["week"])))
            except (KeyError, TypeError, ValueError):
                continue
    revisions = session.execute(
        select(SourcePayload.last_fetched_at, SourcePayload.sha256)
        .where(
            SourcePayload.endpoint == Endpoint.ROSTERS.value,
            SourcePayload.platform_league_id == league_id,
            SourcePayload.http_status == 200,
        )
        .order_by(SourcePayload.last_fetched_at)
    ).all()
    out: dict[int, dict[int, set[str]]] = {}
    for seen_at, sha in revisions:
        weeks = [w for at, w in state_weeks if at <= seen_at]
        if not weeks:
            continue
        body = _json(store, sha)
        if not isinstance(body, list):
            continue
        out[weeks[-1]] = {
            int(r["roster_id"]): {str(p) for p in (r.get("reserve") or [])}
            for r in body
            if isinstance(r, dict) and r.get("roster_id") is not None
        }
    return out


def _json(store: RawStore, sha: str) -> Any:
    try:
        return json.loads(store.get(sha))
    except (ValueError, OSError, gzip.BadGzipFile):
        return None


@dataclass(slots=True)
class SeasonLoad:
    season: int
    result: LoadResult
    prep: PlayerPrep


def load_league_season(
    session: Session,
    source: SleeperLeagueSource,
    league_id: str,
    *,
    franchise_league_id: str = FRANCHISE_LEAGUE_ID,
    reserve_by_week: dict[int, dict[int, set[str]]] | None = None,
) -> SeasonLoad:
    """Normalize + load one Sleeper league season. The caller commits."""
    data = load_league(source, league_id)
    ids, prep = build_identity_maps(
        session, data, league_id=franchise_league_id, reserve_by_week=reserve_by_week
    )
    bundle = normalize(data, ids)
    result = load_season(session, bundle, league_id=franchise_league_id)
    return SeasonLoad(season=data.season, result=result, prep=prep)


__all__ = [
    "FRANCHISE_LEAGUE_ID",
    "PlayerPrep",
    "SeasonLoad",
    "build_identity_maps",
    "load_league_season",
    "reserve_by_week_from_archive",
]
