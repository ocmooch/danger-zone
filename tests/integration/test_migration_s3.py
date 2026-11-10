"""S3 platform-era schema: additive, trigger-safe both ways, indexes behave (S3)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.legacy.verify import connect, verify
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import _fk_disabled_for_sqlite, _make_config, upgrade_to_head
from ff_pipeline.repository.models import (
    LegacyNflCom2026Preseason,
    OwnerPlatformIdentity,
    PlayerStatusSnapshot,
    PlayoffBracket,
    Season,
    TradedPick,
)
from tests.integration.test_legacy_guard import _all_fingerprints
from tests.integration.test_legacy_guard import _build as build_sealed_db

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

S2_HEAD = "a7c9e1b3d5f2"
NEW_TABLES = {
    "owner_platform_identities",
    "playoff_brackets",
    "traded_picks",
    "player_status_snapshots",
    "legacy_nfl_com_2026_preseason",
}
NEW_COLUMNS = {
    "seasons": {"platform", "platform_league_id", "platform_draft_id", "settings_snapshot"},
    "teams": {"platform_team_key", "division", "extra_data"},
    "matchups": {"platform_matchup_key", "extra_data"},
    "transactions": {"platform_transaction_id", "status"},
    "players": {"extra_data"},
}


def _q(db: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _triggers(db: Path) -> set[str]:
    return {str(r[0]) for r in _q(db, "SELECT name FROM sqlite_master WHERE type='trigger'")}


def _tables(db: Path) -> set[str]:
    return {str(r[0]) for r in _q(db, "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: Path, table: str) -> set[str]:
    return {str(r[1]) for r in _q(db, f"PRAGMA table_info({table})")}


def _verified(db: Path) -> bool:
    conn = connect(str(db))
    try:
        return verify(conn, full=True).ok
    finally:
        conn.close()


def _downgrade(db: Path, target: str) -> None:
    engine = create_app_engine(f"sqlite:///{db}")
    try:
        cfg = _make_config()
        with engine.connect() as connection, _fk_disabled_for_sqlite(connection):
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, target)
    finally:
        engine.dispose()


def _upgrade(db: Path) -> None:
    engine = create_app_engine(f"sqlite:///{db}")
    try:
        upgrade_to_head(engine=engine)
    finally:
        engine.dispose()


def test_upgrade_is_additive_on_a_sealed_db(tmp_path: Path) -> None:
    db = build_sealed_db(tmp_path)  # migrated to head (incl. S3), 2015 sealed
    assert NEW_TABLES.issubset(_tables(db))
    for table, cols in NEW_COLUMNS.items():
        assert cols <= _columns(db, table), table
    assert _q(db, "SELECT DISTINCT platform FROM seasons") == [("nfl_com",)]
    for table, cols in NEW_COLUMNS.items():
        for col in cols - {"platform"}:
            assert _q(db, f"SELECT count(*) FROM {table} WHERE {col} IS NOT NULL") == [(0,)]
    assert _verified(db)


def test_round_trip_keeps_triggers_fingerprints_and_the_guard(tmp_path: Path) -> None:
    db = build_sealed_db(tmp_path)
    triggers, prints = _triggers(db), _all_fingerprints(db)
    # Guard v2 (S9) triggers sit on S3 columns, so downgrading past S3 drops them by design.
    v1_triggers = {t for t in triggers if not t.startswith("legacy_guard_v2_")}
    assert len(v1_triggers) == 41
    assert len(triggers - v1_triggers) == 7

    _downgrade(db, S2_HEAD)
    assert not (NEW_TABLES & _tables(db))
    for table, cols in NEW_COLUMNS.items():
        assert not (cols & _columns(db, table)), table
    assert _triggers(db) == v1_triggers  # no batch rebuild dropped a v1 guard trigger
    assert _all_fingerprints(db) == prints
    assert _verified(db)

    _upgrade(db)
    assert _triggers(db) == triggers
    assert _all_fingerprints(db) == prints
    assert _verified(db)

    c = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="legacy_guard"):
            c.execute("UPDATE matchups SET team_score = 0 WHERE matchup_id = 1")
    finally:
        c.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return build_sealed_db(tmp_path)


def test_platform_team_key_is_unique_per_season_but_null_is_free(db: Path) -> None:
    c = sqlite3.connect(db)
    try:
        # season 2 (2016) is open; NULL keys on legacy teams never collide
        c.execute("UPDATE teams SET platform_team_key = 'sleeper:roster:1' WHERE team_id = 3")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            c.execute("UPDATE teams SET platform_team_key = 'sleeper:roster:1' WHERE team_id = 4")
        c.execute("UPDATE teams SET platform_team_key = 'sleeper:roster:2' WHERE team_id = 4")
    finally:
        c.close()


def test_platform_transaction_legs_dedupe_including_null_player(db: Path) -> None:
    c = sqlite3.connect(db)
    leg = (
        "INSERT INTO transactions (season_id, transaction_type, team_id, player_id, direction,"
        " platform_transaction_id, status) VALUES (2, 'trade', ?, ?, ?, 'tx1', 'complete')"
    )
    try:
        c.execute(leg, (3, 2, "in"))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            c.execute(leg, (3, 2, "in"))
        c.execute(leg, (4, 2, "out"))  # other side of the trade: fine
        c.execute(leg, (3, None, "in"))  # FAAB leg, no player
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            c.execute(leg, (3, None, "in"))
        # NFL.com-style rows (no platform id) may repeat, as before S3
        nfl = "INSERT INTO transactions (season_id, transaction_type, team_id) VALUES (2, 'add', 3)"
        c.execute(nfl)
        c.execute(nfl)
    finally:
        c.close()


def test_new_tables_round_trip_through_the_orm(db: Path) -> None:
    engine = create_app_engine(f"sqlite:///{db}")
    now = datetime(2026, 11, 9, tzinfo=UTC)
    try:
        with Session(engine) as ss:
            ss.add_all(
                [
                    OwnerPlatformIdentity(
                        owner_id=1,
                        platform="sleeper",
                        external_user_id="9000000001",
                        first_season=2026,
                    ),
                    PlayoffBracket(
                        season_id=2,
                        bracket="winners",
                        round=1,
                        match_no=1,
                        team1_id=3,
                        team2_id=4,
                        placement=None,
                        team2_from={"w": 1},
                        source="sleeper",
                    ),
                    TradedPick(
                        league_id="L",
                        pick_season=2027,
                        round=2,
                        original_owner_id=1,
                        current_owner_id=2,
                        extra_data={"roster_id": 1, "owner_id": 2},
                    ),
                    PlayerStatusSnapshot(
                        player_id=1,
                        as_of_date=date(2026, 11, 9),
                        source="sleeper",
                        injury_status="Q",
                    ),
                    LegacyNflCom2026Preseason(
                        source_table="teams",
                        original_pk="3",
                        row_data={"team_id": 3, "team_name": "A16"},
                        quarantined_at=now,
                    ),
                ]
            )
            ss.commit()
            season = ss.scalars(select(Season).where(Season.year == 2015)).one()
            assert season.platform == "nfl_com"
            assert ss.scalars(select(PlayoffBracket)).one().team2_from == {"w": 1}
            assert (
                ss.scalars(select(LegacyNflCom2026Preseason)).one().row_data["team_name"] == "A16"
            )
            ss.add(
                OwnerPlatformIdentity(owner_id=2, platform="sleeper", external_user_id="9000000001")
            )
            with pytest.raises(Exception, match="UNIQUE"):
                ss.commit()
    finally:
        engine.dispose()
