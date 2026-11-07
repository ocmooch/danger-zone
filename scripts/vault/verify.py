#!/usr/bin/env python3
"""Verify a legacy-vault directory (seal or snapshot). Standard library only.

Run it from inside an extracted vault directory, or pass the directory:

    python3 verify.py            # verifies the directory this file lives in
    python3 verify.py <dir>

Checks, in order:
  1. MANIFEST.json matches MANIFEST.json.sha256
  2. every file listed in the manifest exists and matches its sha256
     (and no unlisted files are present)
  3. the SQLite copy passes PRAGMA integrity_check
  4. every fingerprinted table in the SQLite copy matches its row count,
     table hash and per-season hashes

This file also defines the fingerprint algorithm (03_LEGACY_PRESERVATION.md §5).
The seal builder imports it from here, so the writer and the checker can never
disagree. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

MANIFEST_NAME = "MANIFEST.json"
MANIFEST_SHA_NAME = "MANIFEST.json.sha256"
BOOKKEEPING_COLUMNS = frozenset({"created_at", "updated_at"})
CHUNK = 1 << 20


# ── fingerprints ─────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_value(value: Any, decl_type: str) -> Any:
    """Normalise one stored value for hashing.

    BOOLEAN columns become true/false, JSON columns are parsed (and later
    re-dumped with sorted keys), blobs become hex, everything else is kept as
    stored (timestamps stay as their stored text, floats are rendered by
    json's repr-based float formatting).
    """
    if value is None:
        return None
    kind = decl_type.upper()
    if kind == "BOOLEAN" and isinstance(value, int):
        return bool(value)
    if kind == "JSON" and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    if isinstance(value, bytes):
        return value.hex()
    return value


def canonical_row(columns: list[str], types: list[str], row: Iterable[Any]) -> str:
    record = {
        col: canonical_value(val, typ) for col, typ, val in zip(columns, types, row, strict=True)
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def table_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, int]]:
    """(name, declared type, pk position) for each column, in table order."""
    return [(r[1], r[2] or "", r[5]) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = sorted((c for c in table_columns(conn, table) if c[2] > 0), key=lambda c: c[2])
    return [c[0] for c in cols] or ["rowid"]


def season_key_sql(conn: sqlite3.Connection, table: str) -> str | None:
    """SQL expression giving the row's season *year*, or None if the table has no season."""
    names = {c[0] for c in table_columns(conn, table)}
    if "season_year" in names:
        return "t.season_year"
    if "season_id" in names:
        return "(SELECT s.year FROM seasons s WHERE s.season_id = t.season_id)"
    if "team_id" in names and table != "teams":
        return (
            "(SELECT s.year FROM teams tm JOIN seasons s ON s.season_id = tm.season_id"
            " WHERE tm.team_id = t.team_id)"
        )
    return None


def iter_canonical_rows(
    conn: sqlite3.Connection, table: str, columns: list[str]
) -> Iterator[tuple[Any, str]]:
    """Yield (season_year or None, canonical json) ordered by primary key."""
    decl = {c[0]: c[1] for c in table_columns(conn, table)}
    types = [decl[c] for c in columns]
    season_sql = season_key_sql(conn, table) or "NULL"
    order = ", ".join(f't."{c}"' for c in primary_key(conn, table))
    select = ", ".join(f't."{c}"' for c in columns)
    sql = f'SELECT {season_sql}, {select} FROM "{table}" t ORDER BY {order}'
    for row in conn.execute(sql):
        yield row[0], canonical_row(columns, types, row[1:])


def fingerprint_table(conn: sqlite3.Connection, table: str, columns: list[str]) -> dict[str, Any]:
    """Row count, whole-table sha256 and per-season sha256 over ``columns``."""
    whole = hashlib.sha256()
    per_season: dict[str, tuple[int, Any]] = {}
    count = 0
    for year, line in iter_canonical_rows(conn, table, columns):
        data = line.encode() + b"\n"
        whole.update(data)
        count += 1
        if year is not None:
            key = str(year)
            if key not in per_season:
                per_season[key] = (0, hashlib.sha256())
            n, h = per_season[key]
            h.update(data)
            per_season[key] = (n + 1, h)
    result: dict[str, Any] = {
        "legacy_columns": list(columns),
        "row_count": count,
        "table_sha256": whole.hexdigest(),
    }
    if per_season:
        result["by_season"] = {
            k: {"rows": n, "sha256": h.hexdigest()}
            for k, (n, h) in sorted(per_season.items(), key=lambda kv: int(kv[0]))
        }
    return result


# ── verification ─────────────────────────────────────────────────────────────


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")

    def fail(self, msg: str) -> None:
        print(f"  FAIL  {msg}")
        self.failures.append(msg)


def verify(root: Path) -> int:
    report = Report()
    manifest_path = root / MANIFEST_NAME
    print(f"Verifying {root}")
    if not manifest_path.is_file():
        print(f"  FAIL  {MANIFEST_NAME} not found")
        return 1

    expected = (root / MANIFEST_SHA_NAME).read_text().split()[0]
    actual = sha256_file(manifest_path)
    if actual == expected:
        report.ok("MANIFEST.json hash")
    else:
        report.fail(f"MANIFEST.json hash {actual} != {expected}")
    manifest = json.loads(manifest_path.read_text())

    # files
    listed: dict[str, str] = manifest.get("files", {})
    bad = 0
    for rel, want in listed.items():
        path = root / rel
        if not path.is_file():
            report.fail(f"missing file {rel}")
            bad += 1
        elif "sha256:" + sha256_file(path) != want:
            report.fail(f"hash mismatch {rel}")
            bad += 1
    present = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name not in (MANIFEST_NAME, MANIFEST_SHA_NAME)
    }
    extra = sorted(present - set(listed))
    for rel in extra:
        report.fail(f"unlisted file {rel}")
    if not bad and not extra:
        report.ok(f"{len(listed)} file hashes")

    # database
    db_rel = manifest.get("database")
    if db_rel:
        conn = sqlite3.connect(f"file:{root / db_rel}?mode=ro&immutable=1", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check == "ok":
                report.ok("sqlite integrity_check")
            else:
                report.fail(f"sqlite integrity_check: {check}")
            for table, want in manifest.get("tables", {}).items():
                got = fingerprint_table(conn, table, want["legacy_columns"])
                if got["row_count"] != want["row_count"]:
                    report.fail(f"{table}: rows {got['row_count']} != {want['row_count']}")
                elif got["table_sha256"] != want["table_sha256"]:
                    changed = [
                        y
                        for y, v in want.get("by_season", {}).items()
                        if got.get("by_season", {}).get(y) != v
                    ]
                    report.fail(f"{table}: hash differs (seasons: {', '.join(changed) or '?'})")
                else:
                    report.ok(f"{table}: {got['row_count']} rows")
        finally:
            conn.close()

    if report.failures:
        print(f"\nFAILED: {len(report.failures)} problem(s).")
        return 1
    print(f"\nPASSED: {manifest.get('label', 'vault')} verified.")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    sys.exit(verify(target.resolve()))
