"""Unit tests for ``ff_pipeline.repository.upsert``.

Uses an in-memory SQLite engine + the project's actual ORM models, so the
helper is exercised against the same column types it'll face at runtime
(JSON, DateTime, Float). Two-pass insert→update tests confirm both the
on-conflict semantics and the rows_added / rows_updated bookkeeping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import Base
from ff_pipeline.repository.models import League, Player
from ff_pipeline.repository.upsert import upsert

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def test_upsert_inserts_then_updates(session: Session) -> None:
    rows = [
        {"gsis_id": "00-A", "name_full": "Player A", "position": "QB", "is_active": True},
        {"gsis_id": "00-B", "name_full": "Player B", "position": "RB", "is_active": True},
    ]
    first = upsert(session, Player, rows, conflict_cols=("gsis_id",))
    assert first.rows_added == 2
    assert first.rows_updated == 0

    # Second pass: one row updated (new position), one new row added.
    rows[0]["position"] = "WR"
    rows.append({"gsis_id": "00-C", "name_full": "Player C", "position": "TE", "is_active": True})
    second = upsert(session, Player, rows, conflict_cols=("gsis_id",))
    assert second.rows_added == 1
    assert second.rows_updated == 2

    a_row = session.execute(select(Player).where(Player.gsis_id == "00-A")).scalar_one()
    assert a_row.position == "WR"


def test_upsert_empty_input_is_noop(session: Session) -> None:
    counts = upsert(session, Player, [], conflict_cols=("gsis_id",))
    assert counts.rows_added == 0
    assert counts.rows_updated == 0


def test_upsert_explicit_update_cols(session: Session) -> None:
    """When update_cols is explicit, other columns must NOT be overwritten."""

    upsert(
        session,
        Player,
        [{"gsis_id": "00-X", "name_full": "Original Name", "position": "QB", "is_active": True}],
        conflict_cols=("gsis_id",),
    )
    upsert(
        session,
        Player,
        [{"gsis_id": "00-X", "name_full": "New Name", "position": "RB", "is_active": True}],
        conflict_cols=("gsis_id",),
        update_cols=("position",),
    )
    row = session.execute(select(Player).where(Player.gsis_id == "00-X")).scalar_one()
    assert row.name_full == "Original Name"
    assert row.position == "RB"


def test_upsert_empty_update_cols_does_nothing_on_conflict(session: Session) -> None:
    """An explicit empty update_cols seeds on insert but never clobbers.

    Regression guard: ``_upsert_season`` relies on this to seed a new
    season as ``in_progress`` without regressing a reconstructed
    ``completed`` season on a later re-sync.
    """

    upsert(
        session,
        Player,
        [{"gsis_id": "00-Y", "name_full": "Seeded", "position": "QB", "is_active": True}],
        conflict_cols=("gsis_id",),
        update_cols=(),
    )
    # Conflicting re-upsert with different values must be ignored entirely.
    upsert(
        session,
        Player,
        [{"gsis_id": "00-Y", "name_full": "Clobber", "position": "RB", "is_active": False}],
        conflict_cols=("gsis_id",),
        update_cols=(),
    )
    row = session.execute(select(Player).where(Player.gsis_id == "00-Y")).scalar_one()
    assert row.name_full == "Seeded"
    assert row.position == "QB"
    assert row.is_active is True


def test_upsert_into_league_table_uses_string_pk(session: Session) -> None:
    """Conflict key can be a primary key, not just a UNIQUE constraint."""

    upsert(
        session,
        League,
        [{"league_id": "1", "name": "L1", "platform": "nfl_com"}],
        conflict_cols=("league_id",),
    )
    upsert(
        session,
        League,
        [{"league_id": "1", "name": "L1 renamed", "platform": "nfl_com"}],
        conflict_cols=("league_id",),
    )
    row = session.execute(select(League).where(League.league_id == "1")).scalar_one()
    assert row.name == "L1 renamed"
