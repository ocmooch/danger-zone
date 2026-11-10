"""One Sleeper league capture run: fetch the request plan, archive every response (S2).

Nothing here parses league data into league tables; that is S3-S5. The only
DB writes are ``source_payloads`` index rows (the caller commits).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from ff_pipeline.archive.raw_store import PayloadMeta, record_payload
from ff_pipeline.crawlers.sleeper_league.client import FetchedPayload, SleeperLeagueError
from ff_pipeline.crawlers.sleeper_league.endpoints import (
    SOURCE,
    Endpoint,
    draft_ids,
    draft_requests,
    drafts_request,
    league_request,
    league_requests,
    players_request,
    state_request,
    user_leagues_request,
    users_request,
    weeks_to_capture,
)
from ff_pipeline.crawlers.sleeper_league.lineage import (
    LeagueLink,
    league_for_season,
    walk_lineage,
)
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import SourcePayload

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore
    from ff_pipeline.crawlers.sleeper_league.client import SleeperLeagueSource
    from ff_pipeline.crawlers.sleeper_league.endpoints import PayloadRequest

log = get_logger(__name__)


class CaptureError(RuntimeError):
    """The run could not be planned (state or league unavailable)."""


@dataclass(slots=True)
class CaptureSummary:
    league_id: str = ""
    season: int | None = None
    max_week: int = 0
    lineage: list[LeagueLink] = field(default_factory=list)
    requests: int = 0
    new_revisions: int = 0
    unchanged: int = 0
    new_files: int = 0
    non_ok: list[str] = field(default_factory=list)  # "404 <url>"
    errors: list[str] = field(default_factory=list)
    players_skipped: bool = False
    dry_run: bool = False
    renewed_league_id: str | None = None  # S9-D4: a renewal found and followed this run


@dataclass(slots=True)
class _Archiver:
    session: Session
    source: SleeperLeagueSource
    store: RawStore
    run_id: str
    dry_run: bool
    summary: CaptureSummary
    fetched: dict[str, FetchedPayload] = field(default_factory=dict)

    def fetch(self, req: PayloadRequest, *, required: bool = False) -> FetchedPayload | None:
        if req.url in self.fetched:
            return self.fetched[req.url]
        try:
            payload = self.source.get(req)
        except SleeperLeagueError as exc:
            if required:
                raise CaptureError(str(exc)) from exc
            self.summary.errors.append(str(exc))
            log.warning("sleeper league fetch failed", url=req.url, error=str(exc))
            return None
        self.summary.requests += 1
        self.fetched[req.url] = payload
        if not payload.ok:
            self.summary.non_ok.append(f"{payload.http_status} {req.url}")
        if not self.dry_run:
            self._archive(payload)
        return payload

    def _archive(self, payload: FetchedPayload) -> None:
        req = payload.request
        season = req.season_year
        if season is None and req.endpoint is Endpoint.LEAGUE:
            body = payload.json()
            if isinstance(body, dict):
                season = _as_int(body.get("season"))
        sha, written = self.store.put(payload.body)
        new = record_payload(
            self.session,
            PayloadMeta(
                source=SOURCE,
                endpoint=req.endpoint.value,
                request_url=req.url,
                platform_league_id=req.league_id,
                season_year=season,
                week=req.week,
                http_status=payload.http_status,
            ),
            sha256=sha,
            byte_size=len(payload.body),
            fetched_at=payload.fetched_at,
            run_id=self.run_id,
        )
        self.summary.new_files += int(written)
        if new:
            self.summary.new_revisions += 1
        else:
            self.summary.unchanged += 1


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def players_due(session: Session, *, now: datetime, refresh_hours: int) -> bool:
    """True when the players directory was not captured within ``refresh_hours``."""
    last = session.scalar(
        select(func.max(SourcePayload.last_fetched_at)).where(
            SourcePayload.source == SOURCE,
            SourcePayload.endpoint == Endpoint.PLAYERS.value,
            SourcePayload.http_status == 200,
        )
    )
    if last is None:
        return True
    if last.tzinfo is None:  # SQLite returns naive datetimes
        last = last.replace(tzinfo=now.tzinfo)
    return now - last >= timedelta(hours=refresh_hours)


def capture_season(
    session: Session,
    source: SleeperLeagueSource,
    store: RawStore,
    *,
    start_league_id: str,
    season: int | None,
    run_id: str,
    now: datetime,
    players_refresh_hours: int = 24,
    force_players: bool = False,
    dry_run: bool = False,
) -> CaptureSummary:
    """Archive every endpoint for one season of the league chain.

    ``season=None`` captures the newest league in the chain. The caller
    commits the session.
    """
    summary = CaptureSummary(dry_run=dry_run)
    arc = _Archiver(session, source, store, run_id, dry_run, summary)

    state_payload = arc.fetch(state_request(), required=True)
    state = state_payload.json() if state_payload is not None else None
    if not isinstance(state, dict):
        raise CaptureError("Sleeper /state/nfl returned no usable JSON")

    def fetch_league(league_id: str) -> dict[str, Any] | None:
        payload = arc.fetch(league_request(league_id), required=True)
        body = payload.json() if payload is not None else None
        return body if isinstance(body, dict) else None

    chain = walk_lineage(fetch_league, start_league_id)
    state_season = _int(state.get("season"))
    newest = chain[0]
    if season is None and state_season and newest.season and state_season > newest.season:
        renewed = find_renewal(arc, newest.league_id, newest.season, state_season)
        if renewed is not None:
            chain = walk_lineage(fetch_league, renewed)
            summary.renewed_league_id = renewed
            log.info("renewed league found", league_id=renewed, season=state_season)
    summary.lineage = chain
    link = chain[0] if season is None else league_for_season(chain, season)
    if link.season is None:
        raise CaptureError(f"league {link.league_id} has no season")
    summary.league_id = link.league_id
    summary.season = link.season
    league = fetch_league(link.league_id) or {}

    summary.max_week = weeks_to_capture(state, league)

    drafts_payload = arc.fetch(drafts_request(link.league_id, link.season))
    drafts = drafts_payload.json() if drafts_payload is not None else None
    for did in draft_ids(league, drafts):
        for req in draft_requests(link.league_id, link.season, did):
            arc.fetch(req)

    for req in league_requests(link.league_id, link.season, summary.max_week):
        arc.fetch(req)

    if force_players or players_due(session, now=now, refresh_hours=players_refresh_hours):
        arc.fetch(players_request())
    else:
        summary.players_skipped = True

    if not dry_run:
        session.flush()
    return summary


def find_renewal(arc: _Archiver, league_id: str, season: int, target: int) -> str | None:
    """The ``target``-season league whose ``previous_league_id`` is ``league_id``,
    looked up in the commissioner's (``is_owner``) leagues (S9-D4)."""
    users_payload = arc.fetch(users_request(league_id, season))
    users = users_payload.json() if users_payload is not None else None
    if not isinstance(users, list):
        return None
    commissioner = next(
        (str(u["user_id"]) for u in users if isinstance(u, dict) and u.get("is_owner")), None
    )
    if commissioner is None:
        return None
    payload = arc.fetch(user_leagues_request(commissioner, target))
    leagues = payload.json() if payload is not None else None
    for lg in leagues if isinstance(leagues, list) else []:
        if isinstance(lg, dict) and str(lg.get("previous_league_id") or "") == league_id:
            return str(lg["league_id"])
    return None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["CaptureError", "CaptureSummary", "capture_season", "find_renewal", "players_due"]
