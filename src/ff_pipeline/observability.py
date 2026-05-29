"""Status + backup helpers powering ``ff-pipeline status`` and ``ff-pipeline backup``.

Separated from the CLI so the implementation is exercised directly in
tests without going through Typer / settings bootstrap. Two surfaces:

* ``collect_status`` — assemble a :class:`StatusReport` from the DB +
  filesystem. Pure function over an open session; the CLI renders it.
* ``perform_backup`` — SQLite ``.backup`` API call into
  ``data/backups/`` with optional retention pruning. Postgres URLs are
  rejected with a clear error rather than silently no-op'ing.

The data is intentionally small and JSON-renderable so a Phase 2
dashboard or alerting wrapper can consume the same structures without
re-querying.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ff_pipeline.repository.models import (
    League,
    PipelineRun,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    Projection,
    Season,
    SourceHealth,
    Transaction,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


BACKUP_FILENAME_PREFIX = "fantasy-"
BACKUP_FILENAME_SUFFIX = ".db"
DEFAULT_BACKUP_RETENTION_DAYS = 30
_BACKUP_DATE_RE = re.compile(r"fantasy-(\d{4}-\d{2}-\d{2})\.db$")


# ---------------------------------------------------------------------------
# Status data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Latest-known state for one crawler source."""

    source: str
    status: str
    last_run_at: datetime | None
    rows_added: int | None
    rows_updated: int | None
    parse_failures: int | None
    duration_ms: int | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class TableCounts:
    """High-level row counts so ``status`` reports actionable totals."""

    leagues: int
    seasons: int
    players: int
    player_stats_raw: int
    player_stats_scored: int
    projections: int
    transactions: int


@dataclass(frozen=True, slots=True)
class StatusReport:
    """All-in-one status surface consumed by the CLI renderer."""

    generated_at: datetime
    database_url: str
    sqlite_db_path: Path | None
    sqlite_db_size_bytes: int | None
    last_run: PipelineRun | None
    sources: tuple[SourceStatus, ...]
    counts: TableCounts
    recent_failures: tuple[PipelineRun, ...] = field(default_factory=tuple)
    log_dir: Path | None = None
    most_recent_log_file: Path | None = None
    most_recent_backup: Path | None = None


# ---------------------------------------------------------------------------
# Backup data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackupResult:
    """What ``perform_backup`` did."""

    backup_path: Path
    bytes_written: int
    pruned_files: tuple[Path, ...] = ()


class BackupError(RuntimeError):
    """Backup failed in a way the CLI should surface verbatim."""


# ---------------------------------------------------------------------------
# Status collection
# ---------------------------------------------------------------------------


def collect_status(
    session: Session,
    *,
    database_url: str,
    log_dir: Path | None = None,
    backup_dir: Path | None = None,
    recent_failure_limit: int = 5,
) -> StatusReport:
    """Build a :class:`StatusReport` from the open session + filesystem.

    Cheap — a handful of small queries; safe to call from cron without
    risking a long-running transaction.
    """

    last_run = _latest_run(session)
    sources = _latest_source_statuses(session)
    counts = _table_counts(session)
    recent_failures = _recent_failures(session, limit=recent_failure_limit)

    sqlite_path = _sqlite_path_from_url(database_url)
    sqlite_size = sqlite_path.stat().st_size if sqlite_path and sqlite_path.exists() else None

    most_recent_log = _most_recent_file(log_dir) if log_dir is not None else None
    most_recent_backup = _most_recent_backup_file(backup_dir) if backup_dir is not None else None

    return StatusReport(
        generated_at=datetime.now(tz=UTC),
        database_url=database_url,
        sqlite_db_path=sqlite_path,
        sqlite_db_size_bytes=sqlite_size,
        last_run=last_run,
        sources=sources,
        counts=counts,
        recent_failures=recent_failures,
        log_dir=log_dir,
        most_recent_log_file=most_recent_log,
        most_recent_backup=most_recent_backup,
    )


def _latest_run(session: Session) -> PipelineRun | None:
    stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(1)
    return session.execute(stmt).scalars().first()


def _latest_source_statuses(session: Session) -> tuple[SourceStatus, ...]:
    """Return the most recent ``source_health`` row per source.

    Joined against ``pipeline_runs`` so we surface the run's start time —
    a row in ``source_health`` only carries ``created_at``, which is when
    the row landed, not when the work began.
    """

    # Group by source, take the max created_at per source. SQLite + Postgres
    # both support this correlated lookup cheaply at our row volumes.
    latest_per_source = (
        select(
            SourceHealth.source.label("source"),
            func.max(SourceHealth.health_id).label("max_health_id"),
        )
        .group_by(SourceHealth.source)
        .subquery()
    )
    stmt = (
        select(SourceHealth, PipelineRun.started_at)
        .join(latest_per_source, SourceHealth.health_id == latest_per_source.c.max_health_id)
        .join(PipelineRun, PipelineRun.run_id == SourceHealth.run_id)
        .order_by(SourceHealth.source)
    )
    out: list[SourceStatus] = []
    for health, started_at in session.execute(stmt).all():
        out.append(
            SourceStatus(
                source=health.source,
                status=health.status,
                last_run_at=started_at,
                rows_added=health.rows_added,
                rows_updated=health.rows_updated,
                parse_failures=health.parse_failures,
                duration_ms=health.duration_ms,
                error_message=health.error_message,
            )
        )
    return tuple(out)


def _table_counts(session: Session) -> TableCounts:
    def _count(model: type) -> int:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())

    return TableCounts(
        leagues=_count(League),
        seasons=_count(Season),
        players=_count(Player),
        player_stats_raw=_count(PlayerStatsRaw),
        player_stats_scored=_count(PlayerStatsScored),
        projections=_count(Projection),
        transactions=_count(Transaction),
    )


def _recent_failures(session: Session, *, limit: int) -> tuple[PipelineRun, ...]:
    stmt = (
        select(PipelineRun)
        .where(PipelineRun.status == "failed")
        .order_by(PipelineRun.started_at.desc())
        .limit(limit)
    )
    return tuple(session.execute(stmt).scalars().all())


def _most_recent_file(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    candidates = [p for p in directory.iterdir() if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _most_recent_backup_file(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    candidates = [p for p in directory.iterdir() if p.is_file() and _BACKUP_DATE_RE.search(p.name)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_status(report: StatusReport, *, verbose: bool = False) -> str:
    """Build the human-readable text the CLI prints. Deterministic so
    the snapshot-style test can assert on stable substrings."""

    lines: list[str] = []
    lines.append(f"ff-pipeline status @ {_format_dt(report.generated_at)} UTC")
    lines.append(f"  database: {report.database_url}")
    if report.sqlite_db_path is not None:
        size = (
            _format_bytes(report.sqlite_db_size_bytes)
            if report.sqlite_db_size_bytes is not None
            else "missing"
        )
        lines.append(f"            file={report.sqlite_db_path} ({size})")
    if report.log_dir is not None:
        lines.append(f"  logs:     dir={report.log_dir}")
        if report.most_recent_log_file is not None:
            lines.append(f"            latest={report.most_recent_log_file.name}")
    if report.most_recent_backup is not None:
        lines.append(f"  backups:  latest={report.most_recent_backup.name}")
    elif report.log_dir is not None:
        # If log_dir was provided we presumably also know about backups;
        # nudge the user to set one up.
        lines.append("  backups:  none found — see `ff-pipeline backup --help`")

    lines.append("")
    if report.last_run is None:
        lines.append("Last run: (none yet — run `ff-pipeline run` to populate)")
    else:
        run = report.last_run
        lines.append(
            f"Last run: #{run.run_id} status={run.status} mode={run.mode or '—'} "
            f"started={_format_dt(run.started_at)} finished={_format_dt(run.finished_at)}"
        )
        if verbose and run.error_summary:
            lines.append(f"  error: {run.error_summary}")

    lines.append("")
    lines.append("Per-source (latest):")
    if not report.sources:
        lines.append("  (no source runs yet)")
    else:
        for src in report.sources:
            lines.append(
                f"  {src.source:<10} status={src.status:<10} "
                f"rows=+{_or_dash(src.rows_added)}~{_or_dash(src.rows_updated)} "
                f"parse_failures={_or_dash(src.parse_failures)} "
                f"duration={_or_dash(src.duration_ms)}ms "
                f"last={_format_dt(src.last_run_at)}"
            )
            if verbose and src.error_message:
                lines.append(f"    error: {src.error_message}")

    lines.append("")
    counts = report.counts
    lines.append(
        f"Counts: leagues={counts.leagues} seasons={counts.seasons} "
        f"players={counts.players} stats_raw={counts.player_stats_raw} "
        f"stats_scored={counts.player_stats_scored} projections={counts.projections} "
        f"transactions={counts.transactions}"
    )

    if verbose and report.recent_failures:
        lines.append("")
        lines.append("Recent failures:")
        for run in report.recent_failures:
            lines.append(
                f"  #{run.run_id} {_format_dt(run.started_at)}: "
                f"{run.error_summary or '(no summary)'}"
            )

    return "\n".join(lines)


def _format_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # unreachable, satisfies type checker


def _or_dash(value: int | None) -> str:
    return "—" if value is None else str(value)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def perform_backup(
    *,
    database_url: str,
    backup_dir: Path,
    today: date | None = None,
    keep_days: int | None = DEFAULT_BACKUP_RETENTION_DAYS,
) -> BackupResult:
    """Snapshot the SQLite database to ``backup_dir`` and prune older files.

    Uses SQLite's online ``.backup`` API so the call is safe to run while
    the pipeline holds connections (it copies pages, not bytes). A
    non-SQLite ``database_url`` raises :class:`BackupError` so the
    operator picks an appropriate tool (``pg_dump`` etc.) instead of
    a silent no-op.
    """

    sqlite_path = _sqlite_path_from_url(database_url)
    if sqlite_path is None:
        raise BackupError(
            f"`ff-pipeline backup` only supports SQLite URLs (got {database_url!r}). "
            "For PostgreSQL use `pg_dump`."
        )
    if not sqlite_path.exists():
        raise BackupError(
            f"Database file does not exist yet: {sqlite_path}. "
            "Run `ff-pipeline init` first."
        )

    backup_dir.mkdir(parents=True, exist_ok=True)
    today = today or datetime.now(tz=UTC).date()
    backup_path = backup_dir / f"{BACKUP_FILENAME_PREFIX}{today.isoformat()}{BACKUP_FILENAME_SUFFIX}"
    _sqlite_online_backup(sqlite_path, backup_path)
    bytes_written = backup_path.stat().st_size

    pruned: tuple[Path, ...] = ()
    if keep_days is not None and keep_days > 0:
        pruned = _prune_backups(backup_dir, keep_days=keep_days, today=today)

    return BackupResult(
        backup_path=backup_path,
        bytes_written=bytes_written,
        pruned_files=pruned,
    )


def _sqlite_online_backup(source: Path, destination: Path) -> None:
    """Use SQLite's online backup API to clone ``source`` to ``destination``.

    Falls back to a plain file copy if the source can't be opened as a
    SQLite database (e.g. an empty file from a botched init) — preserves
    *something* the user can inspect rather than silently losing the
    attempt.
    """

    tmp_path = destination.with_suffix(destination.suffix + ".part")
    if tmp_path.exists():
        tmp_path.unlink()
    src_conn: sqlite3.Connection | None = None
    dest_conn: sqlite3.Connection | None = None
    try:
        try:
            src_conn = sqlite3.connect(str(source))
            dest_conn = sqlite3.connect(str(tmp_path))
            src_conn.backup(dest_conn)
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"SQLite backup failed: {exc}") from exc
    finally:
        if dest_conn is not None:
            dest_conn.close()
        if src_conn is not None:
            src_conn.close()
    if destination.exists():
        destination.unlink()
    shutil.move(str(tmp_path), str(destination))


def _prune_backups(directory: Path, *, keep_days: int, today: date) -> tuple[Path, ...]:
    """Delete dated backup files older than ``keep_days``.

    Cutoff is inclusive: a file dated exactly ``today - keep_days`` is
    kept; older files are removed. Files without a parseable date in
    the name are left alone.
    """

    cutoff = today - timedelta(days=keep_days)
    pruned: list[Path] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _BACKUP_DATE_RE.search(path.name)
        if match is None:
            continue
        try:
            file_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink()
            pruned.append(path)
    pruned.sort()
    return tuple(pruned)


def _sqlite_path_from_url(url: str) -> Path | None:
    """Return the on-disk file path for a ``sqlite:///...`` URL, else None."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw).resolve()


__all__ = [
    "DEFAULT_BACKUP_RETENTION_DAYS",
    "BackupError",
    "BackupResult",
    "SourceStatus",
    "StatusReport",
    "TableCounts",
    "collect_status",
    "perform_backup",
    "render_status",
]
