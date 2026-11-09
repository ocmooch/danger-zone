"""Sleeper player resolution (S4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ff_pipeline.normalizer.sleeper_players import league_referenced_ids, resolve
from ff_pipeline.repository.database import Base
from ff_pipeline.repository.models import Player, PlayerIdentityLink

if TYPE_CHECKING:
    from collections.abc import Iterator

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sleeper_league" / "season_2026"
L = "900000000000000001"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as ss:
        ss.add_all(
            [
                Player(
                    player_id=1,
                    name_full="Alpha Quarterback",
                    position="QB",
                    nfl_team="KC",
                    sleeper_id="100",
                ),
                Player(
                    player_id=2,
                    name_full="Bravo Runner",
                    position="RB",
                    nfl_team="SF",
                    gsis_id="00-0000002",
                ),
                Player(
                    player_id=3,
                    name_full="Charlie Dup",
                    position="WR",
                    nfl_team="NE",
                    sleeper_id="300",
                ),
                Player(player_id=4, name_full="Charlie Canonical", position="WR", nfl_team="NE"),
                Player(
                    player_id=10,
                    name_full="Philadelphia Eagles",
                    position="DEF",
                    nfl_team="PHI",
                    sleeper_id="PHI",
                ),
                Player(player_id=11, name_full="Los Angeles Rams", position="DEF", nfl_team="LA"),
                Player(player_id=12, name_full="Las Vegas Raiders", position="DEF", nfl_team="LV"),
                Player(player_id=13, name_full="Jets", position="DEF"),  # legacy: no team
            ]
        )
        ss.add(
            PlayerIdentityLink(
                member_player_id=3, canonical_player_id=4, source="manual", confidence="high"
            )
        )
        ss.commit()
        yield ss
    engine.dispose()


DIRECTORY = {
    "100": {"full_name": "Alpha Quarterback", "position": "QB", "team": "KC"},
    "200": {"full_name": "Bravo Runner", "position": "RB", "team": "SF", "gsis_id": " 00-0000002 "},
    "300": {"full_name": "Charlie Dup", "position": "WR", "team": "NE"},
    "999": {"first_name": "Zulu", "last_name": "Rookie", "position": "WR", "team": "DET"},
    "NYJ": {"first_name": "New York", "last_name": "Jets", "position": "DEF", "team": "NYJ"},
}


def test_resolution_order_links_def_and_stubs(session: Session) -> None:
    referenced = {
        "100": {"rostered"},
        "200": {"transaction"},
        "300": {"matchup"},
        "PHI": {"rostered"},
        "LAR": {"matchup"},
        "OAK": {"draft"},
        "999": {"rostered", "draft"},
        "NYJ": {"matchup"},
    }
    res = resolve(session, referenced, DIRECTORY, season=2026)
    assert res.resolved == {
        "100": 1,
        "200": 2,
        "300": 4,
        "PHI": 10,
        "LAR": 11,
        "OAK": 12,
        "NYJ": 13,
    }
    assert res.linked == 1  # 300 -> member 3 -> canonical 4
    assert res.by_method == {"matched": 3, "def_sleeper_id": 1, "def_franchise": 2, "def_name": 1}
    assert [(c.sleeper_id, c.name, c.position, c.reasons) for c in res.stub_needed] == [
        ("999", "Zulu Rookie", "WR", ("rostered", "draft"))
    ]


def test_resolve_does_not_create_stub_rows(session: Session) -> None:
    before = session.query(Player).count()
    resolve(session, {"999": {"rostered"}}, DIRECTORY, season=2026)
    session.flush()
    assert session.query(Player).count() == before


def test_league_referenced_ids_from_the_2026_fixtures() -> None:
    def load(name: str) -> list[dict[str, object]]:
        data = json.loads((FX / name).read_text())
        assert isinstance(data, list)
        return data

    rosters = load(f"v1_league_{L}_rosters.json")
    matchups = [m for w in range(1, 10) for m in load(f"v1_league_{L}_matchups_{w}.json")]
    txs = [t for w in range(1, 10) for t in load(f"v1_league_{L}_transactions_{w}.json")]
    picks = load("v1_draft_900000000000100001_picks.json")
    ids = league_referenced_ids(rosters, matchups, txs, picks)
    assert "0" not in ids
    defs = {k for k in ids if not k.isdigit()}
    assert defs and all(k.isalpha() and k.isupper() for k in defs)
    assert any("reserve" in why for why in ids.values())
    assert any("draft" in why for why in ids.values())


def test_override_pins_an_unmatched_player(session: Session) -> None:
    from ff_pipeline.normalizer.sleeper_players import OverrideConflictError, add_sleeper_override

    session.add(Player(player_id=20, name_full="Bam Knight", position="RB", nfl_team="ARI"))
    session.commit()
    directory = {"8122": {"full_name": "Zonovan Knight", "position": "RB", "team": "ARI"}}
    assert resolve(session, {"8122": {"rostered"}}, directory, season=2026).stub_needed
    assert add_sleeper_override(session, "8122", 20, notes="nickname") is True
    assert add_sleeper_override(session, "8122", 20) is False
    session.commit()
    res = resolve(session, {"8122": {"rostered"}}, directory, season=2026)
    assert res.resolved == {"8122": 20}
    assert not res.stub_needed
    with pytest.raises(OverrideConflictError):
        add_sleeper_override(session, "8122", 1)
    with pytest.raises(ValueError, match="does not exist"):
        add_sleeper_override(session, "777", 99999)
