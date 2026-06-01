"""High-level "run the nflverse crawler" function.

Glues the ``NflverseClient`` reader to the repository upserter and records
observability rows in ``pipeline_runs`` and ``source_health``. Called from
the CLI (``ff-pipeline run --source nflverse``) and reusable from the
backfill script in M9.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.crawlers.nflverse.client import (
    LiveNflverseSource,
    NflverseClient,
    NflversePlayerMeta,
    NflversePlayerStat,
)
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import (
    PipelineRun,
    Player,
    PlayerStatsRaw,
    SourceHealth,
)
from ff_pipeline.repository.upsert import UpsertCounts, upsert

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.client import NflverseSource

log = get_logger(__name__)

SOURCE_NAME = "nflverse"


@dataclass(frozen=True, slots=True)
class NflverseRunResult:
    """Outcome of one nflverse crawler run.

    The CLI surfaces these counts to the user; ``pipeline_runs.sources_summary``
    persists the same JSON shape for the M10 status command.
    """

    players_added: int
    players_updated: int
    stats_added: int
    stats_updated: int
    duration_ms: int


def run_nflverse(
    session: Session,
    *,
    seasons: Sequence[int],
    source: NflverseSource | None = None,
    mode: str = "full_sync",
    league_start_year: int | None = None,
    relevant_positions: frozenset[str] | None = None,
) -> NflverseRunResult:
    """Pull nflverse data for ``seasons`` and write it into the DB.

    Creates one ``pipeline_runs`` row + one ``source_health`` row.
    Caller's responsibility to commit. On failure, the run row is marked
    ``failed`` with the exception message and the exception re-raised so
    the CLI returns a non-zero exit code.

    ``league_start_year`` and ``relevant_positions`` scope which player
    *metadata* rows get upserted: nflverse returns the entire NFL player
    universe back to 1999, but a player whose career ended before the
    league existed — or who plays a position this league can't roster —
    is pure clutter. When either is ``None`` no filtering is applied
    (preserving the historical "ingest everything" behaviour). Note this
    only gates the ``load_players`` metadata pass; players who actually
    recorded a stat in a league season are still stubbed from the stat
    rows themselves, so nothing rosterable is ever dropped.
    """

    run = PipelineRun(status="running", mode=mode)
    session.add(run)
    session.flush()  # populate run.run_id for source_health FK
    start = time.perf_counter()

    try:
        client = NflverseClient(source=source or LiveNflverseSource())
        player_meta = client.players()
        player_stats = client.player_stats(seasons)

        kept_meta = _filter_relevant_players(
            player_meta,
            league_start_year=league_start_year,
            relevant_positions=relevant_positions,
        )
        player_counts = _upsert_players(session, kept_meta)
        # gsis_id -> player_id resolution depends on the players upsert above
        # having been flushed; do it now so the stat rows can reference IDs.
        session.flush()
        gsis_to_player_id = _gsis_id_to_player_id(session, [s.gsis_id for s in player_stats])
        # Auto-create stub players for any gsis_id that showed up in stats but
        # not in load_players() (rare but possible — preseason call-ups, etc.).
        stub_counts = _create_stub_players(session, player_stats, gsis_to_player_id)
        if stub_counts > 0:
            session.flush()
            gsis_to_player_id = _gsis_id_to_player_id(session, [s.gsis_id for s in player_stats])

        stat_counts = _upsert_player_stats(session, player_stats, gsis_to_player_id)

    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        run.status = "failed"
        run.finished_at = datetime.now(tz=UTC)
        run.error_summary = f"{type(exc).__name__}: {exc}"
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source=SOURCE_NAME,
                status="failed",
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        )
        raise

    duration_ms = int((time.perf_counter() - start) * 1000)
    total_players_added = player_counts.rows_added + stub_counts
    run.status = "success"
    run.finished_at = datetime.now(tz=UTC)
    run.sources_summary = {
        SOURCE_NAME: {
            "players_added": total_players_added,
            "players_updated": player_counts.rows_updated,
            "stats_added": stat_counts.rows_added,
            "stats_updated": stat_counts.rows_updated,
            "seasons": list(seasons),
        }
    }
    session.add(
        SourceHealth(
            run_id=run.run_id,
            source=SOURCE_NAME,
            status="success",
            rows_added=total_players_added + stat_counts.rows_added,
            rows_updated=player_counts.rows_updated + stat_counts.rows_updated,
            duration_ms=duration_ms,
        )
    )

    log.info(
        "nflverse run complete",
        seasons=list(seasons),
        players_added=total_players_added,
        players_updated=player_counts.rows_updated,
        stats_added=stat_counts.rows_added,
        stats_updated=stat_counts.rows_updated,
        duration_ms=duration_ms,
    )

    return NflverseRunResult(
        players_added=total_players_added,
        players_updated=player_counts.rows_updated,
        stats_added=stat_counts.rows_added,
        stats_updated=stat_counts.rows_updated,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _filter_relevant_players(
    meta: list[NflversePlayerMeta],
    *,
    league_start_year: int | None,
    relevant_positions: frozenset[str] | None,
) -> list[NflversePlayerMeta]:
    """Drop metadata rows that can never matter to this league.

    A player is kept unless it fails one of the active filters:

    * **Era** — ``last_season`` is known and predates ``league_start_year``;
      the player retired before the league's first season.
    * **Position** — ``position`` is not in ``relevant_positions``; the
      league can't roster it (every IDP/lineman/specialist).

    A ``None`` ``last_season`` or ``position`` is treated as *unknown, so
    keep* — we only drop on positive evidence of irrelevance. Both filters
    are skipped entirely when their parameter is ``None``.
    """
    if league_start_year is None and relevant_positions is None:
        return meta

    kept: list[NflversePlayerMeta] = []
    dropped_era = 0
    dropped_position = 0
    for m in meta:
        if (
            league_start_year is not None
            and m.last_season is not None
            and m.last_season < league_start_year
        ):
            dropped_era += 1
            continue
        if (
            relevant_positions is not None
            and m.position is not None
            and m.position.upper() not in relevant_positions
        ):
            dropped_position += 1
            continue
        kept.append(m)

    if dropped_era or dropped_position:
        log.info(
            "Filtered nflverse player metadata to league scope",
            kept=len(kept),
            dropped_pre_league_era=dropped_era,
            dropped_irrelevant_position=dropped_position,
            league_start_year=league_start_year,
        )
    return kept


def _upsert_players(session: Session, meta: list[NflversePlayerMeta]) -> UpsertCounts:
    rows = [
        {
            "gsis_id": m.gsis_id,
            "name_full": m.name_full,
            "name_first": m.name_first,
            "name_last": m.name_last,
            "position": m.position,
            "nfl_team": m.nfl_team,
            "birth_date": m.birth_date,
            "rookie_year": m.rookie_year,
            "last_season": m.last_season,
            "espn_id": m.espn_id,
            "is_active": (m.status or "").upper() == "ACT" or m.status is None,
        }
        for m in meta
    ]
    return upsert(session, Player, rows, conflict_cols=("gsis_id",))


def _gsis_id_to_player_id(session: Session, gsis_ids: list[str]) -> dict[str, int]:
    if not gsis_ids:
        return {}
    stmt = select(Player.gsis_id, Player.player_id).where(Player.gsis_id.in_(gsis_ids))
    return {gsis: pid for gsis, pid in session.execute(stmt).all() if gsis}


def _create_stub_players(
    session: Session,
    stats: list[NflversePlayerStat],
    gsis_to_player_id: dict[str, int],
) -> int:
    missing = [s for s in stats if s.gsis_id not in gsis_to_player_id]
    if not missing:
        return 0
    rows = [
        {
            "gsis_id": s.gsis_id,
            "name_full": s.player_display_name or s.gsis_id,
            "position": s.position,
            "nfl_team": s.nfl_team,
            "is_active": True,
        }
        for s in missing
    ]
    counts = upsert(session, Player, rows, conflict_cols=("gsis_id",))
    return counts.rows_added


def _upsert_player_stats(
    session: Session,
    stats: list[NflversePlayerStat],
    gsis_to_player_id: dict[str, int],
) -> UpsertCounts:
    now = datetime.now(tz=UTC)
    rows = []
    for s in stats:
        pid = gsis_to_player_id.get(s.gsis_id)
        if pid is None:
            # Shouldn't happen post-stub-creation, but skip rather than crash.
            log.warning("Skipping nflverse stat row with no player_id", gsis_id=s.gsis_id)
            continue
        rows.append(
            {
                "player_id": pid,
                "season_year": s.season_year,
                "week": s.week,
                "season_type": s.season_type,
                "nfl_opponent": s.nfl_opponent,
                "source": SOURCE_NAME,
                # SQLAlchemy's JSON type accepts dicts directly on both
                # dialects; no manual json.dumps needed. The json import is
                # retained for the rare case a caller logs the payload.
                "stats": s.stats,
                "is_primary": True,
                "ingested_at": now,
            }
        )
    return upsert(
        session,
        PlayerStatsRaw,
        rows,
        conflict_cols=("player_id", "season_year", "week", "source"),
    )


__all__ = ["NflverseRunResult", "run_nflverse"]
