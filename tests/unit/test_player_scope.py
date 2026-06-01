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

from ff_pipeline.crawlers.nflverse.client import NflversePlayerMeta, NflversePlayerStat
from ff_pipeline.crawlers.nflverse.runner import (
    _create_stub_players,
    _filter_relevant_players,
)
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Owner,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    Projection,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.repository.prune import (
    find_irrelevant_position_players,
    prune_irrelevant_position_players,
    prune_orphan_players,
)

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


# ---------------------------------------------------------------------------
# Stub-creation position gate (prevents IDP regrowth)
# ---------------------------------------------------------------------------


def _stat(gsis: str, *, position: str | None) -> NflversePlayerStat:
    return NflversePlayerStat(
        gsis_id=gsis,
        player_display_name=f"Player {gsis}",
        position=position,
        nfl_team="KC",
        season_year=2024,
        week=1,
        season_type="REG",
        nfl_opponent="DEN",
        stats={"passing_yards": 1.0},
    )


def test_stub_skips_irrelevant_position(session: Session) -> None:
    stats = [
        _stat("skill", position="WR"),
        _stat("idp", position="LB"),
        _stat("unknown", position=None),
    ]
    added = _create_stub_players(session, stats, {}, relevant_positions=RELEVANT)
    session.flush()
    positions = {p.gsis_id: p.position for p in session.execute(select(Player)).scalars().all()}
    # WR stubbed, NULL-position stubbed (unknown -> keep), LB skipped.
    assert added == 2
    assert positions == {"skill": "WR", "unknown": None}


def test_stub_stubs_everything_when_unscoped(session: Session) -> None:
    stats = [_stat("idp", position="LB")]
    added = _create_stub_players(session, stats, {}, relevant_positions=None)
    session.flush()
    assert added == 1


# ---------------------------------------------------------------------------
# Irrelevant-position prune (cascade + protective tables)
# ---------------------------------------------------------------------------


def _scaffold(session: Session) -> tuple[int, int]:
    """Create League/Season/Owner/Team scaffolding; return ``(season_id, team_id)``."""
    session.add(League(league_id="L1", name="Test"))
    season = Season(league_id="L1", year=2024)
    owner = Owner(league_id="L1", display_name="Me")
    session.add_all([season, owner])
    session.flush()
    team = Team(season_id=season.season_id, owner_id=owner.owner_id, team_name="T1")
    session.add(team)
    session.flush()
    return season.season_id, team.team_id


def _rostered(session: Session, player_id: int) -> None:
    """Anchor ``player_id`` to a real roster row via minimal scaffolding."""
    _season_id, team_id = _scaffold(session)
    session.add(TeamRoster(team_id=team_id, player_id=player_id, season_year=2024, week=1))
    session.flush()


def test_irrelevant_position_prune_cascades(session: Session) -> None:
    season_id, _team_id = _scaffold(session)
    idp = Player(name_full="A.J. Lineman", position="OT")
    skill = Player(name_full="Active WR", position="WR")
    session.add_all([idp, skill])
    session.flush()

    # Incidental references on the IDP — these must cascade-delete with it.
    # A scored row chained to the raw row (FK on stat_id) guards the deletion
    # ordering: scored must be deleted before its raw parent.
    raw = PlayerStatsRaw(
        player_id=idp.player_id,
        season_year=2024,
        week=1,
        source="nflverse",
        stats={"x": 0},
        is_primary=True,
        ingested_at=datetime.now(tz=UTC),
    )
    session.add(raw)
    session.flush()
    session.add(
        PlayerStatsScored(
            stat_id=raw.stat_id,
            season_id=season_id,
            player_id=idp.player_id,
            week=1,
            total_points=0.0,
        )
    )
    session.add(
        Projection(
            player_id=idp.player_id,
            season_year=2024,
            week=1,
            source="sleeper",
            projected_points=0.0,
            fetched_at=datetime.now(tz=UTC),
        )
    )
    # A skill player with a stat row is untouched.
    session.add(
        PlayerStatsRaw(
            player_id=skill.player_id,
            season_year=2024,
            week=1,
            source="nflverse",
            stats={"receiving_yards": 50},
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
        )
    )
    session.flush()

    preview = prune_irrelevant_position_players(session, relevant_positions=RELEVANT, dry_run=True)
    assert preview.players_found == 1
    assert preview.by_position == {"OT": 1}
    assert preview.cascade_deleted == {
        "player_stats_raw": 1,
        "player_stats_scored": 1,
        "projections": 1,
        "trending_players": 0,
    }
    # Dry run mutated nothing.
    assert len(session.execute(select(Player)).all()) == 2

    result = prune_irrelevant_position_players(session, relevant_positions=RELEVANT, dry_run=False)
    session.flush()
    assert result.players_deleted == 1
    assert result.cascade_deleted["player_stats_raw"] == 1
    assert result.cascade_deleted["player_stats_scored"] == 1
    assert result.cascade_deleted["projections"] == 1

    remaining = session.execute(select(Player.player_id)).scalars().all()
    assert remaining == [skill.player_id]
    # The IDP's incidental rows are gone; the skill player's stat row stayed.
    assert len(session.execute(select(PlayerStatsRaw)).all()) == 1
    assert session.execute(select(PlayerStatsScored)).all() == []
    assert session.execute(select(Projection)).all() == []


def test_irrelevant_position_prune_protects_rostered(session: Session) -> None:
    # An "IDP-positioned" player that was actually rostered (mislabeled
    # position, fullback, two-way player...) must survive.
    rostered = Player(name_full="Kyle Juszczyk", position="FB")
    session.add(rostered)
    session.flush()
    _rostered(session, rostered.player_id)

    found = find_irrelevant_position_players(session, RELEVANT)
    assert found == []

    result = prune_irrelevant_position_players(session, relevant_positions=RELEVANT, dry_run=False)
    assert result.players_deleted == 0
    assert session.execute(select(Player.player_id)).scalars().all() == [rostered.player_id]


def test_irrelevant_position_prune_keeps_unknown_position(session: Session) -> None:
    blank = Player(name_full="No Position", position=None)
    empty = Player(name_full="Empty Position", position="  ")
    session.add_all([blank, empty])
    session.flush()

    found = find_irrelevant_position_players(session, RELEVANT)
    assert found == []
