"""Table fingerprints for the legacy seal.

The serializer and ``fingerprint_table`` are a typed copy of the algorithm in
``scripts/vault/verify.py`` (``03_LEGACY_PRESERVATION.md`` §5). That script must
stay standard-library-only because it ships inside the vault, so the two copies
are kept identical by a parity unit test rather than by importing one another.

On top of the whole-table fingerprint, this module adds a *scoped* scan: only
the rows the seal protects (sealed seasons, or rows listed in
``legacy_sealed_rows``), plus a short per-row hash so a mismatch can be traced
to individual primary keys.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ff_pipeline.legacy.spec import GUARDED_TABLES, GuardedTable, GuardKind

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Iterator, Sequence
    from pathlib import Path

BOOKKEEPING_COLUMNS = frozenset({"created_at", "updated_at"})
# Per-row hashes only locate a change; the table and season hashes stay full
# sha256, so 16 hex chars (64 bits) is plenty and keeps the registry small.
ROW_HASH_LEN = 16


# ── canonical serializer (mirror of scripts/vault/verify.py) ─────────────────


def canonical_value(value: Any, decl_type: str) -> Any:
    """Normalise one stored value for hashing (see scripts/vault/verify.py)."""
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


def canonical_row(columns: Sequence[str], types: Sequence[str], row: Iterable[Any]) -> str:
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


def _iter_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    where: str = "1",
    params: Sequence[Any] = (),
) -> Iterator[tuple[Any, str, str]]:
    """Yield (season_year or None, pk as text, canonical json) ordered by primary key."""
    decl = {c[0]: c[1] for c in table_columns(conn, table)}
    types = [decl[c] for c in columns]
    season_sql = season_key_sql(conn, table) or "NULL"
    pk_cols = primary_key(conn, table)
    order = ", ".join(f't."{c}"' for c in pk_cols)
    pk_text = " || '|' || ".join(f'CAST(t."{c}" AS TEXT)' for c in pk_cols)
    select = ", ".join(f't."{c}"' for c in columns)
    sql = (
        f'SELECT {season_sql}, {pk_text}, {select} FROM "{table}" t WHERE {where} ORDER BY {order}'
    )
    for row in conn.execute(sql, tuple(params)):
        yield row[0], row[1], canonical_row(columns, types, row[2:])


class _Hasher:
    """Accumulates the whole-table and per-season sha256 over canonical lines."""

    def __init__(self, columns: Sequence[str]) -> None:
        self.columns = list(columns)
        self.whole = hashlib.sha256()
        self.per_season: dict[str, tuple[int, Any]] = {}
        self.count = 0

    def add(self, year: Any, line: str) -> bytes:
        data = line.encode() + b"\n"
        self.whole.update(data)
        self.count += 1
        if year is not None:
            key = str(year)
            if key not in self.per_season:
                self.per_season[key] = (0, hashlib.sha256())
            n, h = self.per_season[key]
            h.update(data)
            self.per_season[key] = (n + 1, h)
        return data

    def result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "legacy_columns": list(self.columns),
            "row_count": self.count,
            "table_sha256": self.whole.hexdigest(),
        }
        if self.per_season:
            result["by_season"] = {
                k: {"rows": n, "sha256": h.hexdigest()}
                for k, (n, h) in sorted(self.per_season.items(), key=lambda kv: int(kv[0]))
            }
        return result


def fingerprint_table(
    conn: sqlite3.Connection, table: str, columns: Sequence[str]
) -> dict[str, Any]:
    """Row count, whole-table sha256 and per-season sha256 over ``columns`` (all rows)."""
    hasher = _Hasher(columns)
    for year, _pk, line in _iter_rows(conn, table, columns):
        hasher.add(year, line)
    return hasher.result()


# ── scoped scan (the rows a seal protects) ───────────────────────────────────


@dataclass
class ScopedScan:
    """Fingerprint of a table's sealed rows plus a per-row hash for locating diffs."""

    table: str
    fingerprint: dict[str, Any]
    row_hashes: dict[str, str] = field(default_factory=dict)
    row_years: dict[str, int | None] = field(default_factory=dict)


def sealed_years(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute("SELECT year FROM seasons WHERE is_sealed = 1 ORDER BY year")
    ]


def _scope_sql(
    conn: sqlite3.Connection, spec: GuardedTable, years: Sequence[int], *, adopt: bool
) -> tuple[str, list[Any]]:
    if spec.kind is GuardKind.REGISTERED:
        if adopt:
            return spec.adopt_filter or "1", []
        return (
            f'CAST(t."{spec.pk}" AS TEXT) IN '
            "(SELECT pk FROM legacy_sealed_rows WHERE table_name = ?)",
            [spec.name],
        )
    if not years:
        return "0", []
    season_sql = season_key_sql(conn, spec.name)
    if season_sql is None:  # pragma: no cover - spec and schema disagree
        raise RuntimeError(f"{spec.name} has no season column")
    marks = ", ".join("?" for _ in years)
    return f"({season_sql}) IN ({marks})", list(years)


def scan_table(
    conn: sqlite3.Connection, spec: GuardedTable, years: Sequence[int], *, adopt: bool = False
) -> ScopedScan:
    """Fingerprint only the sealed rows of ``spec``.

    Season tables are scoped to ``years``. Registered (global) tables are
    scoped to their rows in ``legacy_sealed_rows`` — or, when ``adopt`` is
    true, to the rows that are about to be registered (``spec.adopt_filter``).
    """
    where, params = _scope_sql(conn, spec, years, adopt=adopt)
    hasher = _Hasher(spec.legacy_columns)
    scan = ScopedScan(table=spec.name, fingerprint={})
    for year, pk, line in _iter_rows(conn, spec.name, spec.legacy_columns, where, params):
        data = hasher.add(year, line)
        scan.row_hashes[pk] = hashlib.sha256(data).hexdigest()[:ROW_HASH_LEN]
        scan.row_years[pk] = None if year is None else int(year)
    scan.fingerprint = hasher.result()
    return scan


def scan_all(
    conn: sqlite3.Connection, years: Sequence[int], *, adopt: bool = False
) -> dict[str, ScopedScan]:
    return {spec.name: scan_table(conn, spec, years, adopt=adopt) for spec in GUARDED_TABLES}


def manifest_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
