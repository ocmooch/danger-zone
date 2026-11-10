"""Sleeper's own weekly stats → ``player_stats_raw`` (S6-D3).

Stored with ``source='sleeper'`` and ``is_primary=False``: never scored as the
baseline (``rescore`` reads only nflverse). They let the reconcile tell "the
stat feeds disagree" from "a rule is wrong". Stats are kept in the engine's
vocabulary (``sleeper_stats_to_engine``) so they can be diffed key by key
against the nflverse row. Reads the archive only; the caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ff_pipeline.crawlers.sleeper_league.client import ArchiveMissError
from ff_pipeline.crawlers.sleeper_league.endpoints import BASE_COM, Endpoint, PayloadRequest
from ff_pipeline.repository.models import Player, PlayerStatsRaw
from ff_pipeline.repository.upsert import upsert
from ff_pipeline.scoring.sleeper_rules import sleeper_stats_to_engine

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.sleeper_league.client import SleeperLeagueSource

SOURCE_NAME = "sleeper"


def stats_week_request(season: int, week: int) -> PayloadRequest:
    return PayloadRequest(
        Endpoint.STATS_WEEK,
        f"{BASE_COM}/stats/nfl/{season}/{week}?season_type=regular",
        None,
        season,
        week,
    )


@dataclass(frozen=True, slots=True)
class StatsStoreResult:
    weeks: tuple[int, ...]
    rows_added: int
    rows_updated: int
    unresolved: int  # stat lines whose sleeper id has no players row (not league-relevant)


def store_weekly_stats(
    session: Session,
    source: SleeperLeagueSource,
    *,
    season: int,
    weeks: Sequence[int],
) -> StatsStoreResult:
    """Upsert Sleeper's stat line for every known player in ``weeks``."""
    by_sleeper_id = {
        sid: pid
        for pid, sid in session.execute(
            select(Player.player_id, Player.sleeper_id).where(Player.sleeper_id.is_not(None))
        ).tuples()
    }
    now = datetime.now(tz=UTC)
    rows: list[dict[str, object]] = []
    found: list[int] = []
    unresolved = 0
    for week in weeks:
        try:
            body: Any = source.get(stats_week_request(season, week)).json()
        except (ArchiveMissError, FileNotFoundError):
            continue
        if not isinstance(body, list):
            continue
        found.append(week)
        for line in body:
            if not isinstance(line, dict):
                continue
            sid = str(line.get("player_id") or "")
            pid = by_sleeper_id.get(sid)
            if pid is None:
                unresolved += 1
                continue
            rows.append(
                {
                    "player_id": pid,
                    "season_year": season,
                    "week": week,
                    "season_type": "REG",
                    "nfl_team": line.get("team"),
                    "nfl_opponent": line.get("opponent"),
                    "source": SOURCE_NAME,
                    "stats": sleeper_stats_to_engine(
                        line.get("stats") or {}, is_team_defense=not sid.isdigit()
                    ),
                    "is_primary": False,
                    "ingested_at": now,
                }
            )
    counts = upsert(
        session,
        PlayerStatsRaw,
        rows,
        conflict_cols=("player_id", "season_year", "week", "source"),
    )
    return StatsStoreResult(
        weeks=tuple(found),
        rows_added=counts.rows_added,
        rows_updated=counts.rows_updated,
        unresolved=unresolved,
    )


__all__ = ["SOURCE_NAME", "StatsStoreResult", "stats_week_request", "store_weekly_stats"]
