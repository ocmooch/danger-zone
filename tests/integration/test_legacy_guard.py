"""Legacy guard end to end on a tiny sealed DB (S1).

The fixture migrates a temp SQLite file to head, seeds one sealed season (2015)
and one open season (2016) across every guarded table, writes a manifest of the
whole DB, and adopts it as seal-v1 with only 2015 sealed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from sqlalchemy.orm import Session

from ff_pipeline.legacy.amend import amend
from ff_pipeline.legacy.fingerprint import fingerprint_table
from ff_pipeline.legacy.seal import LegacyError, adopt
from ff_pipeline.legacy.spec import GUARDED_TABLES
from ff_pipeline.legacy.verify import connect, verify
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import _fk_disabled_for_sqlite, _make_config, upgrade_to_head

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

GUARD = "legacy_guard"

SEED = """
INSERT INTO leagues (league_id, name, platform, current_season_year) VALUES ('L', 'Test', 'nfl_com', 2016);
INSERT INTO seasons (season_id, league_id, year, status) VALUES (1, 'L', 2015, 'completed'), (2, 'L', 2016, 'in_progress');
INSERT INTO owners (owner_id, league_id, display_name, is_active) VALUES (1, 'L', 'Ann', 1), (2, 'L', 'Bob', 1);
INSERT INTO commissioners (commissioner_id, league_id, owner_id, from_year) VALUES (1, 'L', 1, 2015);
INSERT INTO owner_identity_overrides (override_id, league_id, external_id_kind, external_id_value, canonical_display_name)
    VALUES (1, 'L', 'nfl_user_id', 'u1', 'Ann');
INSERT INTO players (player_id, name_full, is_active, nfl_com_player_id) VALUES
    (1, 'QB One', 1, '100'), (2, 'RB Two', 1, NULL), (3, 'WR Three', 1, '300');
INSERT INTO player_id_overrides (override_id, external_id_kind, external_id_value, player_id) VALUES (1, 'nfl_com', '100', 1);
INSERT INTO player_identity_links (link_id, member_player_id, canonical_player_id, source, confidence)
    VALUES (1, 3, 1, 'manual', 'high');
INSERT INTO teams (team_id, season_id, owner_id, team_name) VALUES (1, 1, 1, 'A15'), (2, 1, 2, 'B15'), (3, 2, 1, 'A16'), (4, 2, 2, 'B16');
INSERT INTO matchups (matchup_id, season_id, week, team_id, opponent_team_id, team_score, opponent_score, is_win, is_playoff, is_consolation)
    VALUES (1, 1, 1, 1, 2, 100.5, 90.0, 1, 0, 0), (2, 2, 1, 3, 4, 80.0, 85.5, 0, 0, 0);
INSERT INTO transactions (transaction_id, season_id, transaction_type, team_id, player_id, extra_data)
    VALUES (1, 1, 'add', 1, 2, '{"faab": 3}'), (2, 2, 'add', 3, 2, NULL);
INSERT INTO scoring_rules (rule_id, season_id, category, stat_key, points_per_unit, unit_size)
    VALUES (1, 1, 'passing', 'passing_yards', 1.0, 25.0), (2, 2, 'passing', 'passing_yards', 1.0, 25.0);
INSERT INTO team_rosters (roster_id, team_id, player_id, season_year, week, roster_slot, is_starter, extra_data)
    VALUES (1, 1, 1, 2015, 1, 'QB', 1, '{"nfl_com_points": 20.5}'), (2, 3, 1, 2016, 1, 'QB', 1, NULL);
INSERT INTO player_availability (availability_id, player_id, season_year, week, status, is_pre_kickoff_snapshot)
    VALUES (1, 2, 2015, 1, 'free_agent', 0), (2, 2, 2016, 1, 'free_agent', 0);
"""


def _manifest(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "label": "seal-v1",
        "git_commit": "test",
        "created_at": "2026-11-07T00:00:00Z",
        "tables": {
            spec.name: fingerprint_table(conn, spec.name, spec.legacy_columns)
            for spec in GUARDED_TABLES
        },
    }


def _build(tmp_path: Path, *, seal: bool = True) -> Path:
    db = tmp_path / "legacy.db"
    engine = create_app_engine(f"sqlite:///{db}")
    upgrade_to_head(engine=engine)
    engine.dispose()
    conn = connect(str(db))
    try:
        conn.executescript(SEED)
        manifest = tmp_path / "seal-v1.manifest.json"
        manifest.write_text(json.dumps(_manifest(conn)))
        if seal:
            adopt(conn, manifest, [2015])
    finally:
        conn.close()
    return db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _build(tmp_path)


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(str(db_path))
    yield c
    c.close()


def _refused(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=GUARD):
        conn.execute(sql, params)


# ── season-scoped tables ─────────────────────────────────────────────────────

SEASON_WRITES = {
    # table: (update on sealed row, update on open row, delete sealed, delete open,
    #         insert into sealed season, insert into open season)
    "matchups": (
        "UPDATE matchups SET team_score = 0 WHERE matchup_id = 1",
        "UPDATE matchups SET team_score = 0 WHERE matchup_id = 2",
        "DELETE FROM matchups WHERE matchup_id = 1",
        "DELETE FROM matchups WHERE matchup_id = 2",
        "INSERT INTO matchups (season_id, week, team_id, is_playoff, is_consolation) VALUES (1, 2, 1, 0, 0)",
        "INSERT INTO matchups (season_id, week, team_id, is_playoff, is_consolation) VALUES (2, 2, 3, 0, 0)",
    ),
    "teams": (
        "UPDATE teams SET team_name = 'X' WHERE team_id = 1",
        "UPDATE teams SET team_name = 'X' WHERE team_id = 3",
        "DELETE FROM teams WHERE team_id = 2",
        "DELETE FROM teams WHERE team_id = 4",
        "INSERT INTO teams (season_id, owner_id, team_name) VALUES (1, 1, 'New15')",
        "INSERT INTO teams (season_id, owner_id, team_name) VALUES (2, 1, 'New16')",
    ),
    "transactions": (
        "UPDATE transactions SET extra_data = '{\"faab\": 9}' WHERE transaction_id = 1",
        "UPDATE transactions SET extra_data = '{\"faab\": 9}' WHERE transaction_id = 2",
        "DELETE FROM transactions WHERE transaction_id = 1",
        "DELETE FROM transactions WHERE transaction_id = 2",
        "INSERT INTO transactions (season_id, transaction_type) VALUES (1, 'drop')",
        "INSERT INTO transactions (season_id, transaction_type) VALUES (2, 'drop')",
    ),
    "scoring_rules": (
        "UPDATE scoring_rules SET points_per_unit = 2 WHERE rule_id = 1",
        "UPDATE scoring_rules SET points_per_unit = 2 WHERE rule_id = 2",
        "DELETE FROM scoring_rules WHERE rule_id = 1",
        "DELETE FROM scoring_rules WHERE rule_id = 2",
        "INSERT INTO scoring_rules (season_id, category, stat_key) VALUES (1, 'rushing', 'rushing_yards')",
        "INSERT INTO scoring_rules (season_id, category, stat_key) VALUES (2, 'rushing', 'rushing_yards')",
    ),
    "team_rosters": (
        "UPDATE team_rosters SET roster_slot = 'BN' WHERE roster_id = 1",
        "UPDATE team_rosters SET roster_slot = 'BN' WHERE roster_id = 2",
        "DELETE FROM team_rosters WHERE roster_id = 1",
        "DELETE FROM team_rosters WHERE roster_id = 2",
        "INSERT INTO team_rosters (team_id, player_id, season_year, week) VALUES (1, 2, 2015, 2)",
        "INSERT INTO team_rosters (team_id, player_id, season_year, week) VALUES (3, 2, 2016, 2)",
    ),
    "player_availability": (
        "UPDATE player_availability SET status = 'waivers' WHERE availability_id = 1",
        "UPDATE player_availability SET status = 'waivers' WHERE availability_id = 2",
        "DELETE FROM player_availability WHERE availability_id = 1",
        "DELETE FROM player_availability WHERE availability_id = 2",
        "INSERT INTO player_availability (player_id, season_year, week, status, is_pre_kickoff_snapshot) VALUES (1, 2015, 3, 'x', 0)",
        "INSERT INTO player_availability (player_id, season_year, week, status, is_pre_kickoff_snapshot) VALUES (1, 2016, 3, 'x', 0)",
    ),
}


@pytest.mark.parametrize("table", sorted(SEASON_WRITES))
def test_sealed_season_writes_are_refused_open_season_writes_succeed(
    conn: sqlite3.Connection, table: str
) -> None:
    upd_sealed, upd_open, del_sealed, del_open, ins_sealed, ins_open = SEASON_WRITES[table]
    conn.execute("PRAGMA foreign_keys = OFF")  # isolate the guard from FK checks
    _refused(conn, upd_sealed)
    _refused(conn, ins_sealed)
    _refused(conn, del_sealed)
    conn.execute(upd_open)
    conn.execute(ins_open)
    conn.execute(del_open)


def test_moving_a_row_into_a_sealed_season_is_refused(conn: sqlite3.Connection) -> None:
    _refused(conn, "UPDATE matchups SET season_id = 1, week = 9 WHERE matchup_id = 2")


def test_rewriting_identical_values_is_allowed(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE matchups SET team_score = 100.5 WHERE matchup_id = 1")
    conn.execute("UPDATE matchups SET updated_at = '2030-01-01' WHERE matchup_id = 1")


def test_non_legacy_column_on_sealed_row_is_writable(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE matchups ADD COLUMN platform TEXT")  # like a later additive column
    conn.execute("UPDATE matchups SET platform = 'nfl_com' WHERE matchup_id = 1")
    conn.execute("UPDATE seasons SET updated_at = '2030-01-01' WHERE season_id = 1")


# ── the seal row ─────────────────────────────────────────────────────────────


def test_unsealing_or_editing_a_sealed_season_is_refused(conn: sqlite3.Connection) -> None:
    _refused(conn, "UPDATE seasons SET is_sealed = 0 WHERE season_id = 1")
    _refused(conn, "UPDATE seasons SET seal_id = NULL WHERE season_id = 1")
    _refused(conn, "UPDATE seasons SET champion_team_id = 2 WHERE season_id = 1")
    conn.execute("PRAGMA foreign_keys = OFF")
    _refused(conn, "DELETE FROM seasons WHERE season_id = 1")
    conn.execute("UPDATE seasons SET status = 'completed' WHERE season_id = 2")


# ── global identity tables and players ───────────────────────────────────────


def test_global_tables_allow_new_rows_but_protect_sealed_ones(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO owners (league_id, display_name, is_active) VALUES ('L', 'Cy', 1)")
    conn.execute(
        "INSERT INTO player_id_overrides (external_id_kind, external_id_value, player_id)"
        " VALUES ('sleeper', '42', 2)"
    )
    new_owner = conn.execute("SELECT max(owner_id) FROM owners").fetchone()[0]
    conn.execute("UPDATE owners SET display_name = 'Cyrus' WHERE owner_id = ?", (new_owner,))
    conn.execute("DELETE FROM owners WHERE owner_id = ?", (new_owner,))

    _refused(conn, "UPDATE owners SET display_name = 'Annie' WHERE owner_id = 1")
    _refused(conn, "UPDATE leagues SET current_season_year = 2027 WHERE league_id = 'L'")
    _refused(conn, "UPDATE player_identity_links SET confidence = 'low' WHERE link_id = 1")
    conn.execute("PRAGMA foreign_keys = OFF")
    _refused(conn, "DELETE FROM commissioners WHERE commissioner_id = 1")
    _refused(conn, "DELETE FROM owner_identity_overrides WHERE override_id = 1")
    _refused(conn, "DELETE FROM player_id_overrides WHERE override_id = 1")


def test_players_identity_mapping_is_protected(conn: sqlite3.Connection) -> None:
    _refused(conn, "UPDATE players SET nfl_com_player_id = '999' WHERE player_id = 1")
    _refused(conn, "UPDATE players SET nfl_com_player_id = NULL WHERE player_id = 3")
    conn.execute("PRAGMA foreign_keys = OFF")
    _refused(conn, "DELETE FROM players WHERE player_id = 1")
    # Filling a NULL id and editing re-derivable columns stay allowed.
    conn.execute("UPDATE players SET nfl_com_player_id = '200' WHERE player_id = 2")
    conn.execute("UPDATE players SET name_full = 'Q. One', sleeper_id = 's1' WHERE player_id = 1")
    conn.execute("INSERT INTO players (name_full, is_active) VALUES ('Rookie', 1)")


# ── unlock window, audit tables ──────────────────────────────────────────────


def _set_unlock(conn: sqlite3.Connection, delta: timedelta) -> None:
    until = (datetime.now(tz=UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE legacy_guard SET unlocked_until = ? WHERE id = 1", (until,))


def test_unlock_window_allows_writes_until_it_expires(conn: sqlite3.Connection) -> None:
    _set_unlock(conn, timedelta(minutes=5))
    conn.execute("UPDATE matchups SET team_score = 101 WHERE matchup_id = 1")
    _set_unlock(conn, timedelta(seconds=-1))
    _refused(conn, "UPDATE matchups SET team_score = 102 WHERE matchup_id = 1")


def test_audit_tables_are_append_only(conn: sqlite3.Connection) -> None:
    _refused(conn, "UPDATE legacy_seals SET kind = 'x'")
    _refused(conn, "DELETE FROM legacy_seals")
    _refused(conn, "DELETE FROM legacy_sealed_rows")
    _refused(conn, "INSERT INTO legacy_sealed_rows VALUES ('owners', '99', 'x')")
    conn.execute(
        "INSERT INTO legacy_amendments (created_at, reason, command, before_seal, after_seal,"
        " tables_changed, rows_changed) VALUES ('t', 'r', 'c', 'a', 'b', '{}', '{}')"
    )
    _refused(conn, "UPDATE legacy_amendments SET reason = 'x'")
    _refused(conn, "DELETE FROM legacy_amendments")


# ── verify ───────────────────────────────────────────────────────────────────


def test_verify_passes_on_clean_db_and_ignores_open_season(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE matchups SET team_score = 1 WHERE matchup_id = 2")  # 2016 is open
    conn.execute("INSERT INTO owners (league_id, display_name, is_active) VALUES ('L', 'Cy', 1)")
    assert verify(conn, full=False).ok
    report = verify(conn, full=True)
    assert report.ok, report.lines()


def test_quick_verify_detects_a_dropped_trigger(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER legacy_guard_matchups_update")
    report = verify(conn, full=False)
    assert not report.ok
    assert any("legacy_guard_matchups_update is missing" in p for p in report.problems)


def test_full_verify_locates_a_changed_row(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER legacy_guard_team_rosters_update")  # simulate a bypass
    conn.execute("UPDATE team_rosters SET roster_slot = 'BN' WHERE roster_id = 1")
    report = verify(conn, full=True)
    assert not report.ok
    (diff,) = report.diffs
    assert diff.table == "team_rosters"
    assert diff.seasons == ["2015"]
    assert diff.changed == ["1"]
    text = "\n".join(report.lines())
    assert "team_rosters" in text and "changed pk 1" in text


def test_verify_is_a_noop_without_a_seal(tmp_path: Path) -> None:
    db = _build(tmp_path, seal=False)
    c = connect(str(db))
    try:
        report = verify(c, full=True)
        assert not report.adopted and report.ok
        # Guards are inert before adoption.
        c.execute("UPDATE matchups SET team_score = 0 WHERE matchup_id = 1")
        c.execute("UPDATE players SET nfl_com_player_id = 'x' WHERE player_id = 1")
    finally:
        c.close()


def test_adopt_refuses_a_db_that_differs_from_the_manifest(tmp_path: Path) -> None:
    db = _build(tmp_path, seal=False)
    c = connect(str(db))
    try:
        c.execute("UPDATE matchups SET team_score = 1 WHERE matchup_id = 1")
        with pytest.raises(LegacyError, match="matchups"):
            adopt(c, tmp_path / "seal-v1.manifest.json", [2015])
        assert c.execute("SELECT count(*) FROM legacy_seals").fetchone()[0] == 0
        assert c.execute("SELECT is_sealed FROM seasons WHERE year = 2015").fetchone()[0] == 0
    finally:
        c.close()


def test_adopt_twice_is_refused(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(LegacyError, match="already adopted"):
        adopt(conn, tmp_path / "seal-v1.manifest.json", [2015])


# ── amend ────────────────────────────────────────────────────────────────────


def _python(db: Path, sql: str, exit_code: int = 0) -> list[str]:
    code = (
        "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
        f"c.execute({sql!r}); c.commit(); c.close(); sys.exit({exit_code})"
    )
    return [sys.executable, "-c", code, str(db)]


def test_amend_records_audit_row_and_new_seal(
    conn: sqlite3.Connection, db_path: Path, tmp_path: Path
) -> None:
    result = amend(
        conn,
        reason="Fix 2015 wk1 score (test)",
        command=_python(db_path, "UPDATE matchups SET team_score = 101.0 WHERE matchup_id = 1"),
        backup_dir=tmp_path / "amendments",
    )
    assert result.returncode == 0
    assert result.backup_path.is_file()
    assert result.backup_path.name.startswith("fantasy-pre-amend-fix-2015-wk1-score-test-")
    assert (result.before_seal, result.after_seal) == ("seal-v1", "seal-v2")
    row = conn.execute(
        "SELECT reason, before_seal, after_seal, tables_changed, rows_changed FROM legacy_amendments"
    ).fetchone()
    assert row[:3] == ("Fix 2015 wk1 score (test)", "seal-v1", "seal-v2")
    assert json.loads(row[3]) == {"matchups": ["2015"]}
    assert json.loads(row[4])["matchups"]["changed"] == ["1"]
    assert conn.execute("SELECT unlocked_until FROM legacy_guard").fetchone()[0] is None
    assert verify(conn, full=True).ok  # the new baseline is current
    _refused(conn, "UPDATE matchups SET team_score = 5 WHERE matchup_id = 1")


def test_amend_relocks_when_the_command_fails(
    conn: sqlite3.Connection, db_path: Path, tmp_path: Path
) -> None:
    result = amend(
        conn,
        reason="failing script",
        command=_python(db_path, "SELECT 1", exit_code=3),
        backup_dir=tmp_path / "amendments",
    )
    assert result.returncode == 3
    assert result.amendment_id is None and result.after_seal is None
    assert conn.execute("SELECT unlocked_until FROM legacy_guard").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM legacy_amendments").fetchone()[0] == 0


def test_amend_relocks_when_the_runner_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    def boom(_cmd: object) -> int:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        amend(conn, reason="interrupted", command=["x"], backup_dir=tmp_path, runner=boom)
    assert conn.execute("SELECT unlocked_until FROM legacy_guard").fetchone()[0] is None


def test_amend_requires_a_clean_verify(conn: sqlite3.Connection, tmp_path: Path) -> None:
    conn.execute("DROP TRIGGER legacy_guard_teams_update")
    with pytest.raises(LegacyError, match="not clean"):
        amend(conn, reason="r", command=["true"], backup_dir=tmp_path)


# ── migrations and re-derivable writers ──────────────────────────────────────


def _all_fingerprints(db: Path) -> dict[str, Any]:
    c = sqlite3.connect(db)
    try:
        return {s.name: fingerprint_table(c, s.name, s.legacy_columns) for s in GUARDED_TABLES}
    finally:
        c.close()


def test_upgrade_downgrade_upgrade_keeps_fingerprints(tmp_path: Path) -> None:
    db = _build(tmp_path)
    before = _all_fingerprints(db)
    engine = create_app_engine(f"sqlite:///{db}")
    try:
        cfg = _make_config()
        with engine.connect() as connection, _fk_disabled_for_sqlite(connection):
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, "-1")
        assert _all_fingerprints(db) == before
        upgrade_to_head(engine=engine)
    finally:
        engine.dispose()
    assert _all_fingerprints(db) == before
    c = sqlite3.connect(db)
    try:
        triggers = c.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()
        assert triggers[0] > 0
    finally:
        c.close()


def test_rescore_on_a_sealed_season_succeeds(db_path: Path) -> None:
    from ff_pipeline.repository.models import PlayerStatsRaw, PlayerStatsScored
    from ff_pipeline.scoring.rescore import rescore_seasons

    engine = create_app_engine(f"sqlite:///{db_path}")
    try:
        with Session(engine) as ss:
            ss.add(
                PlayerStatsRaw(
                    player_id=1,
                    season_year=2015,
                    week=1,
                    season_type="REG",
                    source="nflverse",
                    is_primary=True,
                    ingested_at=datetime.now(tz=UTC),
                    stats={"passing_yards": 250.0},
                )
            )
            ss.commit()
            result = rescore_seasons(ss, season_years=[2015], league_id="L")
            ss.commit()
            assert result.rows_scored == 1
            assert ss.query(PlayerStatsScored).count() == 1
    finally:
        engine.dispose()
    c = connect(str(db_path))
    try:
        assert verify(c, full=True).ok
    finally:
        c.close()
