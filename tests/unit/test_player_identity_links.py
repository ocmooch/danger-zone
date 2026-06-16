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
from ff_pipeline.repository.models import Player, PlayerIdentityLink
from ff_pipeline.repository.queries import player_identity_cluster

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
