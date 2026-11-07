"""Legacy-vault fingerprint serializer and archive retention (S0, shared with S1).

The fingerprint must be deterministic across runs and insertion order, and
stable under JSON key order; it must ignore bookkeeping columns and localise a
change to the season it happened in.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

VAULT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "vault"


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(VAULT_DIR))
    spec = importlib.util.spec_from_file_location(name, VAULT_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vfy = _load("verify")
sync = _load("vault_sync")

ROWS = [
    # matchup_id, season_id, team_score, is_win, extra, updated_at
    (1, 1, 101.5, 1, '{"b": 2, "a": 1}', "2026-01-01"),
    (2, 1, 88.25, 0, None, "2026-01-01"),
    (3, 2, 0.1, 1, '{"x": [1, 2]}', "2026-01-01"),
]
COLS = ["matchup_id", "season_id", "team_score", "is_win", "extra"]


def _db(rows: list[tuple], path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE seasons (season_id INTEGER PRIMARY KEY, year INTEGER);
        INSERT INTO seasons VALUES (1, 2014), (2, 2015);
        CREATE TABLE matchups (matchup_id INTEGER PRIMARY KEY, season_id INTEGER,
            team_score FLOAT, is_win BOOLEAN, extra JSON, updated_at DATETIME);
        """
    )
    conn.executemany("INSERT INTO matchups VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


@pytest.fixture
def base(tmp_path: Path) -> dict:
    return vfy.fingerprint_table(_db(ROWS, tmp_path / "a.db"), "matchups", COLS)


def test_deterministic_and_insertion_order_independent(tmp_path: Path, base: dict) -> None:
    again = vfy.fingerprint_table(_db(ROWS, tmp_path / "b.db"), "matchups", COLS)
    shuffled = vfy.fingerprint_table(_db(ROWS[::-1], tmp_path / "c.db"), "matchups", COLS)
    assert again == base == shuffled
    assert base["row_count"] == 3
    assert set(base["by_season"]) == {"2014", "2015"}


def test_json_key_order_and_bookkeeping_do_not_matter(tmp_path: Path, base: dict) -> None:
    rows = [list(r) for r in ROWS]
    rows[0][4] = '{"a": 1, "b": 2}'  # same JSON, different key order
    rows[1][5] = "2030-12-31"  # updated_at is not a legacy column
    got = vfy.fingerprint_table(_db([tuple(r) for r in rows], tmp_path / "d.db"), "matchups", COLS)
    assert got == base


def test_change_is_localised_to_its_season(tmp_path: Path, base: dict) -> None:
    rows = [list(r) for r in ROWS]
    rows[2][2] = 0.1000001
    got = vfy.fingerprint_table(_db([tuple(r) for r in rows], tmp_path / "e.db"), "matchups", COLS)
    assert got["table_sha256"] != base["table_sha256"]
    assert got["by_season"]["2014"] == base["by_season"]["2014"]
    assert got["by_season"]["2015"] != base["by_season"]["2015"]


def test_canonical_row_types() -> None:
    line = vfy.canonical_row(
        ["b", "f", "j", "n", "x"],
        ["BOOLEAN", "FLOAT", "JSON", "VARCHAR", "BLOB"],
        [1, 0.1, '{"z": 1, "a": null}', None, b"\x01\xff"],
    )
    assert line == '{"b":true,"f":0.1,"j":{"a":null,"z":1},"n":null,"x":"01ff"}'
    # Malformed JSON is kept verbatim rather than crashing the fingerprint.
    assert vfy.canonical_value("{not json", "JSON") == "{not json"


def test_retention_pins_seal_v1_and_keeps_four_newest() -> None:
    seal = "dz-vault_seal-v1_20261107_aaaaaaaa.tar.gz.age"
    snaps = [f"dz-vault_snap_202611{d:02d}_bbbbbbbb.tar.gz.age" for d in range(8, 15)]
    keep, delete = sync.plan_retention([seal, *snaps, "README.md", ".x.partial"])
    assert seal in keep
    assert len(keep) == 5
    assert keep == sorted([seal, *snaps[-4:]])
    assert delete == sorted(snaps[:3])


def test_retention_never_deletes_when_under_limit() -> None:
    names = [
        "dz-vault_seal-v1_20261107_aaaaaaaa.tar.gz.age",
        "dz-vault_snap_20261108_bbbbbbbb.tar.gz.age",
    ]
    assert sync.plan_retention(names) == (sorted(names), [])
