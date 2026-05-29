"""Round-trip integration tests for alembic migrations.

Uses a temp-file SQLite database (not :memory:, which loses state across
alembic's per-step connections).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

if TYPE_CHECKING:
    from pathlib import Path

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import (
    current_revision,
    downgrade_to_base,
    upgrade_to_head,
)
from ff_pipeline.repository.models import Base


@pytest.fixture
def temp_db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.mark.integration
def test_migration_creates_every_modeled_table(temp_db_url: str) -> None:
    engine = create_app_engine(temp_db_url)
    try:
        upgrade_to_head(engine=engine)
        inspector = sa.inspect(engine)
        live_tables = set(inspector.get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    expected = set(Base.metadata.tables.keys())
    assert live_tables == expected, f"missing or extra tables: {expected ^ live_tables}"


@pytest.mark.integration
def test_migration_round_trip_up_then_down(temp_db_url: str) -> None:
    engine = create_app_engine(temp_db_url)
    try:
        upgrade_to_head(engine=engine)
        inspector = sa.inspect(engine)
        assert len(inspector.get_table_names()) > 1  # includes alembic_version

        downgrade_to_base(engine=engine)
        inspector = sa.inspect(engine)
        # After full downgrade only alembic_version remains.
        remaining = set(inspector.get_table_names())
        assert remaining <= {"alembic_version"}, f"residual tables: {remaining}"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_migration_is_idempotent(temp_db_url: str) -> None:
    engine = create_app_engine(temp_db_url)
    try:
        upgrade_to_head(engine=engine)
        # Second upgrade should be a no-op (no exception, no table changes).
        upgrade_to_head(engine=engine)
        inspector = sa.inspect(engine)
        live_tables = set(inspector.get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    assert live_tables == set(Base.metadata.tables.keys())


@pytest.mark.integration
def test_current_revision_does_not_raise(temp_db_url: str) -> None:
    engine = create_app_engine(temp_db_url)
    try:
        upgrade_to_head(engine=engine)
        # Should not raise; output goes to stdout via alembic.
        current_revision(engine=engine)
    finally:
        engine.dispose()
