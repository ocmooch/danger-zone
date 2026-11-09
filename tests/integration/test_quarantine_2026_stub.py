"""Quarantine of the NFL.com 2026 placeholder (D-2; S4)."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session

from ff_pipeline.legacy.quarantine import quarantine_2026_stub
from ff_pipeline.legacy.seal import LegacyError
from ff_pipeline.legacy.verify import connect, verify
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from tests.integration.test_legacy_guard import _all_fingerprints
from tests.integration.test_legacy_guard import _build as build_sealed_db

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

LEAGUE = "900000000000000001"
STUB = """
INSERT INTO seasons (season_id, league_id, year, status) VALUES (3, 'L', 2026, 'in_progress');
INSERT INTO teams (team_id, season_id, owner_id, team_name, final_rank, regular_season_points_for)
    VALUES (5, 3, 1, 'A26', NULL, 0.0), (6, 3, 2, 'B26 "quoted"', NULL, NULL);
INSERT INTO team_rosters (roster_id, team_id, player_id, season_year, week, roster_slot, is_starter, extra_data)
    VALUES (10, 5, 1, 2026, 1, 'QB', 1, '{"nfl_com_points": 12.34}'),
           (11, 5, 2, 2026, 1, 'BN', 0, NULL),
           (12, 6, 3, 2026, 1, 'R/W/T', 1, NULL);
UPDATE players SET last_rostered_season = 2026 WHERE player_id IN (1, 2, 3);
"""


def _rows(db: Path, sql: str) -> list[dict[str, Any]]:
    c = sqlite3.connect(db)
    try:
        cur = c.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        c.close()


def _sealed_prints(db: Path) -> dict[str, Any]:
    """Per-table fingerprints of the sealed season only (whole-table hashes move with 2026)."""
    return {t: (v.get("by_season") or {}).get("2015") for t, v in _all_fingerprints(db).items()}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = build_sealed_db(tmp_path)  # 2015 sealed, 2016 open
    c = sqlite3.connect(path)
    try:
        c.executescript(STUB)
    finally:
        c.close()
    return path


@pytest.fixture
def conn(db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(str(db))
    yield c
    c.close()


def test_moves_rows_byte_for_byte_and_audits(db: Path, conn: sqlite3.Connection) -> None:
    teams_before = _rows(db, "SELECT * FROM teams WHERE season_id = 3 ORDER BY team_id")
    rosters_before = _rows(
        db, "SELECT * FROM team_rosters WHERE team_id IN (5, 6) ORDER BY roster_id"
    )
    prints = _sealed_prints(db)

    r = quarantine_2026_stub(conn, sleeper_league_id=LEAGUE)
    assert (r.team_ids, r.roster_rows, r.already_done) == ([5, 6], 3, False)

    q = _rows(db, "SELECT * FROM legacy_nfl_com_2026_preseason ORDER BY quarantine_id")
    copies = {(x["source_table"], x["original_pk"]): json.loads(x["row_data"]) for x in q}
    for t in teams_before:
        assert copies[("teams", str(t["team_id"]))] == t
    for ro in rosters_before:
        assert copies[("team_rosters", str(ro["roster_id"]))] == ro
    assert {x["amendment_id"] for x in q} == {r.amendment_id}

    assert _rows(db, "SELECT count(*) n FROM teams WHERE season_id = 3") == [{"n": 0}]
    assert _rows(db, "SELECT count(*) n FROM team_rosters WHERE team_id IN (5, 6)") == [{"n": 0}]
    season = _rows(
        db, "SELECT platform, platform_league_id, year, status FROM seasons WHERE season_id = 3"
    )
    assert season == [
        {"platform": "sleeper", "platform_league_id": LEAGUE, "year": 2026, "status": "in_progress"}
    ]

    amendments = _rows(db, "SELECT * FROM legacy_amendments")
    assert len(amendments) == 1
    a = amendments[0]
    assert a["before_seal"] == a["after_seal"] == "seal-v1"
    assert json.loads(a["rows_changed"])["teams"]["moved"] == [5, 6]
    assert _sealed_prints(db) == prints  # the sealed 2015 scope is untouched
    assert verify(conn, full=True).ok


def test_rerun_is_a_no_op(conn: sqlite3.Connection) -> None:
    quarantine_2026_stub(conn, sleeper_league_id=LEAGUE)
    again = quarantine_2026_stub(conn, sleeper_league_id=LEAGUE)
    assert again.already_done
    assert conn.execute("SELECT count(*) FROM legacy_amendments").fetchone()[0] == 1


def test_failed_copy_check_rolls_everything_back(db: Path, conn: sqlite3.Connection) -> None:
    with pytest.raises(LegacyError, match="does not match"):
        quarantine_2026_stub(conn, sleeper_league_id=LEAGUE, fail_check=True)
    assert _rows(db, "SELECT count(*) n FROM teams WHERE season_id = 3") == [{"n": 2}]
    assert _rows(db, "SELECT count(*) n FROM legacy_nfl_com_2026_preseason") == [{"n": 0}]
    assert _rows(db, "SELECT count(*) n FROM legacy_amendments") == [{"n": 0}]
    assert _rows(db, "SELECT platform FROM seasons WHERE season_id = 3") == [
        {"platform": "nfl_com"}
    ]


def test_refuses_when_the_stub_has_matchups(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO matchups (season_id, week, team_id, is_playoff, is_consolation) VALUES (3, 1, 5, 0, 0)"
    )
    with pytest.raises(LegacyError, match=r"matchups\.team_id"):
        quarantine_2026_stub(conn, sleeper_league_id=LEAGUE)
    assert conn.execute("SELECT count(*) FROM legacy_nfl_com_2026_preseason").fetchone()[0] == 0


def test_refuses_a_sealed_or_relabelled_season(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE seasons SET platform = 'sleeper' WHERE season_id = 3")
    with pytest.raises(LegacyError, match="expected 'nfl_com'"):
        quarantine_2026_stub(conn, sleeper_league_id=LEAGUE)


def test_rostered_spans_recomputed_after_the_move(db: Path, conn: sqlite3.Connection) -> None:
    quarantine_2026_stub(conn, sleeper_league_id=LEAGUE)
    engine = create_app_engine(f"sqlite:///{db}")
    try:
        with Session(engine) as ss:
            recompute_rostered_spans(ss)
            ss.commit()
    finally:
        engine.dispose()
    spans = {
        r["player_id"]: r["last_rostered_season"]
        for r in _rows(db, "SELECT player_id, last_rostered_season FROM players")
    }
    assert spans == {1: 2016, 2: None, 3: None}
