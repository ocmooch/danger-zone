"""Application-level guard: ``assert_writable`` refuses sealed seasons with a clear error."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.legacy_guard import (
    SealedSeasonError,
    assert_writable,
    is_unlocked,
    utc_now_text,
)
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import League, LegacyGuard, Season

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        ss.add(League(league_id="L", name="Test", platform="nfl_com"))
        ss.add(Season(season_id=1, league_id="L", year=2015, is_sealed=True, seal_id="seal-v1"))
        ss.add(Season(season_id=2, league_id="L", year=2026))
        ss.commit()
        yield ss
    engine.dispose()


def test_sealed_season_raises_with_year_and_amend_command(session: Session) -> None:
    with pytest.raises(SealedSeasonError) as excinfo:
        assert_writable(session, 1)
    message = str(excinfo.value)
    assert "2015" in message
    assert "seal-v1" in message
    assert "ff-pipeline legacy amend" in message
    assert excinfo.value.year == 2015


def test_unsealed_and_unknown_seasons_are_writable(session: Session) -> None:
    assert_writable(session, 2)
    assert_writable(session, 999)


def test_open_unlock_window_allows_writes(session: Session) -> None:
    future = utc_now_text(datetime.now(tz=UTC) + timedelta(minutes=5))
    session.execute(update(LegacyGuard).where(LegacyGuard.id == 1).values(unlocked_until=future))
    assert is_unlocked(session)
    assert_writable(session, 1)


def test_expired_unlock_window_is_locked(session: Session) -> None:
    past = utc_now_text(datetime.now(tz=UTC) - timedelta(minutes=1))
    session.execute(update(LegacyGuard).where(LegacyGuard.id == 1).values(unlocked_until=past))
    assert not is_unlocked(session)
    with pytest.raises(SealedSeasonError):
        assert_writable(session, 1)
