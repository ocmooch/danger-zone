"""Legacy fingerprint: determinism, and parity between the package and the vault script.

``scripts/vault/verify.py`` must stay stdlib-only (it ships inside the vault),
so ``ff_pipeline.legacy.fingerprint`` keeps its own copy of the algorithm.
These tests run both on the same DBs and require identical output.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from ff_pipeline.legacy import fingerprint as pkg

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

VAULT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "vault"


def _load_vault_verify() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vault_verify", VAULT_DIR / "verify.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["vault_verify"] = module
    spec.loader.exec_module(module)
    return module


vault = _load_vault_verify()
IMPLEMENTATIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "package": pkg.fingerprint_table,
    "vault": vault.fingerprint_table,
}

ROWS = [
    # matchup_id, season_id, team_score, is_win, extra, updated_at
    (1, 1, 101.5, 1, '{"b": 2, "a": 1}', "2026-01-01"),
    (2, 1, 88.25, 0, None, "2026-01-01"),
    (3, 2, 0.1, 1, '{"x": [1, 2]}', "2026-01-01"),
]
COLS = ["matchup_id", "season_id", "team_score", "is_win", "extra"]


def _db(rows: list[tuple[Any, ...]], path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE seasons (season_id INTEGER PRIMARY KEY, year INTEGER);
        INSERT INTO seasons VALUES (1, 2014), (2, 2015);
        CREATE TABLE teams (team_id INTEGER PRIMARY KEY, season_id INTEGER);
        INSERT INTO teams VALUES (10, 1), (20, 2);
        CREATE TABLE matchups (matchup_id INTEGER PRIMARY KEY, season_id INTEGER,
            team_score FLOAT, is_win BOOLEAN, extra JSON, updated_at DATETIME);
        CREATE TABLE rosters (roster_id INTEGER PRIMARY KEY, team_id INTEGER, slot TEXT,
            blob BLOB);
        INSERT INTO rosters VALUES (1, 10, 'QB', X'01ff'), (2, 20, NULL, NULL);
        CREATE TABLE owners (owner_id INTEGER PRIMARY KEY, name TEXT, active BOOLEAN);
        INSERT INTO owners VALUES (2, 'b', 0), (1, 'a', 1);
        """
    )
    conn.executemany("INSERT INTO matchups VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _fingerprint(impl: str, rows: list[tuple[Any, ...]], path: Path) -> dict[str, Any]:
    conn = _db(rows, path)
    try:
        return IMPLEMENTATIONS[impl](conn, "matchups", COLS)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("matchups", COLS),
        ("rosters", ["roster_id", "team_id", "slot", "blob"]),  # season via team_id
        ("owners", ["owner_id", "name", "active"]),  # no season
        ("teams", ["team_id", "season_id"]),
    ],
)
def test_package_matches_vault_script(tmp_path: Path, table: str, columns: list[str]) -> None:
    conn = _db(ROWS, tmp_path / "p.db")
    try:
        assert pkg.fingerprint_table(conn, table, columns) == vault.fingerprint_table(
            conn, table, columns
        )
    finally:
        conn.close()


def test_canonical_row_parity() -> None:
    args = (
        ["b", "f", "j", "n", "x"],
        ["BOOLEAN", "FLOAT", "JSON", "VARCHAR", "BLOB"],
        [1, 0.1, '{"z": 1, "a": null}', None, b"\x01\xff"],
    )
    line = pkg.canonical_row(*args)
    assert line == vault.canonical_row(*args)
    assert line == '{"b":true,"f":0.1,"j":{"a":null,"z":1},"n":null,"x":"01ff"}'
    # Malformed JSON is kept verbatim rather than crashing the fingerprint.
    assert pkg.canonical_value("{not json", "JSON") == "{not json"


@pytest.mark.parametrize("impl", sorted(IMPLEMENTATIONS))
def test_deterministic_and_insertion_order_independent(tmp_path: Path, impl: str) -> None:
    base = _fingerprint(impl, ROWS, tmp_path / "a.db")
    again = _fingerprint(impl, ROWS, tmp_path / "b.db")
    shuffled = _fingerprint(impl, ROWS[::-1], tmp_path / "c.db")
    assert again == base == shuffled
    assert base["row_count"] == 3
    assert set(base["by_season"]) == {"2014", "2015"}


@pytest.mark.parametrize("impl", sorted(IMPLEMENTATIONS))
def test_json_key_order_and_bookkeeping_do_not_matter(tmp_path: Path, impl: str) -> None:
    base = _fingerprint(impl, ROWS, tmp_path / "a.db")
    rows = [list(r) for r in ROWS]
    rows[0][4] = '{"a": 1, "b": 2}'  # same JSON, different key order
    rows[1][5] = "2030-12-31"  # updated_at is not a legacy column
    assert _fingerprint(impl, [tuple(r) for r in rows], tmp_path / "d.db") == base


@pytest.mark.parametrize("impl", sorted(IMPLEMENTATIONS))
def test_change_is_localised_to_its_season(tmp_path: Path, impl: str) -> None:
    base = _fingerprint(impl, ROWS, tmp_path / "a.db")
    rows = [list(r) for r in ROWS]
    rows[2][2] = 0.1000001
    got = _fingerprint(impl, [tuple(r) for r in rows], tmp_path / "e.db")
    assert got["table_sha256"] != base["table_sha256"]
    assert got["by_season"]["2014"] == base["by_season"]["2014"]
    assert got["by_season"]["2015"] != base["by_season"]["2015"]
