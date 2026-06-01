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

Irrelevant-position prune
-------------------------

The orphan prune is deliberately blind to *position*, so it can never reach
the bulk of the noise: nflverse's weekly stats file carries a stat line for
every IDP and lineman in the league, so each of them lands a
``player_stats_raw`` (and, after scoring, ``player_stats_scored``) row. That
single reference makes them non-orphans, and they survive. On the real
database that is ~5,500 players an IDP-less league can never roster.

``prune_irrelevant_position_players`` closes that gap. A player is removed
when **both** hold:

* its ``position`` is positively irrelevant — known, non-blank, and not in
  the league's rosterable set (``RELEVANT_POSITIONS``); a ``NULL``/blank
  position is "unknown, so keep", and
* no **protective** table references it — none of ``team_rosters``,
  ``transactions``, ``player_availability``, ``player_id_overrides``.

The protective set is the safety invariant. Position labels are unreliable
(NFL.com scrape artifacts on team defenses, fullbacks rostered as flex,
two-way players like a CB who plays WR), but a roster / transaction /
availability / override row is *ground truth* that this league actually
fielded or pinned the player. Anything so referenced is kept regardless of
its position string, so nothing rosterable is ever dropped.

Removal cascades the player's rows in the **incidental** tables that the
bulk feeds populate — ``player_stats_raw``, ``player_stats_scored``,
``projections``, ``trending_players`` — because those rows describe a player
the league will never roster and would otherwise dangle. The incidental
deletes never touch a kept player.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

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

#: Tables that record real league participation or a manual pin. A player
#: referenced by any of these is kept no matter what its ``position`` string
#: says — rostering/transacting is ground truth; position labels are not.
_PROTECTIVE_REFERRERS = (
    TeamRoster,
    Transaction,
    PlayerAvailability,
    PlayerIdOverride,
)

#: Bulk-feed tables that nflverse/Sleeper populate across the whole NFL
#: universe. For an irrelevant-position player these are incidental and get
#: cascade-deleted alongside the player row. Order matters: ``player_stats_
#: scored`` carries a FK to ``player_stats_raw.stat_id``, so the scored child
#: must be deleted before its raw parent.
_INCIDENTAL_REFERRERS = (
    PlayerStatsScored,
    PlayerStatsRaw,
    Projection,
    TrendingPlayer,
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


@dataclass(slots=True)
class IrrelevantPositionPruneResult:
    """Outcome of one irrelevant-position prune (or dry-run)."""

    dry_run: bool
    players_found: int
    players_deleted: int
    by_position: dict[str, int] = field(default_factory=dict)
    #: table name -> incidental rows deleted (or that would be deleted).
    cascade_deleted: dict[str, int] = field(default_factory=dict)


def _protected_player_ids(session: Session) -> set[int]:
    """Collect every ``player_id`` referenced by a *protective* table.

    A player in this set was rostered, transacted, made available, or pinned
    by hand — real league facts that override an unreliable position label.
    """
    protected: set[int] = set()
    for model in _PROTECTIVE_REFERRERS:
        rows = session.execute(select(model.player_id).distinct()).scalars()
        protected.update(pid for pid in rows if pid is not None)
    return protected


def find_irrelevant_position_players(
    session: Session, relevant_positions: frozenset[str]
) -> list[tuple[int, str]]:
    """Return ``(player_id, position)`` for every prunable irrelevant player.

    A row qualifies when its ``position`` is known, non-blank, and outside
    ``relevant_positions`` (compared upper-cased), *and* it is not referenced
    by any protective table. ``NULL``/blank positions are treated as unknown
    and kept. ``relevant_positions`` is upper-cased defensively so callers
    may pass either case.
    """
    relevant = frozenset(p.upper() for p in relevant_positions)
    protected = _protected_player_ids(session)
    stmt = select(Player.player_id, Player.position)
    out: list[tuple[int, str]] = []
    for pid, position in session.execute(stmt).all():
        if pid in protected:
            continue
        if position is None or not position.strip():
            continue  # unknown position -> keep
        if position.upper() in relevant:
            continue
        out.append((pid, position))
    return out


def _count_incidental_rows(session: Session, player_ids: list[int]) -> dict[str, int]:
    """Per-incidental-table row counts for ``player_ids`` (no mutation)."""
    counts: dict[str, int] = {}
    for model in _INCIDENTAL_REFERRERS:
        total = 0
        for start in range(0, len(player_ids), _DELETE_CHUNK):
            chunk = player_ids[start : start + _DELETE_CHUNK]
            total += session.execute(
                select(func.count()).select_from(model).where(model.player_id.in_(chunk))
            ).scalar_one()
        counts[model.__tablename__] = total
    return counts


def prune_irrelevant_position_players(
    session: Session, *, relevant_positions: frozenset[str], dry_run: bool
) -> IrrelevantPositionPruneResult:
    """Delete irrelevant-position players + their incidental rows.

    Caller commits on success. A dry run touches nothing but still reports
    the full position breakdown and the incidental row counts that *would*
    be cascade-deleted, so the operator sees the blast radius before
    choosing to proceed. Incidental rows are deleted before the parent
    ``players`` rows so no foreign key is ever left dangling.
    """
    candidates = find_irrelevant_position_players(session, relevant_positions)
    by_position: dict[str, int] = {}
    for _pid, position in candidates:
        by_position[position] = by_position.get(position, 0) + 1

    player_ids = [pid for pid, _pos in candidates]

    if dry_run or not candidates:
        cascade = _count_incidental_rows(session, player_ids) if candidates else {}
        log.info(
            "Irrelevant-position prune (dry run)"
            if dry_run
            else "Irrelevant-position prune: nothing to do",
            players_found=len(candidates),
            cascade=cascade,
        )
        return IrrelevantPositionPruneResult(
            dry_run=dry_run,
            players_found=len(candidates),
            players_deleted=0,
            by_position=by_position,
            cascade_deleted=cascade,
        )

    # Count the incidental children up front (so the reported totals don't
    # depend on driver rowcount semantics), then delete children before the
    # parent players. Everything is chunked to stay under SQLite's bound-
    # parameter ceiling.
    cascade_deleted = _count_incidental_rows(session, player_ids)
    for model in _INCIDENTAL_REFERRERS:
        for start in range(0, len(player_ids), _DELETE_CHUNK):
            chunk = player_ids[start : start + _DELETE_CHUNK]
            session.execute(delete(model).where(model.player_id.in_(chunk)))

    for start in range(0, len(player_ids), _DELETE_CHUNK):
        chunk = player_ids[start : start + _DELETE_CHUNK]
        session.execute(delete(Player).where(Player.player_id.in_(chunk)))

    log.info(
        "Irrelevant-position prune complete",
        players_found=len(candidates),
        players_deleted=len(player_ids),
        cascade=cascade_deleted,
    )
    return IrrelevantPositionPruneResult(
        dry_run=False,
        players_found=len(candidates),
        players_deleted=len(player_ids),
        by_position=by_position,
        cascade_deleted=cascade_deleted,
    )


__all__ = [
    "IrrelevantPositionPruneResult",
    "PruneResult",
    "find_irrelevant_position_players",
    "find_orphan_players",
    "prune_irrelevant_position_players",
    "prune_orphan_players",
]
