"""Ingest nflverse weekly injury report data into ``player_injury_reports``.

Modelled on runner.py. Reads ``load_injuries(seasons)``, resolves each row's
``gsis_id`` to an internal ``player_id``, and upserts into the
``player_injury_reports`` table. Rows whose ``gsis_id`` is null or has no
matching ``players`` row are silently skipped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.crawlers.nflverse.client import LiveNflverseSource
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import Player, PlayerInjuryReport
from ff_pipeline.repository.upsert import UpsertCounts, upsert

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.client import NflverseSource

log = get_logger(__name__)

SOURCE_NAME = "nflverse_injuries"


@dataclass(frozen=True, slots=True)
class NflverseInjuryRunResult:
    rows_added: int
    rows_updated: int
    duration_ms: int


def run_injury_reports(
    session: Session,
    *,
    seasons: Sequence[int],
    source: NflverseSource | None = None,
) -> NflverseInjuryRunResult:
    """Pull nflverse injury report data for ``seasons`` and upsert into DB.

    Caller's responsibility to commit. Rows whose ``gsis_id`` is null or
    doesn't resolve to a known ``player_id`` are silently skipped — no stub
    players are created (injury data is supplemental, not identity-defining).
    """
    start = time.perf_counter()
    _source = source or LiveNflverseSource()

    df = _source.load_injuries(seasons)
    gsis_to_player_id = _build_gsis_index(session, df)
    counts = _upsert_injury_rows(session, df, gsis_to_player_id)

    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(
        "injury_reports run complete",
        seasons=list(seasons),
        rows_added=counts.rows_added,
        rows_updated=counts.rows_updated,
        duration_ms=duration_ms,
    )
    return NflverseInjuryRunResult(
        rows_added=counts.rows_added,
        rows_updated=counts.rows_updated,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_gsis_index(session: Session, df: pl.DataFrame) -> dict[str, int]:
    """Query ``players`` for all gsis_ids present in ``df``."""
    if df.is_empty() or "gsis_id" not in df.columns:
        return {}
    gsis_ids = [v for v in df["gsis_id"].unique().to_list() if v]
    if not gsis_ids:
        return {}
    stmt = select(Player.gsis_id, Player.player_id).where(Player.gsis_id.in_(gsis_ids))
    return {gsis: pid for gsis, pid in session.execute(stmt).all() if gsis}


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _upsert_injury_rows(
    session: Session,
    df: pl.DataFrame,
    gsis_to_player_id: dict[str, int],
) -> UpsertCounts:
    rows = []
    skipped = 0
    for row in df.iter_rows(named=True):
        gsis = _opt_str(row.get("gsis_id"))
        if not gsis:
            skipped += 1
            continue
        pid = gsis_to_player_id.get(gsis)
        if pid is None:
            skipped += 1
            continue
        season_year = _opt_int(row.get("season"))
        week = _opt_int(row.get("week"))
        if season_year is None or week is None:
            skipped += 1
            continue
        rows.append(
            {
                "player_id": pid,
                "season_year": season_year,
                "week": week,
                "game_type": _opt_str(row.get("game_type")),
                "report_status": _opt_str(row.get("report_status")),
                "report_primary_injury": _opt_str(row.get("report_primary_injury")),
                "report_secondary_injury": _opt_str(row.get("report_secondary_injury")),
                "practice_status": _opt_str(row.get("practice_status")),
                "date_modified": _parse_datetime(row.get("date_modified")),
            }
        )
    if skipped:
        log.info(
            "Skipped injury rows with no resolved player_id or missing key fields",
            skipped=skipped,
            ingested=len(rows),
        )
    return upsert(
        session,
        PlayerInjuryReport,
        rows,
        conflict_cols=("player_id", "season_year", "week", "game_type"),
    )


__all__ = ["NflverseInjuryRunResult", "run_injury_reports"]
