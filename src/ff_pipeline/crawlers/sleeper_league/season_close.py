"""Facts the season-close seal needs from the archive and the reconcile (S9-D5)."""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ff_pipeline.crawlers.sleeper_league.archive_view import load_league
from ff_pipeline.crawlers.sleeper_league.client import ArchiveSleeperLeagueSource
from ff_pipeline.crawlers.sleeper_league.endpoints import Endpoint
from ff_pipeline.legacy.seal_season import SeasonCloseFacts
from ff_pipeline.normalizer.sleeper_brackets import derive_postseason
from ff_pipeline.normalizer.sleeper_league import team_key
from ff_pipeline.repository.models import Season, SourcePayload
from ff_pipeline.scoring.platform_reconcile import RULE, UNEXPLAINED, reconcile_platform_points

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore


def complete_observed_at(session: Session, store: RawStore, league_id: str) -> datetime | None:
    """First capture time of a ``league`` revision whose status is ``complete``."""
    rows = session.execute(
        select(SourcePayload.first_fetched_at, SourcePayload.sha256)
        .where(
            SourcePayload.endpoint == Endpoint.LEAGUE.value,
            SourcePayload.platform_league_id == league_id,
            SourcePayload.http_status == 200,
        )
        .order_by(SourcePayload.first_fetched_at)
    ).tuples()
    for first, sha in rows:
        try:
            body: Any = json.loads(store.get(sha))
        except (ValueError, OSError, gzip.BadGzipFile):
            continue
        if isinstance(body, dict) and body.get("status") == "complete":
            return first
    return None


def gather_facts(
    session: Session, store: RawStore, *, year: int, franchise_league_id: str
) -> SeasonCloseFacts:
    season = session.scalars(
        select(Season).where(Season.league_id == franchise_league_id, Season.year == year)
    ).first()
    if season is None or not season.platform_league_id:
        return SeasonCloseFacts(complete_observed_at=None, reconcile_failures=0)
    rec = reconcile_platform_points(session, league_id=franchise_league_id, season_year=year)
    failures = sum(rec.counts(starters_only=False).get(c, 0) for c in (RULE, UNEXPLAINED))
    if rec.note:
        failures += 1  # e.g. scoring_rules_missing
    data = load_league(ArchiveSleeperLeagueSource(session, store), season.platform_league_id)
    settings = data.league.get("settings") or {}
    last = int(settings.get("last_scored_leg") or 0)
    if data.league.get("status") == "complete":
        last = max(last, data.weeks)
    scores = {
        (w, team_key(m["roster_id"])): float(m.get("custom_points") or m.get("points") or 0)
        for w, ms in data.matchups_by_week.items()
        for m in ms
    }
    post = derive_postseason(
        winners=data.winners_bracket,
        losers=data.losers_bracket,
        team_key=team_key,
        playoff_week_start=int(settings["playoff_week_start"])
        if settings.get("playoff_week_start")
        else None,
        playoff_round_type=settings.get("playoff_round_type"),
        last_finalized_week=last,
        scores=scores,
    )
    return SeasonCloseFacts(
        complete_observed_at=complete_observed_at(session, store, season.platform_league_id),
        reconcile_failures=failures,
        bracket_warnings=tuple(post.warnings),
    )


__all__ = ["complete_observed_at", "gather_facts"]
