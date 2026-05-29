"""Idempotent INSERT ... ON CONFLICT ... DO UPDATE helper.

Every crawler writes through this single function so that re-running a
pipeline doesn't produce duplicates and corrections silently replace the
previous values. The implementation is dialect-aware (SQLite + PostgreSQL)
and returns a count of rows added vs. updated so source-health bookkeeping
can be filled in by the caller.

Why a hand-rolled helper instead of SQLAlchemy's ``session.merge()``:

* ``merge`` is per-row and issues a SELECT before each write, which is far
  too chatty for the ~10k+ rows nflverse produces per season.
* ``merge`` keys on the primary key only; our natural-key constraints
  (e.g. ``UNIQUE(player_id, season_year, week, source)``) are what we
  actually want to deduplicate on.

The two supported dialects expose ``insert(...).on_conflict_do_update(...)``
with matching APIs; the only difference is which ``insert`` we import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import Base

log = get_logger(__name__)

# SQLite caps bound parameters per statement at 999 (older versions) or
# 32766 (newer). Stay well under either: 500 rows * up to ~20 cols = 10k.
_DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class UpsertCounts:
    """How many rows the most recent ``upsert`` call inserted vs. updated.

    SQLite's ``RETURNING xmax``-style trick is not available, so we
    approximate by counting pre-existing rows that matched the conflict key
    and subtracting from the input set. Exact counts only when the caller
    cares; the crawler uses them for source-health stats, not invariants.
    """

    rows_added: int
    rows_updated: int

    @property
    def rows_total(self) -> int:
        return self.rows_added + self.rows_updated


def upsert(
    session: Session,
    model: type[Base],
    rows: Iterable[dict[str, Any]],
    *,
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> UpsertCounts:
    """Insert ``rows`` into ``model``'s table; update on conflict.

    ``conflict_cols`` must name a UNIQUE constraint or primary key on the
    table — both dialects require an inferred constraint to attach the DO
    UPDATE clause to.

    ``update_cols`` defaults to "every column in the input dict except the
    conflict keys and ``created_at``" — so the caller doesn't have to
    enumerate them explicitly for the common case. ``updated_at`` is left
    to the model's ``onupdate=func.now()`` default.
    """

    materialized = list(rows)
    if not materialized:
        return UpsertCounts(0, 0)

    table = model.__table__
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    insert_fn = _insert_for_dialect(dialect)

    rows_added = 0
    rows_updated = 0

    for chunk in _chunked(materialized, batch_size):
        # Resolve which existing PKs already match this batch's conflict
        # keys *before* the write — that's how we tell INSERT from UPDATE.
        existing_keys = _existing_conflict_keys(session, model, chunk, conflict_cols)

        stmt = insert_fn(table).values(chunk)
        effective_update_cols = update_cols or _default_update_cols(chunk[0], conflict_cols)
        if effective_update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict_cols),
                set_={col: stmt.excluded[col] for col in effective_update_cols},
            )
        else:
            # No columns to update beyond the conflict keys themselves;
            # treat this as "ignore conflicts".
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))

        session.execute(stmt)

        batch_existing = sum(
            1
            for row in chunk
            if _normalize_key_tuple(tuple(row[c] for c in conflict_cols)) in existing_keys
        )
        rows_updated += batch_existing
        rows_added += len(chunk) - batch_existing

    log.debug(
        "Upsert complete",
        table=table.name,
        rows_added=rows_added,
        rows_updated=rows_updated,
    )
    return UpsertCounts(rows_added=rows_added, rows_updated=rows_updated)


def _insert_for_dialect(dialect: str) -> Any:
    if dialect == "postgresql":
        return pg_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise ValueError(f"upsert() only supports sqlite and postgresql; got dialect={dialect!r}")


def _default_update_cols(sample_row: dict[str, Any], conflict_cols: Sequence[str]) -> list[str]:
    skip = {*conflict_cols, "created_at"}
    return [c for c in sample_row if c not in skip]


def _existing_conflict_keys(
    session: Session,
    model: type[Base],
    chunk: list[dict[str, Any]],
    conflict_cols: Sequence[str],
) -> set[tuple[Any, ...]]:
    """Query the table for any rows whose conflict-key tuples are in chunk.

    Returns the set of matching key tuples normalized via
    ``_normalize_key_tuple``. Used to count update vs. insert in the returned
    UpsertCounts. One small SELECT per batch — cheaper than parsing
    dialect-specific RETURNING output, and the counts are advisory (logged,
    not asserted on).
    """

    from sqlalchemy import select, tuple_

    cols = [getattr(model, c) for c in conflict_cols]
    key_tuples = [tuple(row[c] for c in conflict_cols) for row in chunk]
    stmt = select(*cols).where(tuple_(*cols).in_(key_tuples))
    return {_normalize_key_tuple(tuple(r)) for r in session.execute(stmt).all()}


def _normalize_key_tuple(values: tuple[Any, ...]) -> tuple[Any, ...]:
    """Normalize datetimes so SQLite roundtrips compare equal to inputs.

    SQLite's ``DateTime(timezone=True)`` is text-backed and silently drops
    tzinfo on read, so a UTC-aware datetime inserted by the caller comes back
    naive. Coerce both sides to naive-UTC before set membership tests.
    """

    return tuple(_normalize_key_value(v) for v in values)


def _normalize_key_value(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _chunked(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


__all__ = ["UpsertCounts", "upsert"]
