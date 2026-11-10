"""S9 season close: seal a finished Sleeper season as seal-v2 with guard v2."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from ff_pipeline.legacy.amend import amend
from ff_pipeline.legacy.seal import LegacyError
from ff_pipeline.legacy.seal_season import SeasonCloseFacts, preconditions, seal_season
from ff_pipeline.legacy.verify import connect, current_seal, verify
from tests.integration.test_sleeper_load import _load, db, engine  # noqa: F401
from tests.integration.test_sleeper_rich import _decided, _load_bundle
from tests.unit.test_sleeper_normalizer import fixture_league

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration
GUARD = "legacy_guard"
NOW = datetime(2027, 1, 20, tzinfo=UTC)
FACTS = SeasonCloseFacts(complete_observed_at=NOW - timedelta(days=9), reconcile_failures=0)


def _finished(engine: Engine) -> None:  # noqa: F811
    """The fixture season, fully decided and marked complete."""
    _load(engine)
    data = _decided(fixture_league())
    _load_bundle(engine, replace(data, league={**data.league, "status": "complete"}))


def _champion(db: Path) -> str:  # noqa: F811
    c = sqlite3.connect(db)
    try:
        return str(
            c.execute(
                "SELECT t.team_name FROM seasons s JOIN teams t ON t.team_id = s.champion_team_id"
                " WHERE s.year = 2026"
            ).fetchone()[0]
        )
    finally:
        c.close()


def test_preconditions_refuse_until_everything_holds(db: Path, engine: Engine) -> None:  # noqa: F811
    _load(engine)  # in progress, undecided
    conn = connect(str(db))
    try:
        early = preconditions(conn, 2026, "x", FACTS, now=NOW)
        assert any("status" in p for p in early)
        assert any("champion / runner-up" in p for p in early)
    finally:
        conn.close()
    _finished(engine)
    champ = _champion(db)
    conn = connect(str(db))
    try:
        assert preconditions(conn, 2026, champ, FACTS, now=NOW) == []
        assert preconditions(conn, 2026, f"  {champ.upper()} ", FACTS, now=NOW) == []
        assert any("does not match" in p for p in preconditions(conn, 2026, "Nope", FACTS, now=NOW))
        fresh = replace(FACTS, complete_observed_at=NOW - timedelta(days=2))
        assert any(
            "correction window" in p for p in preconditions(conn, 2026, champ, fresh, now=NOW)
        )
        bad = replace(FACTS, reconcile_failures=3)
        assert any("reconcile" in p for p in preconditions(conn, 2026, champ, bad, now=NOW))
        inverted = replace(FACTS, bracket_warnings=("winner scored less",))
        assert any("bracket" in p for p in preconditions(conn, 2026, champ, inverted, now=NOW))
        assert any(
            "NFL.com" in p or "nfl_com" in p for p in preconditions(conn, 2016, "x", FACTS, now=NOW)
        )
    finally:
        conn.close()


def test_seal_season_guards_the_sleeper_era_and_verify_passes(
    tmp_path: Path,
    db: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    _finished(engine)
    champ = _champion(db)
    conn = connect(str(db))
    try:
        v1_tables = current_seal(conn).manifest["tables"]  # type: ignore[union-attr]
        res = seal_season(conn, 2026, champ, FACTS, backup_dir=tmp_path, now=NOW)
        assert res.problems == [] and res.seal_id == "seal-v2" and res.backup_path.exists()
        seal = current_seal(conn)
        assert seal is not None and seal.manifest["guard_version"] == 2
        assert seal.manifest["sealed_years"] == [2015, 2026]
        assert set(seal.manifest["platform_tables"]) >= {"playoff_brackets", "teams", "matchups"}
        # seal-v1's own coverage of 2015 is unchanged
        assert (
            seal.manifest["tables"]["matchups"]["by_season"]["2015"]
            == (v1_tables["matchups"]["by_season"]["2015"])
        )
        assert verify(conn, full=True).ok
        sid = conn.execute("SELECT season_id FROM seasons WHERE year = 2026").fetchone()[0]
        for sql in (
            f"UPDATE teams SET extra_data = '{{}}' WHERE season_id = {sid}",
            f"UPDATE matchups SET extra_data = '{{}}' WHERE season_id = {sid}",
            f"UPDATE transactions SET status = 'x' WHERE season_id = {sid}",
            f"UPDATE seasons SET settings_snapshot = '{{}}' WHERE season_id = {sid}",
            f"UPDATE playoff_brackets SET placement = 9 WHERE season_id = {sid}",
            f"DELETE FROM playoff_brackets WHERE season_id = {sid}",
            "INSERT INTO playoff_brackets (season_id, bracket, round, match_no, source)"
            f" VALUES ({sid}, 'winners', 9, 9, 'x')",
            "UPDATE team_rosters SET extra_data = '{}' WHERE season_year = 2026",
        ):
            with pytest.raises(sqlite3.IntegrityError, match=GUARD):
                conn.execute(sql)
        # an unsealed NFL.com season stays writable
        conn.execute("UPDATE matchups SET extra_data = '{}' WHERE season_id = 2")
        conn.commit()
        assert verify(conn, full=True).ok
        with pytest.raises(LegacyError, match="already sealed"):
            seal_season(conn, 2026, champ, FACTS, backup_dir=tmp_path, now=NOW)
    finally:
        conn.close()


def test_amend_after_a_v2_seal_re_seals_sleeper_columns_and_keeps_coverage(
    tmp_path: Path,
    db: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    _finished(engine)
    conn = connect(str(db))
    try:
        seal_season(conn, 2026, _champion(db), FACTS, backup_dir=tmp_path, now=NOW)

        def change(_cmd: object) -> int:
            c = sqlite3.connect(db)
            try:
                c.execute(
                    "UPDATE teams SET extra_data = json_set(extra_data, '$.note', 'fixed')"
                    " WHERE season_id = (SELECT season_id FROM seasons WHERE year = 2026)"
                )
                c.commit()
            finally:
                c.close()
            return 0

        res = amend(conn, reason="fix a note", command=["x"], backup_dir=tmp_path, runner=change)
        assert res.v2_changed == ["teams"] and res.after_seal == "seal-v3"
        seal = current_seal(conn)
        assert seal is not None and seal.manifest["guard_version"] == 2
        assert verify(conn, full=True).ok
    finally:
        conn.close()
