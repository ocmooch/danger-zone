"""The Sleeper projection upsert skips hollow (all-zero) rows.

Sleeper returns full rosters of all-zero projection rows for pre-coverage
seasons and for unprojected players; persisting them advertises coverage the
source never had. ``_upsert_projections`` must drop them and keep real ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.sleeper.endpoints import SleeperProjection
from ff_pipeline.crawlers.sleeper.runner import _is_hollow_projection, _upsert_projections
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import Player, Projection

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'proj.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def test_is_hollow_projection() -> None:
    assert _is_hollow_projection({"rush_yd": 0.0, "rec_yd": 0.0}, 0.0) is True
    assert _is_hollow_projection({}, None) is True
    assert _is_hollow_projection(None, 0.0) is True
    assert _is_hollow_projection({"rush_yd": 57.4}, None) is False  # stats-only real
    assert _is_hollow_projection({"rush_yd": 0.0}, 12.3) is False  # scored real


def test_upsert_projections_skips_hollow_rows(session: Session) -> None:
    hollow_player = Player(name_full="Hollow Henry", is_active=True)
    real_player = Player(name_full="Real Romo", is_active=True)
    session.add_all([hollow_player, real_player])
    session.flush()

    projections = [
        SleeperProjection(
            sleeper_id="hollow",
            season_year=2017,
            week=7,
            season_type="regular",
            stats={"rush_yd": 0.0, "rec_yd": 0.0, "pass_yd": 0.0},
        ),
        SleeperProjection(
            sleeper_id="real",
            season_year=2023,
            week=7,
            season_type="regular",
            stats={"rush_yd": 88.6, "rec_yd": 12.0},
        ),
    ]
    _upsert_projections(
        session,
        projections,
        sleeper_to_player_id={
            "hollow": int(hollow_player.player_id),
            "real": int(real_player.player_id),
        },
        scoring_rules=None,
    )
    session.flush()

    kept = session.execute(select(Projection.player_id, Projection.season_year)).all()
    assert kept == [(int(real_player.player_id), 2023)]  # only the real row persisted
    assert int(session.execute(select(func.count(Projection.projection_id))).scalar_one()) == 1
