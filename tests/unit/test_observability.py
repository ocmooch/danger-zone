"""Unit tests for the M10 observability surface.

Covers:

* ``collect_status`` / ``render_status`` against an empty DB and against
  a DB seeded with one league, one season, and a small fan of pipeline
  runs (success + failure).
* ``perform_backup`` end-to-end: writes a dated file in the target
  directory, prunes older files by ``--keep-days``, rejects non-SQLite
  URLs, refuses to clobber when the source file is missing.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

from ff_pipeline.observability import (
    BackupError,
    collect_status,
    perform_backup,
    render_status,
)
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    PipelineRun,
    Player,
    PlayerStatsRaw,
    Season,
    SourceHealth,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'fantasy.db'}"


@pytest.fixture
def session(db_url: str) -> Iterator[Session]:
    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed_runs(session: Session) -> None:
    """One league, one season, one successful run, one failed run."""

    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    session.add(Season(league_id="36271", year=2025, status="in_progress"))
    player = Player(name_full="Test QB", position="QB", gsis_id="00-0000123")
    session.add(player)
    session.flush()
    session.add(
        PlayerStatsRaw(
            player_id=player.player_id,
            season_year=2025,
            week=1,
            source="nflverse",
            stats={"passing_yards": 250.0},
            is_primary=True,
            ingested_at=datetime.now(tz=UTC),
        )
    )

    # Older failed run for one source — should show up under recent_failures.
    older = PipelineRun(
        started_at=datetime(2025, 9, 1, 8, 0, tzinfo=UTC),
        finished_at=datetime(2025, 9, 1, 8, 0, 5, tzinfo=UTC),
        status="failed",
        mode="full_sync",
        error_summary="AuthFailureError: cookie expired",
    )
    session.add(older)
    session.flush()
    session.add(
        SourceHealth(
            run_id=older.run_id,
            source="nfl_com",
            status="failed",
            error_message="cookie expired",
            duration_ms=120,
        )
    )

    # Newer successful run for two sources.
    newer = PipelineRun(
        started_at=datetime(2025, 9, 2, 8, 0, tzinfo=UTC),
        finished_at=datetime(2025, 9, 2, 8, 1, tzinfo=UTC),
        status="success",
        mode="full_sync",
        sources_summary={"nflverse": {"stats_added": 100}},
    )
    session.add(newer)
    session.flush()
    session.add_all(
        [
            SourceHealth(
                run_id=newer.run_id,
                source="nflverse",
                status="success",
                rows_added=100,
                rows_updated=20,
                duration_ms=4321,
            ),
            SourceHealth(
                run_id=newer.run_id,
                source="nfl_com",
                status="success",
                rows_added=10,
                rows_updated=0,
                duration_ms=8765,
            ),
        ]
    )
    session.commit()


# ---------------------------------------------------------------------------
# Status — empty DB
# ---------------------------------------------------------------------------


def test_collect_status_on_empty_db(session: Session, db_url: str, tmp_path: Path) -> None:
    report = collect_status(
        session,
        database_url=db_url,
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )
    assert report.last_run is None
    assert report.sources == ()
    assert report.counts.leagues == 0
    assert report.counts.seasons == 0
    assert report.counts.players == 0
    assert report.recent_failures == ()


def test_render_status_empty(session: Session, db_url: str, tmp_path: Path) -> None:
    report = collect_status(
        session,
        database_url=db_url,
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )
    text = render_status(report)
    assert "Last run: (none yet" in text
    assert "no source runs yet" in text
    assert "leagues=0" in text


# ---------------------------------------------------------------------------
# Status — populated DB
# ---------------------------------------------------------------------------


def test_collect_status_picks_latest_run_and_per_source(
    session: Session, db_url: str, tmp_path: Path
) -> None:
    _seed_runs(session)
    report = collect_status(
        session,
        database_url=db_url,
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )
    assert report.last_run is not None
    assert report.last_run.status == "success"

    sources_by_name = {s.source: s for s in report.sources}
    # Latest per source: the newer success row wins for both.
    assert sources_by_name["nflverse"].status == "success"
    assert sources_by_name["nflverse"].rows_added == 100
    assert sources_by_name["nfl_com"].status == "success"

    # One historical failure surfaces.
    assert len(report.recent_failures) == 1
    assert report.recent_failures[0].error_summary == "AuthFailureError: cookie expired"


def test_render_status_verbose_includes_error_summary(
    session: Session, db_url: str, tmp_path: Path
) -> None:
    _seed_runs(session)
    report = collect_status(
        session,
        database_url=db_url,
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )
    text = render_status(report, verbose=True)
    assert "Recent failures:" in text
    assert "AuthFailureError" in text
    assert "nflverse" in text
    assert "nfl_com" in text


def test_render_status_non_verbose_hides_error_detail(
    session: Session, db_url: str, tmp_path: Path
) -> None:
    _seed_runs(session)
    report = collect_status(
        session,
        database_url=db_url,
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )
    text = render_status(report, verbose=False)
    assert "Recent failures:" not in text
    assert "AuthFailureError" not in text


def test_collect_status_surfaces_sqlite_file_size(
    session: Session, db_url: str, tmp_path: Path
) -> None:
    _seed_runs(session)
    report = collect_status(
        session,
        database_url=db_url,
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )
    assert report.sqlite_db_path is not None
    assert report.sqlite_db_size_bytes is not None
    assert report.sqlite_db_size_bytes > 0


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def test_perform_backup_writes_dated_file(tmp_path: Path) -> None:
    db_path = tmp_path / "fantasy.db"
    # Initialize a real SQLite database so the .backup API has pages to copy.
    engine = create_app_engine(f"sqlite:///{db_path}")
    upgrade_to_head(engine=engine)
    engine.dispose()

    backup_dir = tmp_path / "backups"
    today = date(2026, 5, 28)
    result = perform_backup(
        database_url=f"sqlite:///{db_path}",
        backup_dir=backup_dir,
        today=today,
    )
    assert result.backup_path == backup_dir / "fantasy-2026-05-28.db"
    assert result.backup_path.exists()
    assert result.bytes_written > 0
    # Backup file must be a valid SQLite database itself.
    conn = sqlite3.connect(str(result.backup_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    table_names = {r[0] for r in rows}
    assert "pipeline_runs" in table_names


def test_perform_backup_prunes_old_files(tmp_path: Path) -> None:
    db_path = tmp_path / "fantasy.db"
    engine = create_app_engine(f"sqlite:///{db_path}")
    upgrade_to_head(engine=engine)
    engine.dispose()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    # Two old files (one inside retention, one outside) + an unrelated file.
    today = date(2026, 5, 28)
    inside = backup_dir / f"fantasy-{(today - timedelta(days=10)).isoformat()}.db"
    outside = backup_dir / f"fantasy-{(today - timedelta(days=40)).isoformat()}.db"
    unrelated = backup_dir / "ignore-me.txt"
    inside.write_bytes(b"placeholder")
    outside.write_bytes(b"placeholder")
    unrelated.write_text("not a backup")

    result = perform_backup(
        database_url=f"sqlite:///{db_path}",
        backup_dir=backup_dir,
        today=today,
        keep_days=30,
    )
    assert outside in result.pruned_files
    assert inside not in result.pruned_files
    assert not outside.exists()
    assert inside.exists()
    assert unrelated.exists()


def test_perform_backup_prunes_milestone_backups_by_count(tmp_path: Path) -> None:
    db_path = tmp_path / "fantasy.db"
    engine = create_app_engine(f"sqlite:///{db_path}")
    upgrade_to_head(engine=engine)
    engine.dispose()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    # Three milestone snapshots with increasing mtimes + a dated daily that
    # must be left to the day-based sweep, not the milestone sweep.
    milestones = [
        backup_dir / "fantasy-premerge-20260101T000000Z.db",
        backup_dir / "fantasy-prenelson-20260201T000000Z.db",
        backup_dir / "fantasy-pre-identity-repair-20260301T000000Z.db",
    ]
    for i, path in enumerate(milestones):
        path.write_bytes(b"placeholder")
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
    daily = backup_dir / "fantasy-2026-01-15.db"
    daily.write_bytes(b"placeholder")

    result = perform_backup(
        database_url=f"sqlite:///{db_path}",
        backup_dir=backup_dir,
        today=date(2026, 5, 28),
        keep_days=None,
        keep_milestones=1,
    )
    # Oldest two milestones pruned; newest kept; dated daily untouched.
    assert milestones[0] in result.pruned_files
    assert milestones[1] in result.pruned_files
    assert not milestones[0].exists()
    assert not milestones[1].exists()
    assert milestones[2].exists()
    assert daily.exists()


def test_perform_backup_keep_milestones_none_keeps_all(tmp_path: Path) -> None:
    db_path = tmp_path / "fantasy.db"
    engine = create_app_engine(f"sqlite:///{db_path}")
    upgrade_to_head(engine=engine)
    engine.dispose()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old_milestone = backup_dir / "fantasy-premerge-20200101T000000Z.db"
    old_milestone.write_bytes(b"placeholder")

    result = perform_backup(
        database_url=f"sqlite:///{db_path}",
        backup_dir=backup_dir,
        today=date(2026, 5, 28),
        keep_days=None,
        keep_milestones=None,
    )
    assert result.pruned_files == ()
    assert old_milestone.exists()


def test_perform_backup_rejects_postgres(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="SQLite"):
        perform_backup(
            database_url="postgresql+psycopg://u:p@localhost/db",
            backup_dir=tmp_path / "backups",
        )


def test_perform_backup_errors_when_db_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    with pytest.raises(BackupError, match="does not exist"):
        perform_backup(
            database_url=f"sqlite:///{db_path}",
            backup_dir=tmp_path / "backups",
        )


def test_perform_backup_keep_days_none_skips_prune(tmp_path: Path) -> None:
    db_path = tmp_path / "fantasy.db"
    engine = create_app_engine(f"sqlite:///{db_path}")
    upgrade_to_head(engine=engine)
    engine.dispose()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    ancient = backup_dir / "fantasy-2000-01-01.db"
    ancient.write_bytes(b"placeholder")

    result = perform_backup(
        database_url=f"sqlite:///{db_path}",
        backup_dir=backup_dir,
        today=date(2026, 5, 28),
        keep_days=None,
    )
    assert result.pruned_files == ()
    assert ancient.exists()


__all__ = [
    "test_collect_status_on_empty_db",
    "test_collect_status_picks_latest_run_and_per_source",
    "test_collect_status_surfaces_sqlite_file_size",
    "test_perform_backup_errors_when_db_missing",
    "test_perform_backup_keep_days_none_skips_prune",
    "test_perform_backup_keep_milestones_none_keeps_all",
    "test_perform_backup_prunes_milestone_backups_by_count",
    "test_perform_backup_prunes_old_files",
    "test_perform_backup_rejects_postgres",
    "test_perform_backup_writes_dated_file",
    "test_render_status_empty",
    "test_render_status_non_verbose_hides_error_detail",
    "test_render_status_verbose_includes_error_summary",
]
