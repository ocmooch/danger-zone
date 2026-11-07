"""Writing seal baselines: the one-time adopt of seal-v1, and re-seals after amendments."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ff_pipeline.legacy.fingerprint import (
    ScopedScan,
    fingerprint_table,
    manifest_sha256,
    scan_all,
    sha256_file,
)
from ff_pipeline.legacy.spec import GUARDED_TABLES
from ff_pipeline.legacy.verify import current_seal, has_table

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path


class LegacyError(RuntimeError):
    """A legacy-guard operation refused to proceed."""


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_text() -> str:
    return utc_text(datetime.now(tz=UTC))


def git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def alembic_revision(conn: sqlite3.Connection) -> str | None:
    if not has_table(conn, "alembic_version"):
        return None
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return None if row is None else str(row[0])


def set_unlock(conn: sqlite3.Connection, minutes: float, reason: str) -> str:
    until = utc_text(datetime.now(tz=UTC) + timedelta(minutes=minutes))
    conn.execute(
        "UPDATE legacy_guard SET unlocked_until = ?, unlocked_reason = ? WHERE id = 1",
        (until, reason),
    )
    return until


def relock(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE legacy_guard SET unlocked_until = NULL, unlocked_reason = NULL WHERE id = 1"
    )


def require_guard_schema(conn: sqlite3.Connection) -> None:
    if not has_table(conn, "legacy_guard"):
        raise LegacyError(
            "This database has no legacy guard tables yet; run `ff-pipeline migrate up` first."
        )


def baseline_manifest(
    seal_id: str,
    kind: str,
    created_at: str,
    years: Sequence[int],
    scans: dict[str, ScopedScan],
    extra: dict[str, Any] | None = None,
) -> str:
    """Canonical JSON text of a baseline (stored verbatim; its sha256 is stored too)."""
    doc: dict[str, Any] = {
        "seal_id": seal_id,
        "kind": kind,
        "created_at": created_at,
        "sealed_years": list(years),
        "tables": {name: scans[name].fingerprint for name in sorted(scans)},
    }
    if extra:
        doc.update(extra)
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def write_baseline(
    conn: sqlite3.Connection,
    *,
    seal_id: str,
    kind: str,
    created_at: str,
    years: Sequence[int],
    scans: dict[str, ScopedScan],
    git_commit: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Store a baseline and rebuild the sealed-row registry.

    Must run inside the caller's transaction, with the guard unlocked.
    """
    raw = baseline_manifest(seal_id, kind, created_at, years, scans, extra)
    conn.execute("DELETE FROM legacy_sealed_rows")
    conn.executemany(
        "INSERT INTO legacy_sealed_rows (table_name, pk, row_hash) VALUES (?, ?, ?)",
        [(name, pk, h) for name, scan in scans.items() for pk, h in scan.row_hashes.items()],
    )
    conn.execute(
        "INSERT INTO legacy_seals (seal_id, created_at, kind, git_commit, alembic_revision,"
        " manifest_sha256, manifest) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (seal_id, created_at, kind, git_commit, alembic_revision(conn), manifest_sha256(raw), raw),
    )
    # Seasons sealed for the first time by this baseline record which seal did it.
    conn.execute(
        "UPDATE seasons SET sealed_at = ?, seal_id = ? WHERE is_sealed = 1 AND seal_id IS NULL",
        (created_at, seal_id),
    )


@dataclass
class AdoptResult:
    seal_id: str
    years: list[int]
    sealed_rows: int


def _check_against_manifest(conn: sqlite3.Connection, manifest: dict[str, Any]) -> list[str]:
    """Whole-table fingerprints of the live DB vs the committed seal manifest."""
    problems: list[str] = []
    tables: dict[str, Any] = manifest.get("tables", {})
    expected_names = {spec.name for spec in GUARDED_TABLES}
    if set(tables) != expected_names:
        problems.append(
            f"manifest tables {sorted(tables)} != guarded tables {sorted(expected_names)}"
        )
        return problems
    for spec in GUARDED_TABLES:
        want = tables[spec.name]
        if list(want["legacy_columns"]) != list(spec.legacy_columns):
            problems.append(f"{spec.name}: manifest legacy columns differ from the guard spec")
            continue
        got = fingerprint_table(conn, spec.name, spec.legacy_columns)
        if got != want:
            seasons = sorted(
                (
                    y
                    for y in set(got.get("by_season", {})) | set(want.get("by_season", {}))
                    if got.get("by_season", {}).get(y) != want.get("by_season", {}).get(y)
                ),
                key=int,
            )
            problems.append(
                f"{spec.name}: rows {got['row_count']} vs manifest {want['row_count']}, "
                f"hash differs (seasons: {', '.join(seasons) or '-'})"
            )
    return problems


def adopt(conn: sqlite3.Connection, manifest_path: Path, years: Sequence[int]) -> AdoptResult:
    """One-time: check live == committed manifest, then store seal-v1 and seal ``years``."""
    require_guard_schema(conn)
    if current_seal(conn) is not None:
        raise LegacyError("A seal is already adopted; use `ff-pipeline legacy status`.")

    sidecar = manifest_path.with_name(manifest_path.name + ".sha256")
    file_sha = sha256_file(manifest_path)
    if sidecar.is_file() and sidecar.read_text().split()[0] != file_sha:
        raise LegacyError(f"{manifest_path.name} does not match {sidecar.name}")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    seal_id = str(manifest.get("label") or manifest.get("seal") or "")
    if not seal_id:
        raise LegacyError(f"{manifest_path} has no seal label")

    problems = _check_against_manifest(conn, manifest)
    if problems:
        raise LegacyError(
            "Live DB does not match the seal manifest; not sealing:\n  " + "\n  ".join(problems)
        )

    present = {
        int(r[0])
        for r in conn.execute(
            f"SELECT year FROM seasons WHERE year IN ({', '.join('?' for _ in years)})",
            tuple(years),
        )
    }
    absent = sorted(set(years) - present)
    if absent:
        raise LegacyError(f"cannot seal missing seasons: {absent}")

    created_at = now_text()
    conn.execute("BEGIN IMMEDIATE")
    try:
        set_unlock(conn, 5, f"adopt {seal_id}")
        conn.execute(
            f"UPDATE seasons SET is_sealed = 1 WHERE year IN ({', '.join('?' for _ in years)})",
            tuple(years),
        )
        scans = scan_all(conn, sorted(years), adopt=True)
        # Same rows, same algorithm: the sealed seasons must hash exactly as in
        # the committed manifest.
        for name, scan in scans.items():
            want_by = manifest["tables"][name].get("by_season", {})
            for year, entry in scan.fingerprint.get("by_season", {}).items():
                if want_by.get(year) != entry:
                    raise LegacyError(f"{name} {year}: scoped hash disagrees with the manifest")
        write_baseline(
            conn,
            seal_id=seal_id,
            kind="adopt",
            created_at=created_at,
            years=sorted(years),
            scans=scans,
            git_commit=git_head(),
            extra={
                "source_manifest": {
                    "file": manifest_path.name,
                    "sha256": file_sha,
                    "git_commit": manifest.get("git_commit"),
                    "created_at": manifest.get("created_at"),
                }
            },
        )
        relock(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return AdoptResult(
        seal_id=seal_id,
        years=sorted(years),
        sealed_rows=sum(len(s.row_hashes) for s in scans.values()),
    )
