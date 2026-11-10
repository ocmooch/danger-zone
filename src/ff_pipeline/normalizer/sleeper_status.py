"""Sleeper players directory → ``player_status_snapshots`` (S7, ``04`` §8).

One row per (player, capture date UTC, ``source='sleeper'``) for the players the
league references that season (S7-D7). A directory revision seen on several
capture days gives a row for its first and its last day (the index keeps only
those two sightings). The game-day capture (Sunday morning) makes the kickoff
statuses available; the Wednesday capture adds a mid-week reading. Text fields
such as injury notes and news are not stored, only the news timestamp.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ff_pipeline.crawlers.sleeper_league.endpoints import Endpoint
from ff_pipeline.repository.models import PlayerStatusSnapshot, SourcePayload
from ff_pipeline.repository.upsert import UpsertCounts, upsert

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy.orm import Session

    from ff_pipeline.archive.raw_store import RawStore

SOURCE = "sleeper"


def status_rows(
    directory: Mapping[str, Any], *, as_of: date, player_ids: Mapping[str, int]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sid, pid in player_ids.items():
        info = directory.get(sid)
        if not isinstance(info, dict):
            continue
        extra = {k: info[k] for k in ("news_updated", "practice_description") if info.get(k)}
        rows.append(
            {
                "player_id": pid,
                "as_of_date": as_of,
                "source": SOURCE,
                "injury_status": _s(info.get("injury_status"), 32),
                "injury_body_part": _s(info.get("injury_body_part"), 64),
                "injury_start_date": _date(info.get("injury_start_date")),
                "practice_participation": _s(info.get("practice_participation"), 32),
                "depth_chart_position": _s(info.get("depth_chart_position"), 16),
                "depth_chart_order": _i(info.get("depth_chart_order")),
                "nfl_team": _s(info.get("team"), 8),
                "status": _s(info.get("status"), 32),
                "extra_data": extra or None,
            }
        )
    return rows


def store_status_snapshots(
    session: Session,
    revisions: Iterable[tuple[date, Mapping[str, Any]]],
    *,
    player_ids: Mapping[str, int],
) -> UpsertCounts:
    by_key: dict[tuple[int, date], dict[str, Any]] = {}
    for as_of, directory in revisions:
        for row in status_rows(directory, as_of=as_of, player_ids=player_ids):
            by_key[(row["player_id"], as_of)] = row  # several revisions one day: the last wins
    return upsert(
        session,
        PlayerStatusSnapshot,
        list(by_key.values()),
        conflict_cols=("player_id", "as_of_date", "source"),
    )


def archived_directory_revisions(
    session: Session, store: RawStore, *, skip_days: set[date] | None = None
) -> list[tuple[date, dict[str, Any]]]:
    """``(capture date, directory)`` for each archived revision's first and last day.

    ``skip_days`` (days already snapshotted) avoids re-reading ~5 MB directories
    on every load."""
    import gzip
    import json

    skip = skip_days or set()
    out: list[tuple[date, dict[str, Any]]] = []
    rows = session.execute(
        select(SourcePayload.sha256, SourcePayload.first_fetched_at, SourcePayload.last_fetched_at)
        .where(SourcePayload.endpoint == Endpoint.PLAYERS.value, SourcePayload.http_status == 200)
        .order_by(SourcePayload.first_fetched_at)
    ).tuples()
    for sha, first, last in rows:
        days = [d for d in dict.fromkeys(x.date() for x in (first, last) if x) if d not in skip]
        if not days:
            continue
        try:
            body = json.loads(store.get(sha))
        except (ValueError, OSError, gzip.BadGzipFile):
            continue
        if not isinstance(body, dict):
            continue
        out.extend((day, body) for day in days)
    return out


def _s(v: Any, n: int) -> str | None:
    if v is None or v == "":
        return None
    return str(v)[:n]


def _i(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def snapshotted_days(session: Session) -> set[date]:
    return set(
        session.scalars(
            select(PlayerStatusSnapshot.as_of_date)
            .where(PlayerStatusSnapshot.source == SOURCE)
            .distinct()
        )
    )


__all__ = [
    "archived_directory_revisions",
    "snapshotted_days",
    "status_rows",
    "store_status_snapshots",
]
