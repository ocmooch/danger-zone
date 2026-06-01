"""Tests for league-scope player filtering + orphan pruning.

Two refinements work together:

* The nflverse runner's ``_filter_relevant_players`` stops pre-league-era
  and unrosterable player *metadata* from being ingested in the first place.
* ``prune_orphan_players`` removes rows that already landed and have no
  connection to any league fact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nflverse.client import NflversePlayerMeta
from ff_pipeline.crawlers.nflverse.runner import _filter_relevant_players
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import Player, PlayerStatsRaw
from ff_pipeline.repository.prune import prune_orphan_players

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

RELEVANT = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})


def _meta(
    gsis: str,
    *,
    name: str,
    position: str | None,
    last_season: int | None,
) -> NflversePlayerMeta:
    return NflversePlayerMeta(
        gsis_id=gsis,
        name_full=name,
        name_first=None,
        name_last=None,
        position=position,
        nfl_team=None,
        birth_date=None,
        rookie_year=None,
        last_season=last_season,
        espn_id=None,
        status="ACT",
    )


# ---------------------------------------------------------------------------
# Ingestion filter
# ---------------------------------------------------------------------------


def test_filter_keeps_modern_skill_player() -> None:
    kept = _filter_relevant_players(
        [_meta("1", name="Active WR", position="WR", last_season=2025)],
        league_start_year=2010,
        relevant_positions=RELEVANT,
    )
    assert [m.gsis_id for m in kept] == ["1"]


def test_filter_drops_pre_league_era_player() -> None:
    kept = _filter_relevant_players(
        [_meta("2", name="Retired QB", position="QB", last_season=2007)],
        league_start_year=2010,
        relevant_positions=RELEVANT,
    )
    assert kept == []


def test_filter_drops_unrosterable_position() -> None:
    kept = _filter_relevant_players(
        [_meta("3", name="Some Linebacker", position="LB", last_season=2024)],
        league_start_year=2010,
        relevant_positions=RELEVANT,
    )
    assert kept == []


def test_filter_keeps_unknown_last_season_and_position() -> None:
    # NULL last_season / position means "unknown" — keep on no positive
    # evidence of irrelevance.
    kept = _filter_relevant_players(
        [
            _meta("4", name="No Era", position="RB", last_season=None),
            _meta("5", name="No Position", position=None, last_season=2024),
        ],
        league_start_year=2010,
        relevant_positions=RELEVANT,
    )
    assert {m.gsis_id for m in kept} == {"4", "5"}


def test_filter_noop_when_both_unset() -> None:
    metas = [_meta("6", name="Old DB", position="DB", last_season=2001)]
    kept = _filter_relevant_players(metas, league_start_year=None, relevant_positions=None)
    assert kept == metas


# ---------------------------------------------------------------------------
# Orphan prune
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'prune.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def test_prune_deletes_only_orphans(session: Session) -> None:
    referenced = Player(name_full="Rostered Star", position="WR", is_active=True)
    orphan = Player(name_full="Pre-2010 Lineman", position="OT", is_active=False)
    session.add_all([referenced, orphan])
    session.flush()

    # Anchor `referenced` to a stat row so it is no longer an orphan.
    session.add(
        PlayerStatsRaw(
            player_id=referenced.player_id,
            season_year=2024,
            week=1,
            source="nflverse",
            stats={"receiving_yards": 100},
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
        )
    )
    session.flush()

    preview = prune_orphan_players(session, dry_run=True)
    assert preview.orphans_found == 1
    assert preview.deleted == 0
    assert preview.by_position == {"OT": 1}
    # Dry run mutated nothing.
    assert session.execute(select(Player)).all() != []
    assert len(session.execute(select(Player)).all()) == 2

    result = prune_orphan_players(session, dry_run=False)
    session.flush()
    assert result.deleted == 1

    remaining = session.execute(select(Player.player_id)).scalars().all()
    assert remaining == [referenced.player_id]


def test_prune_reports_nothing_when_clean(session: Session) -> None:
    star = Player(name_full="Rostered Star", position="QB")
    session.add(star)
    session.flush()
    session.add(
        PlayerStatsRaw(
            player_id=star.player_id,
            season_year=2024,
            week=1,
            source="nflverse",
            stats={"passing_yards": 300},
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
        )
    )
    session.flush()

    result = prune_orphan_players(session, dry_run=False)
    assert result.orphans_found == 0
    assert result.deleted == 0
