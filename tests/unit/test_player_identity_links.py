"""Tests for the player identity crosswalk read helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nflverse.client import NflversePlayerStat
from ff_pipeline.crawlers.nflverse.runner import _create_stub_players, _gsis_id_to_player_id
from ff_pipeline.crawlers.sleeper.runner import _build_sleeper_to_player_id_map
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Owner,
    Player,
    PlayerIdentityLink,
    PlayerStatsRaw,
    PlayerStatsScored,
    Season,
    Team,
    TeamRoster,
    Transaction,
)
from ff_pipeline.repository.queries import (
    player_identity_cluster,
    player_source_identity_mismatches,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _player(session: Session, name: str, **kwargs: object) -> int:
    row = Player(name_full=name, is_active=True, **kwargs)
    session.add(row)
    session.flush()
    assert isinstance(row.player_id, int)
    return row.player_id


def _team(session: Session, year: int) -> tuple[int, int]:
    league = session.get(League, "league")
    if league is None:
        league = League(league_id="league", name="League", platform="nfl_com")
        session.add(league)
        session.flush()
    owner = Owner(league_id="league", display_name=f"Owner {year}", is_active=True)
    season = Season(league_id="league", year=year, status="completed")
    session.add_all([owner, season])
    session.flush()
    team = Team(season_id=season.season_id, owner_id=owner.owner_id, team_name=f"Team {year}")
    session.add(team)
    session.flush()
    return season.season_id, team.team_id


def test_player_identity_cluster_defaults_to_self(session: Session) -> None:
    player_id = _player(session, "Mike Williams", nfl_com_player_id="2558846")

    assert player_identity_cluster(session, player_id) == {
        "player_id": player_id,
        "canonical_player_id": player_id,
        "member_player_ids": [player_id],
    }


def test_player_identity_cluster_returns_linked_members(session: Session) -> None:
    canonical = _player(session, "Mike Williams", nfl_com_player_id="2558846")
    member = _player(session, "Mike Williams", gsis_id="00-0033536")
    session.add(
        PlayerIdentityLink(
            member_player_id=member,
            canonical_player_id=canonical,
            source="manual",
            confidence="high",
            notes="fixture cross-source split",
        )
    )
    session.flush()

    assert player_identity_cluster(session, member) == {
        "player_id": member,
        "canonical_player_id": canonical,
        "member_player_ids": [canonical, member],
    }
    assert player_identity_cluster(session, canonical) == {
        "player_id": canonical,
        "canonical_player_id": canonical,
        "member_player_ids": [canonical, member],
    }


def test_player_identity_cluster_unknown_player(session: Session) -> None:
    assert player_identity_cluster(session, 999_999) is None


def test_source_identity_mismatch_reports_impossible_nfl_com_owner(session: Session) -> None:
    season_id, team_id = _team(session, 2016)
    wrong = _player(
        session,
        "Les Miller",
        position="NT",
        nfl_com_player_id="2533034",
        gsis_id="old-les",
        rookie_year=1987,
        last_season=1998,
    )
    session.add_all(
        [
            TeamRoster(team_id=team_id, player_id=wrong, season_year=2016, week=0),
            Transaction(
                season_id=season_id,
                transaction_type="draft",
                team_id=team_id,
                player_id=wrong,
            ),
        ]
    )
    session.flush()

    assert player_source_identity_mismatches(session) == [
        {
            "player_id": wrong,
            "name_full": "Les Miller",
            "position": "NT",
            "rookie_year": 1987,
            "last_season": 1998,
            "first_observed_season": 2016,
            "last_observed_season": 2016,
            "nfl_com_player_id": "2533034",
            "gsis_id": "old-les",
            "roster_row_count": 1,
            "transaction_row_count": 1,
            "draft_pick_count": 1,
            "reason": "observed_after_nfl_career_without_stats",
        }
    ]


def test_source_identity_mismatch_allows_exact_late_career_stash(session: Session) -> None:
    season_id, team_id = _team(session, 2021)
    tebow = _player(
        session,
        "Tim Tebow",
        position="QB",
        nfl_com_player_id="497135",
        gsis_id="tebow",
        rookie_year=2010,
        last_season=2012,
    )
    raw = PlayerStatsRaw(
        player_id=tebow,
        season_year=2011,
        week=1,
        source="nflverse",
        is_primary=True,
    )
    session.add(raw)
    session.flush()
    session.add_all(
        [
            Transaction(
                season_id=season_id,
                transaction_type="free_agent_add",
                team_id=team_id,
                player_id=tebow,
            ),
            PlayerStatsScored(
                stat_id=raw.stat_id,
                season_id=season_id,
                player_id=tebow,
                week=1,
                total_points=0.0,
            ),
        ]
    )
    session.flush()

    assert player_source_identity_mismatches(session) == []


def test_nflverse_gsis_map_resolves_linked_member_to_canonical(session: Session) -> None:
    canonical = _player(session, "Mike Williams", nfl_com_player_id="2558846")
    member = _player(session, "Mike Williams", gsis_id="00-0033536")
    session.add(
        PlayerIdentityLink(
            member_player_id=member,
            canonical_player_id=canonical,
            source="manual",
            confidence="high",
        )
    )
    session.flush()

    assert _gsis_id_to_player_id(session, ["00-0033536"]) == {"00-0033536": canonical}


def test_sleeper_map_resolves_linked_member_to_canonical(session: Session) -> None:
    canonical = _player(session, "Mike Williams", sleeper_id="8770", nfl_com_player_id="2558846")
    member = _player(session, "Mike Williams", sleeper_id="4068", gsis_id="00-0033536")
    session.add(
        PlayerIdentityLink(
            member_player_id=member,
            canonical_player_id=canonical,
            source="manual",
            confidence="high",
        )
    )
    session.flush()

    assert _build_sleeper_to_player_id_map(session)["4068"] == canonical
    assert _build_sleeper_to_player_id_map(session)["8770"] == canonical


def _nflverse_stat(gsis_id: str) -> NflversePlayerStat:
    return NflversePlayerStat(
        gsis_id=gsis_id,
        player_display_name="Mike Williams",
        position="WR",
        nfl_team="LAC",
        season_year=2017,
        week=7,
        season_type="REG",
        nfl_opponent="DEN",
        stats={},
    )


def test_reingest_does_not_restrand_linked_member(session: Session) -> None:
    """A crawl that re-sees a linked member's gsis must route its stat row to the
    canonical id and mint no new twin — the split must not reappear on reingest.

    This is the durability guard for the identity-aware ingest: it exercises the
    actual stub-minting path (``_create_stub_players``) twice and asserts both the
    no-new-player invariant and crosswalk stability.
    """
    canonical = _player(session, "Mike Williams", nfl_com_player_id="2558846")
    member = _player(session, "Mike Williams", gsis_id="00-0033536")
    session.add(
        PlayerIdentityLink(
            member_player_id=member,
            canonical_player_id=canonical,
            source="manual",
            confidence="high",
        )
    )
    session.flush()
    players_before = session.query(Player).count()

    stats = [_nflverse_stat("00-0033536")]
    for _ in range(2):  # re-ingest: running the crawl twice must be idempotent
        gsis_map = _gsis_id_to_player_id(session, ["00-0033536"])
        # The member's gsis resolves to the canonical id, so it is never "missing"
        # and no stub is minted.
        assert gsis_map == {"00-0033536": canonical}
        minted = _create_stub_players(session, stats, gsis_map)
        session.flush()
        assert minted == 0
        assert session.query(Player).count() == players_before

    # Crosswalk unchanged: still exactly one link, still member -> canonical.
    links = session.query(PlayerIdentityLink).all()
    assert len(links) == 1
    assert links[0].member_player_id == member
    assert links[0].canonical_player_id == canonical
    # The member's gsis never resolves back to the member itself.
    assert member not in _gsis_id_to_player_id(session, ["00-0033536"]).values()
