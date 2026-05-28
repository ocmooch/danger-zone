"""Unit tests for the M9 backfill orchestrator.

These cover the resumability logic (already-completed steps get
skipped), per-season commit boundaries, and the auth-failure → clean
abort path. The real runners are swapped out for stubs so the test
doesn't need fixture HTML or parquet."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session

from ff_pipeline import backfill as backfill_module
from ff_pipeline.backfill import (
    BACKFILL_MODE,
    run_backfill,
)
from ff_pipeline.crawlers.nfl_com.client import AuthFailureError
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import PipelineRun, SourceHealth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


# ---------------------------------------------------------------------------
# Stubs that mirror the runner contract
# ---------------------------------------------------------------------------


@dataclass
class _StubNflverseResult:
    players_added: int = 1
    players_updated: int = 0
    stats_added: int = 1
    stats_updated: int = 0
    duration_ms: int = 1


@dataclass
class _StubNflComResult:
    owners_added: int = 1
    owners_updated: int = 0
    teams_added: int = 1
    teams_updated: int = 0
    rosters_added: int = 1
    rosters_updated: int = 0
    matchups_added: int = 1
    matchups_updated: int = 0
    transactions_added: int = 0
    transactions_updated: int = 0
    availability_added: int = 0
    availability_updated: int = 0


def _stub_nflverse_runner(
    raise_on_year: int | None = None,
    raise_kind: type[Exception] = RuntimeError,
) -> Any:
    """Build a stub `run_nflverse(...)` that writes the same pipeline_runs
    shape the real one does — so the resumability check has data to read."""

    def _stub(
        session: Session,
        *,
        seasons: Any,
        mode: str = "full_sync",
        source: Any = None,
    ) -> _StubNflverseResult:
        _ = source
        year = seasons[0]
        if raise_on_year is not None and year == raise_on_year:
            raise raise_kind(f"boom on {year}")
        run = PipelineRun(
            status="success",
            mode=mode,
            sources_summary={
                "nflverse": {
                    "players_added": 1,
                    "players_updated": 0,
                    "stats_added": 1,
                    "stats_updated": 0,
                    "seasons": [year],
                }
            },
        )
        session.add(run)
        session.flush()
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source="nflverse",
                status="success",
                rows_added=2,
                rows_updated=0,
                duration_ms=1,
            )
        )
        return _StubNflverseResult()

    return _stub


def _stub_nfl_com_runner(
    raise_on_year: int | None = None,
    raise_kind: type[Exception] = RuntimeError,
) -> Any:
    def _stub(
        session: Session,
        *,
        league_id: str,
        year: int,
        week: int,
        fetcher: Any,
        snapshot_kind: Any = None,
        now: Any = None,
        mode: str = "full_sync",
    ) -> _StubNflComResult:
        _ = (league_id, fetcher, snapshot_kind, now)
        if raise_on_year is not None and year == raise_on_year:
            raise raise_kind(f"auth boom on {year}")
        run = PipelineRun(
            status="success",
            mode=mode,
            sources_summary={
                "nfl_com": {
                    "year": year,
                    "week": week,
                    "owners_added": 1,
                    "owners_updated": 0,
                    "teams_added": 1,
                    "teams_updated": 0,
                    "rosters_added": 1,
                    "rosters_updated": 0,
                    "matchups_added": 1,
                    "matchups_updated": 0,
                    "transactions_added": 0,
                    "transactions_updated": 0,
                    "availability_added": 0,
                    "availability_updated": 0,
                }
            },
        )
        session.add(run)
        session.flush()
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source="nfl_com",
                status="success",
                rows_added=4,
                rows_updated=0,
                duration_ms=1,
            )
        )
        return _StubNflComResult()

    return _stub


class _StubClient:
    """Minimal stand-in for NflComClient — only the close() contract is used."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _stub_client_factory(client: _StubClient) -> Any:
    def _factory(cookie: str, delay: float) -> _StubClient:
        _ = (cookie, delay)
        return client

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backfill_runs_each_source_per_season(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill_module, "run_nflverse", _stub_nflverse_runner())
    monkeypatch.setattr(backfill_module, "run_nfl_com", _stub_nfl_com_runner())
    client = _StubClient()

    result = run_backfill(
        session,
        league_id="36271",
        start_year=2014,
        end_year=2015,
        cookie_value="cookie-stub",
        sources=("nflverse", "nfl_com"),
        nfl_com_client_factory=_stub_client_factory(client),
    )

    assert result.failed == 0
    assert result.completed == 4  # 2 sources x 2 years
    assert result.skipped == 0
    # Sources cleaned up.
    assert client.closed is True
    # All pipeline_runs were written with mode='backfill'.
    runs = session.query(PipelineRun).all()
    assert {r.mode for r in runs} == {BACKFILL_MODE}


def test_backfill_skips_completed_years(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill_module, "run_nflverse", _stub_nflverse_runner())
    monkeypatch.setattr(backfill_module, "run_nfl_com", _stub_nfl_com_runner())
    client = _StubClient()
    # First pass to land successful runs.
    run_backfill(
        session,
        league_id="36271",
        start_year=2020,
        end_year=2020,
        cookie_value="cookie-stub",
        sources=("nflverse", "nfl_com"),
        nfl_com_client_factory=_stub_client_factory(client),
    )

    # Second pass over the same year should skip both.
    result = run_backfill(
        session,
        league_id="36271",
        start_year=2020,
        end_year=2020,
        cookie_value="cookie-stub",
        sources=("nflverse", "nfl_com"),
        nfl_com_client_factory=_stub_client_factory(_StubClient()),
    )
    assert result.completed == 0
    assert result.skipped == 2
    assert all(o.status == "skipped" for o in result.per_season)


def test_backfill_force_overrides_skip(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill_module, "run_nflverse", _stub_nflverse_runner())
    monkeypatch.setattr(backfill_module, "run_nfl_com", _stub_nfl_com_runner())
    run_backfill(
        session,
        league_id="36271",
        start_year=2020,
        end_year=2020,
        cookie_value="cookie-stub",
        sources=("nflverse",),
    )
    result = run_backfill(
        session,
        league_id="36271",
        start_year=2020,
        end_year=2020,
        cookie_value="cookie-stub",
        sources=("nflverse",),
        force=True,
    )
    assert result.completed == 1
    assert result.skipped == 0


def test_backfill_aborts_cleanly_on_auth_failure(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill_module, "run_nflverse", _stub_nflverse_runner())
    monkeypatch.setattr(
        backfill_module,
        "run_nfl_com",
        _stub_nfl_com_runner(raise_on_year=2015, raise_kind=AuthFailureError),
    )
    client = _StubClient()

    result = run_backfill(
        session,
        league_id="36271",
        start_year=2014,
        end_year=2016,
        cookie_value="cookie-stub",
        sources=("nflverse", "nfl_com"),
        nfl_com_client_factory=_stub_client_factory(client),
    )

    assert result.aborted_at == ("nfl_com", 2015)
    # Everything up to but not including 2015 nfl_com should be completed.
    statuses = [(o.source, o.year, o.status) for o in result.per_season]
    assert ("nflverse", 2014, "completed") in statuses
    assert ("nfl_com", 2014, "completed") in statuses
    assert ("nflverse", 2015, "completed") in statuses
    assert ("nfl_com", 2015, "failed") in statuses
    # 2016 not attempted — abort short-circuits.
    assert all(year != 2016 for _, year, _ in statuses)
    assert client.closed is True

    # And resumability picks up where the failure stopped — re-run with a
    # fixed runner advances past 2015's auth-failed step (still pending).
    monkeypatch.setattr(backfill_module, "run_nfl_com", _stub_nfl_com_runner())
    result2 = run_backfill(
        session,
        league_id="36271",
        start_year=2014,
        end_year=2016,
        cookie_value="cookie-stub",
        sources=("nflverse", "nfl_com"),
        nfl_com_client_factory=_stub_client_factory(_StubClient()),
    )
    # 2014 + 2015 nflverse + 2014 nfl_com all skipped (prior successes);
    # 2015 nfl_com runs (now succeeds), 2016 both sources run.
    skipped = [o for o in result2.per_season if o.status == "skipped"]
    completed = [o for o in result2.per_season if o.status == "completed"]
    assert len(skipped) == 3
    assert len(completed) == 3
    assert {(o.source, o.year) for o in completed} == {
        ("nfl_com", 2015),
        ("nflverse", 2016),
        ("nfl_com", 2016),
    }


def test_backfill_requires_cookie_when_nfl_com_in_sources(session: Session) -> None:
    with pytest.raises(ValueError, match="cookie_value is required"):
        run_backfill(
            session,
            league_id="36271",
            start_year=2020,
            end_year=2020,
            cookie_value=None,
            sources=("nfl_com",),
        )


def test_backfill_rejects_bad_year_range(session: Session) -> None:
    with pytest.raises(ValueError, match="start_year"):
        run_backfill(
            session,
            league_id="36271",
            start_year=2025,
            end_year=2014,
            cookie_value=None,
            sources=("nflverse",),
        )


def test_backfill_existing_progress_handles_malformed_rows(
    session: Session,
) -> None:
    """A pipeline_runs row with a non-dict sources_summary is ignored
    rather than crashing the resumability check — defensive against
    older schema rows / hand-edited DB state."""
    session.add(
        PipelineRun(
            status="success",
            mode=BACKFILL_MODE,
            sources_summary={"nflverse": "not a dict"},
        )
    )
    session.add(
        PipelineRun(
            status="success",
            mode=BACKFILL_MODE,
            sources_summary=None,
        )
    )
    session.commit()
    # Should not raise; should return an empty set.
    progress: set[tuple[Any, ...]] = backfill_module._existing_backfill_progress(session)
    assert progress == set()


__all__ = [
    "test_backfill_aborts_cleanly_on_auth_failure",
    "test_backfill_existing_progress_handles_malformed_rows",
    "test_backfill_force_overrides_skip",
    "test_backfill_rejects_bad_year_range",
    "test_backfill_requires_cookie_when_nfl_com_in_sources",
    "test_backfill_runs_each_source_per_season",
    "test_backfill_skips_completed_years",
]
