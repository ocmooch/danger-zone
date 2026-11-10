"""Platform user → owner crosswalk (S4). Pseudonymous data only (public repo)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ff_pipeline.normalizer.owner_identity import (
    CrosswalkError,
    apply,
    load_crosswalk,
    validate,
)
from ff_pipeline.repository.database import Base
from ff_pipeline.repository.models import League, Owner, OwnerPlatformIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

YAML = """
platform: sleeper
league_id: '900000000000000001'
identities:
  - sleeper_user_id: '9000000001'
    sleeper_display_name: Manager 01
    roster_id: 1
    owner_id: 1
    confidence: high
  - sleeper_user_id: '9000000002'
    sleeper_display_name: Manager 02
    roster_id: 2
    owner_id: 2
    confidence: confirmed
"""
USERS = [{"user_id": "9000000001"}, {"user_id": "9000000002"}]
ROSTERS: list[dict[str, Any]] = [
    {"roster_id": 1, "owner_id": "9000000001", "co_owners": None},
    {"roster_id": 2, "owner_id": "9000000002", "co_owners": None},
]


def _cw(tmp_path: Path, text: str = YAML) -> Any:
    p = tmp_path / "cw.yaml"
    p.write_text(text)
    return load_crosswalk(p)


def test_valid_crosswalk_has_no_problems(tmp_path: Path) -> None:
    assert validate(_cw(tmp_path), USERS, ROSTERS, {1, 2}) == []


def test_unmapped_user_fails_with_a_stanza(tmp_path: Path) -> None:
    users = [*USERS, {"user_id": "9000000003", "display_name": "Manager 03"}]
    rosters = [*ROSTERS, {"roster_id": 3, "owner_id": "9000000003"}]
    problems = validate(_cw(tmp_path), users, rosters, {1, 2})
    assert len(problems) == 1
    assert "9000000003 is not mapped" in problems[0]
    assert "roster_id: 3" in problems[0]
    assert "owner_id: <existing" in problems[0]


def test_owner_mapped_twice_fails(tmp_path: Path) -> None:
    text = YAML.replace("owner_id: 2", "owner_id: 1")
    assert any(
        "owner_id 1 is mapped to 2" in p
        for p in validate(_cw(tmp_path, text), USERS, ROSTERS, {1, 2})
    )


def test_roster_mismatch_fails(tmp_path: Path) -> None:
    rosters = [
        {"roster_id": 1, "owner_id": "9000000002"},
        {"roster_id": 2, "owner_id": "9000000001"},
    ]
    problems = validate(_cw(tmp_path), USERS, rosters, {1, 2})
    assert sum("roster_id" in p for p in problems) == 2


def test_unmapped_co_owner_fails(tmp_path: Path) -> None:
    rosters = [{**ROSTERS[0], "co_owners": ["9000000099"]}, ROSTERS[1]]
    assert any("co-owner 9000000099" in p for p in validate(_cw(tmp_path), USERS, rosters, {1, 2}))


def test_medium_confidence_and_unknown_owner_fail(tmp_path: Path) -> None:
    text = YAML.replace("confidence: high", "confidence: medium")
    problems = validate(_cw(tmp_path, text), USERS, ROSTERS, {2})
    assert any("confidence 'medium'" in p for p in problems)
    assert any("owner_id 1 is not in owners" in p for p in problems)


def test_malformed_files_raise(tmp_path: Path) -> None:
    with pytest.raises(CrosswalkError, match="unknown keys"):
        _cw(tmp_path, YAML.replace("confidence: high", "confidence: high\n    surprise: 1"))
    with pytest.raises(CrosswalkError, match="identities"):
        _cw(tmp_path, "platform: sleeper\n")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as ss:
        ss.add(League(league_id="L", name="Fixture League", platform="nfl_com"))
        ss.add_all(
            [
                Owner(owner_id=1, league_id="L", display_name="Owner One", is_active=True),
                Owner(owner_id=2, league_id="L", display_name="Owner Two", is_active=True),
            ]
        )
        ss.commit()
        yield ss
    engine.dispose()


def test_apply_is_idempotent_and_widens_seasons(tmp_path: Path, session: Session) -> None:
    cw = _cw(tmp_path)
    r1 = apply(session, cw, season=2026, league_id="L")
    session.commit()
    r2 = apply(session, cw, season=2027, league_id="L")
    session.commit()
    assert (r1.inserted, r1.updated, r2.inserted, r2.updated) == (2, 0, 0, 2)
    rows = session.scalars(
        select(OwnerPlatformIdentity).order_by(OwnerPlatformIdentity.owner_id)
    ).all()
    assert [(r.owner_id, r.external_user_id, r.first_season, r.last_season) for r in rows] == [
        (1, "9000000001", 2026, 2027),
        (2, "9000000002", 2026, 2027),
    ]


def test_new_owner_entry_creates_an_owner(tmp_path: Path, session: Session) -> None:
    text = (
        YAML
        + """  - sleeper_user_id: '9000000003'
    new_owner: true
    owner_display_name: Owner Three
    confidence: confirmed
"""
    )
    cw = _cw(tmp_path, text)
    assert validate(cw, [*USERS, {"user_id": "9000000003"}], ROSTERS, {1, 2}) == []
    result = apply(session, cw, season=2026, league_id="L")
    session.commit()
    assert result.new_owners == 1
    assert session.scalars(select(Owner).where(Owner.display_name == "Owner Three")).one()
