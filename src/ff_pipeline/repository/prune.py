"""Prune fully-orphaned ``players`` rows.

nflverse's ``load_players`` returns the entire NFL player universe back to
1999 (and older), so a fresh backfill leaves the ``players`` table dominated
by people this league never touched — players who retired before the league's
first season, plus every position the league can't roster. Two-thirds of the
table can be pure metadata with no connection to any league fact.

The ingestion-time filter in the nflverse runner stops *new* such rows from
accumulating; this module cleans up rows that already landed.

What counts as an orphan
------------------------

A player is **fully orphaned** when no other table references its
``player_id`` — none of:

* ``player_stats_raw`` / ``player_stats_scored`` (ever recorded a stat in a
  league season),
* ``team_rosters`` (ever rostered),
* ``player_availability`` (ever in the league's waiver/FA universe),
* ``transactions`` (ever added/dropped/traded),
* ``projections`` (ever projected),
* ``trending_players`` (ever trended),
* ``player_id_overrides`` (manually pinned).

Because orphans have no referrers by definition, deletion is safe: there is
no foreign key to cascade and nothing downstream can break. This is the
conservative scope — referenced-but-irrelevant rows (e.g. an IDP player who
recorded stats nflverse happened to pull) are intentionally left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import (
    Player,
    PlayerAvailability,
    PlayerIdOverride,
    PlayerStatsRaw,
    PlayerStatsScored,
    Projection,
    TeamRoster,
    Transaction,
    TrendingPlayer,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Max ids per DELETE — well under SQLite's default 999 bound-parameter cap.
_DELETE_CHUNK = 500

#: Every table carrying a ``player_id`` FK back to ``players``. A player
#: referenced by *any* of these is not an orphan.
_REFERRERS = (
    PlayerStatsRaw,
    PlayerStatsScored,
    TeamRoster,
    PlayerAvailability,
    Transaction,
    Projection,
    TrendingPlayer,
    PlayerIdOverride,
)


@dataclass(slots=True)
class PruneResult:
    """Outcome of one prune (or dry-run)."""

    dry_run: bool
    orphans_found: int
    deleted: int
    by_position: dict[str, int] = field(default_factory=dict)


def _referenced_player_ids(session: Session) -> set[int]:
    """Collect every ``player_id`` referenced by any referrer table.

    One ``SELECT DISTINCT player_id`` per table — each a single sequential
    scan — instead of a correlated ``NOT EXISTS`` that re-scans every
    referrer once per candidate player. Several referrer tables have no
    standalone ``player_id`` index, so the correlated form is minutes-slow
    on the real database; this form is a handful of one-pass scans.
    """
    referenced: set[int] = set()
    for model in _REFERRERS:
        rows = session.execute(select(model.player_id).distinct()).scalars()
        referenced.update(pid for pid in rows if pid is not None)
    return referenced


def find_orphan_players(session: Session) -> list[tuple[int, str | None]]:
    """Return ``(player_id, position)`` for every fully-orphaned player."""
    referenced = _referenced_player_ids(session)
    stmt = select(Player.player_id, Player.position)
    return [(pid, pos) for pid, pos in session.execute(stmt).all() if pid not in referenced]


def prune_orphan_players(session: Session, *, dry_run: bool) -> PruneResult:
    """Delete fully-orphaned players (or report them, when ``dry_run``).

    Caller is responsible for committing on success. A dry run touches no
    rows. The position breakdown is computed before any delete so the
    summary is identical in both modes.
    """
    orphans = find_orphan_players(session)
    by_position: dict[str, int] = {}
    for _pid, position in orphans:
        key = position or "(none)"
        by_position[key] = by_position.get(key, 0) + 1

    if dry_run or not orphans:
        log.info(
            "Orphan prune (dry run)" if dry_run else "Orphan prune: nothing to do",
            orphans_found=len(orphans),
        )
        return PruneResult(
            dry_run=dry_run,
            orphans_found=len(orphans),
            deleted=0,
            by_position=by_position,
        )

    # Delete by primary key, not by re-evaluating the orphan predicate.
    # Several referrer tables have no standalone ``player_id`` index, so a
    # ``DELETE ... WHERE <8 correlated NOT EXISTS>`` makes SQLite full-scan
    # them once per candidate row — pathologically slow on a 25k-row table.
    # We already have the orphan ids from the (planned, fast) SELECT above;
    # delete them straight by PK, chunked to stay clear of SQLite's bound-
    # parameter ceiling.
    orphan_ids = [pid for pid, _pos in orphans]
    for start in range(0, len(orphan_ids), _DELETE_CHUNK):
        chunk = orphan_ids[start : start + _DELETE_CHUNK]
        session.execute(delete(Player).where(Player.player_id.in_(chunk)))
    deleted = len(orphan_ids)
    log.info("Orphan prune complete", orphans_found=len(orphans), deleted=deleted)
    return PruneResult(
        dry_run=False,
        orphans_found=len(orphans),
        deleted=deleted,
        by_position=by_position,
    )


__all__ = ["PruneResult", "find_orphan_players", "prune_orphan_players"]
