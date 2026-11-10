"""Check the live DB against the stored seal baseline (``03`` §7).

Never repairs anything: it reports which table, which seasons and which
primary keys differ, and exits non-zero through the CLI.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ff_pipeline.legacy import guard_v2
from ff_pipeline.legacy.fingerprint import ScopedScan, manifest_sha256, scan_table, sealed_years
from ff_pipeline.legacy.spec import GUARDED_TABLES
from ff_pipeline.legacy.triggers import trigger_ddl

PK_SAMPLE = 10


@dataclass
class Seal:
    seal_id: str
    created_at: str
    kind: str
    manifest_sha256: str
    manifest: dict[str, Any]
    raw_manifest: str


@dataclass
class TableDiff:
    table: str
    expected_rows: int
    actual_rows: int
    seasons: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    def describe(self, limit: int = PK_SAMPLE) -> str:
        parts = [f"{self.table}: rows {self.actual_rows} (sealed {self.expected_rows})"]
        if self.seasons:
            parts.append(f"seasons {', '.join(self.seasons)}")
        for label, pks in (
            ("changed", self.changed),
            ("missing", self.missing),
            ("added", self.added),
        ):
            if pks:
                more = f" (+{len(pks) - limit} more)" if len(pks) > limit else ""
                parts.append(f"{label} pk {', '.join(pks[:limit])}{more}")
        return "; ".join(parts)


@dataclass
class VerifyReport:
    seal_id: str | None
    full: bool
    problems: list[str] = field(default_factory=list)
    diffs: list[TableDiff] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)

    @property
    def adopted(self) -> bool:
        return self.seal_id is not None

    @property
    def ok(self) -> bool:
        return not self.problems and not self.diffs

    def lines(self) -> list[str]:
        if not self.adopted:
            return ["No legacy seal adopted in this database; nothing to verify."]
        out = [f"  FAIL  {p}" for p in self.problems]
        out.extend(f"  FAIL  {d.describe()}" for d in self.diffs)
        failed = {d.table for d in self.diffs}
        out.extend(
            f"  ok    {t}: {n} sealed rows" for t, n in self.checked.items() if t not in failed
        )
        mode = "full" if self.full else "quick"
        if self.ok:
            out.append(f"PASSED ({mode}): live DB matches {self.seal_id}.")
        else:
            out.append(
                f"FAILED ({mode}): {len(self.problems) + len(self.diffs)} problem(s) against "
                f"{self.seal_id}. Nothing was repaired; compare with the newest vault copy "
                "(docs/RUNBOOK.md, Legacy vault)."
            )
        return out


def connect(path: str) -> sqlite3.Connection:
    """Autocommit connection; callers open transactions explicitly."""
    return sqlite3.connect(path, isolation_level=None, timeout=60)


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def current_seal(conn: sqlite3.Connection) -> Seal | None:
    """The newest baseline in ``legacy_seals`` (append-only, so newest = last rowid)."""
    if not has_table(conn, "legacy_seals"):
        return None
    row = conn.execute(
        "SELECT seal_id, created_at, kind, manifest_sha256, manifest FROM legacy_seals"
        " ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    raw = row[4] if isinstance(row[4], str) else json.dumps(row[4])
    return Seal(row[0], row[1], row[2], row[3], json.loads(raw), raw)


def check_triggers(conn: sqlite3.Connection) -> list[str]:
    live = dict(
        conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
    )
    problems: list[str] = []
    for name, sql in trigger_ddl():
        if name not in live:
            problems.append(f"guard trigger {name} is missing")
        elif live[name] != sql:
            problems.append(f"guard trigger {name} has been altered")
    return problems


def registry(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {spec.name: {} for spec in GUARDED_TABLES}
    for table, pk, row_hash in conn.execute(
        "SELECT table_name, pk, row_hash FROM legacy_sealed_rows"
    ):
        out.setdefault(table, {})[pk] = row_hash
    return out


def _pk_sort(pks: set[str]) -> list[str]:
    return sorted(pks, key=lambda p: (0, int(p), p) if p.lstrip("-").isdigit() else (1, 0, p))


def diff_rows(
    table: str, scan: ScopedScan, sealed: dict[str, str], expected_rows: int
) -> TableDiff:
    live = scan.row_hashes
    changed = {pk for pk in live.keys() & sealed.keys() if live[pk] != sealed[pk]}
    missing = sealed.keys() - live.keys()
    added = live.keys() - sealed.keys()
    seasons = sorted(
        {str(scan.row_years[pk]) for pk in changed | added if scan.row_years.get(pk) is not None},
        key=int,
    )
    return TableDiff(
        table=table,
        expected_rows=expected_rows,
        actual_rows=len(live),
        seasons=seasons,
        changed=_pk_sort(changed),
        missing=_pk_sort(missing),
        added=_pk_sort(added),
    )


def _season_mismatches(got: dict[str, Any], want: dict[str, Any]) -> list[str]:
    got_s = got.get("by_season", {})
    want_s = want.get("by_season", {})
    return sorted(
        (k for k in got_s.keys() | want_s.keys() if got_s.get(k) != want_s.get(k)), key=int
    )


def verify(conn: sqlite3.Connection, *, full: bool = False) -> VerifyReport:
    """Compare the live DB with the newest seal baseline.

    Quick: guard triggers present and unaltered, the set of sealed seasons,
    and row count + table hash of every guarded table's sealed rows. Full adds
    per-season hashes, a row-by-row check of the registry, the stored
    baseline's own hash, and ``PRAGMA integrity_check``.
    """
    seal = current_seal(conn)
    if seal is None:
        return VerifyReport(seal_id=None, full=full)
    report = VerifyReport(seal_id=seal.seal_id, full=full)
    report.problems.extend(check_triggers(conn))
    report.problems.extend(guard_v2.trigger_problems(conn, required=guard_v2.is_v2(seal.manifest)))

    years: list[int] = [int(y) for y in seal.manifest["sealed_years"]]
    live_years = sealed_years(conn)
    if live_years != years:
        report.problems.append(f"sealed seasons are {live_years}, seal {seal.seal_id} has {years}")

    if full and manifest_sha256(seal.raw_manifest) != seal.manifest_sha256:
        report.problems.append(f"stored baseline for {seal.seal_id} does not match its sha256")

    sealed_rows: dict[str, dict[str, str]] | None = None
    for spec in GUARDED_TABLES:
        want = seal.manifest["tables"][spec.name]
        scan = scan_table(conn, spec, years)
        got = scan.fingerprint
        differs = (
            got["row_count"] != want["row_count"] or got["table_sha256"] != want["table_sha256"]
        )
        if full:
            differs = differs or bool(_season_mismatches(got, want))
        if differs or full:
            if sealed_rows is None:
                sealed_rows = registry(conn)
            diff = diff_rows(spec.name, scan, sealed_rows.get(spec.name, {}), want["row_count"])
            if not diff.seasons:
                diff.seasons = _season_mismatches(got, want)
            if differs or diff.changed or diff.missing or diff.added:
                report.diffs.append(diff)
                continue
        report.checked[spec.name] = got["row_count"]

    report.problems.extend(guard_v2.fingerprint_problems(conn, seal.manifest, years, full=full))

    if full:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            report.problems.append(f"sqlite integrity_check: {check}")
    return report
