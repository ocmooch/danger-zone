"""``legacy amend``: the one audited way to change sealed data (``03`` §8).

Clean verify → named backup → time-boxed unlock → run the command → relock
(always) → fingerprint diff → ``legacy_amendments`` row → new baseline
``seal-vN+1``. Producing and uploading the vault archive for the new seal is
deferred to S9; the CLI prints the follow-up commands.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ff_pipeline.legacy.fingerprint import scan_all, sealed_years
from ff_pipeline.legacy.seal import (
    LegacyError,
    git_head,
    now_text,
    relock,
    require_guard_schema,
    set_unlock,
    write_baseline,
)
from ff_pipeline.legacy.verify import TableDiff, diff_rows, registry, verify

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

DEFAULT_UNLOCK_MINUTES = 10.0


@dataclass
class AmendResult:
    returncode: int
    before_seal: str
    backup_path: Path
    after_seal: str | None = None
    amendment_id: int | None = None
    diffs: list[TableDiff] = field(default_factory=list)


def slugify(reason: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "amend"


def next_seal_id(conn: sqlite3.Connection) -> str:
    highest = 1
    for (seal_id,) in conn.execute("SELECT seal_id FROM legacy_seals"):
        match = re.fullmatch(r"seal-v(\d+)", str(seal_id))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"seal-v{highest + 1}"


def backup_database(conn: sqlite3.Connection, destination: Path) -> None:
    """SQLite online backup (page copy; safe while other connections are open)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()


def _run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode


def amend(
    conn: sqlite3.Connection,
    *,
    reason: str,
    command: Sequence[str],
    backup_dir: Path,
    unlock_minutes: float = DEFAULT_UNLOCK_MINUTES,
    runner: Callable[[Sequence[str]], int] = _run,
    now: datetime | None = None,
) -> AmendResult:
    """Run ``command`` with the guard unlocked and record what it changed."""
    if not reason.strip():
        raise LegacyError("--reason is required")
    if not command:
        raise LegacyError("no command given; pass it after `--`")
    require_guard_schema(conn)

    report = verify(conn, full=True)
    if not report.adopted:
        raise LegacyError(
            "No seal adopted; run `ff-pipeline legacy seal --adopt <manifest>` first."
        )
    if not report.ok:
        raise LegacyError(
            "Legacy verify is not clean; refusing to amend:\n" + "\n".join(report.lines())
        )
    before_seal = report.seal_id
    assert before_seal is not None
    before_rows = registry(conn)

    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"fantasy-pre-amend-{slugify(reason)}-{stamp}.db"
    backup_database(conn, backup_path)
    result = AmendResult(returncode=-1, before_seal=before_seal, backup_path=backup_path)

    set_unlock(conn, unlock_minutes, reason)
    try:
        result.returncode = runner(command)
    finally:
        relock(conn)

    years = sealed_years(conn)
    scans = scan_all(conn, years)
    for name, scan in scans.items():
        sealed = before_rows.get(name, {})
        diff = diff_rows(name, scan, sealed, len(sealed))
        if diff.changed or diff.missing or diff.added:
            result.diffs.append(diff)
    if not result.diffs:
        return result

    after_seal = next_seal_id(conn)
    created_at = now_text()
    tables_changed: dict[str, Any] = {d.table: d.seasons for d in result.diffs}
    rows_changed: dict[str, Any] = {
        d.table: {"changed": d.changed, "missing": d.missing, "added": d.added}
        for d in result.diffs
    }
    git_commit = git_head()
    conn.execute("BEGIN IMMEDIATE")
    try:
        set_unlock(conn, 5, f"re-seal after amendment: {reason}")
        write_baseline(
            conn,
            seal_id=after_seal,
            kind="amend",
            created_at=created_at,
            years=years,
            scans=scans,
            git_commit=git_commit,
            extra={"previous_seal": before_seal, "reason": reason},
        )
        cur = conn.execute(
            "INSERT INTO legacy_amendments (created_at, reason, command, git_commit,"
            " before_seal, after_seal, tables_changed, rows_changed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                created_at,
                reason,
                shlex.join(command),
                git_commit,
                before_seal,
                after_seal,
                json.dumps(tables_changed, sort_keys=True),
                json.dumps(rows_changed, sort_keys=True),
            ),
        )
        relock(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    result.after_seal = after_seal
    result.amendment_id = cur.lastrowid
    return result
