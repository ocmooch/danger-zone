"""Integration test for the ADP crawler end-to-end.

Drives ``run_adp`` against ``LocalFixtureAdpSource`` (no network) and a fresh
sqlite DB built via the alembic migrations. Verifies:

* Raw per-source rows land in ``player_adp`` (FFC + MFL), keyed by
  ``(season, source, source_player_key)``.
* Source players resolve to canonical ``player_id`` by name+position, with the
  era guard disambiguating same-name players (two "Mike Williams").
* An unresolvable source player is still stored (``player_id`` NULL) and counted
  as unresolved — never silently dropped, never mis-assigned.
* ``pipeline_runs`` + per-source ``source_health`` bookkeeping is written.
* The read-only ``player_adp_rows_for_season`` helper returns one entry per
  source for a player the dashboard can blend.
* A season whose target format is unavailable falls back **loudly**
  (``format_fallback=True``, ``actual_format`` recorded).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.adp.endpoints import LocalFixtureAdpSource
from ff_pipeline.crawlers.adp.format_map import FULL_PPR, STANDARD
from ff_pipeline.crawlers.adp.runner import run_adp
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import League, PlayerAdp, Season, SourceHealth
from ff_pipeline.repository.queries import player_adp_rows_for_season

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ff_pipeline.repository.models import Player

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "adp"
LEAGUE_ID = "TEST-LEAGUE"


@pytest.fixture
def db_engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_app_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade_to_head(engine=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    with Session(db_engine) as ss:
        yield ss


def _seed(session: Session) -> dict[str, int]:
    from ff_pipeline.repository.models import Player

    session.add(League(league_id=LEAGUE_ID, name="Test League", platform="nfl_com"))
    session.flush()
    for year in (2010, 2015):
        session.add(Season(league_id=LEAGUE_ID, year=year))
    session.flush()

    def player(name: str, pos: str, rookie: int, last: int) -> Player:
        p = Player(name_full=name, position=pos, rookie_year=rookie, last_season=last)
        session.add(p)
        session.flush()
        return p

    ids = {
        "ab": player("Antonio Brown", "WR", 2010, 2021).player_id,
        "ap": player("Adrian Peterson", "RB", 2007, 2021).player_id,
        # Two same-name players; only the 2010-2017 one is active in 2015.
        "mw_old": player("Mike Williams", "WR", 2010, 2017).player_id,
        "mw_new": player("Mike Williams", "WR", 2017, 2024).player_id,
    }
    session.commit()
    return ids


def _sources() -> list[LocalFixtureAdpSource]:
    return [
        LocalFixtureAdpSource(name="ffc", directory=FIXTURE_DIR),
        LocalFixtureAdpSource(name="mfl", directory=FIXTURE_DIR),
    ]


def test_adp_run_stores_and_resolves(session: Session) -> None:
    ids = _seed(session)
    result = run_adp(session, league_id=LEAGUE_ID, year=2015, sources=_sources())
    session.commit()

    by_source = {o.source: o for o in result.outcomes}
    assert by_source["ffc"].status == "success"
    assert by_source["ffc"].actual_format == FULL_PPR
    assert by_source["ffc"].format_fallback is False
    # FFC fixture: 4 rows, 3 resolve (Brown, Peterson, Mike Williams), 1 unresolved.
    assert by_source["ffc"].matched == 3
    assert by_source["ffc"].unresolved == 1
    # MFL fixture: 2 rows, both resolve (comma name handled).
    assert by_source["mfl"].matched == 2
    assert by_source["mfl"].unresolved == 0

    # The unresolved FFC row is stored with a NULL player_id (kept, not dropped).
    total = session.execute(select(func.count()).select_from(PlayerAdp)).scalar_one()
    assert total == 6
    null_rows = session.execute(
        select(func.count()).select_from(PlayerAdp).where(PlayerAdp.player_id.is_(None))
    ).scalar_one()
    assert null_rows == 1

    # Era guard: the 2015 "Mike Williams" maps to the 2010-2017 player, not the later one.
    mw_row = session.execute(
        select(PlayerAdp).where(PlayerAdp.source == "ffc", PlayerAdp.source_player_key == "1003")
    ).scalar_one()
    assert mw_row.player_id == ids["mw_old"]


def test_queries_helper_groups_sources_per_player(session: Session) -> None:
    ids = _seed(session)
    run_adp(session, league_id=LEAGUE_ID, year=2015, sources=_sources())
    session.commit()

    season_id = session.execute(select(Season.season_id).where(Season.year == 2015)).scalar_one()
    rows = player_adp_rows_for_season(session, season_id)

    # Antonio Brown has both an FFC and an MFL row → blendable downstream.
    ab_sources = {r.source for r in rows[ids["ab"]]}
    assert ab_sources == {"ffc", "mfl"}
    # The unresolved player is absent from the per-player map (audit-only).
    assert all(pid is not None for pid in rows)


def test_loud_format_fallback_when_target_unavailable(session: Session) -> None:
    _seed(session)
    # 2010 target is half-PPR; only an FFC *standard* fixture exists, so the
    # runner must walk half → full → standard and flag the substitution.
    result = run_adp(session, league_id=LEAGUE_ID, year=2010, sources=_sources())
    session.commit()

    ffc = next(o for o in result.outcomes if o.source == "ffc")
    assert ffc.actual_format == STANDARD
    assert ffc.format_fallback is True
    assert ffc.requested_format == "half_ppr"

    stored = (
        session.execute(
            select(PlayerAdp).where(PlayerAdp.source == "ffc", PlayerAdp.actual_format == STANDARD)
        )
        .scalars()
        .all()
    )
    assert stored and all(r.format_fallback for r in stored)


def test_bookkeeping_written(session: Session) -> None:
    _seed(session)
    run_adp(session, league_id=LEAGUE_ID, year=2015, sources=_sources())
    session.commit()

    health = (
        session.execute(select(SourceHealth).where(SourceHealth.source.like("adp:%")))
        .scalars()
        .all()
    )
    sources = {h.source for h in health}
    assert sources == {"adp:ffc", "adp:mfl"}
    ffc_health = next(h for h in health if h.source == "adp:ffc")
    assert ffc_health.status == "success"
    assert ffc_health.parse_failures == 1  # the unresolved FFC row
