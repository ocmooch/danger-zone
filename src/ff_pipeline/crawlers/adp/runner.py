"""Run the ADP crawler for one season across all configured sources.

For each source we walk the season's format fallback chain (full-PPR for
2011+, half-PPR for 2010; closest-first, standard last), take the first format
the source actually serves, resolve each entry to a canonical ``player_id``, and
upsert raw rows into ``player_adp``. Per-source ``source_health`` rows record
rows written, unresolved counts (``parse_failures``), and whether a format
fallback was used — so coverage and any silent-substitution risk are auditable.

The weighted multi-source blend + reach/value delta are computed downstream in
the dashboard; this layer only stores faithfully.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.crawlers.adp.format_map import fallback_chain, requested_format_for_year
from ff_pipeline.crawlers.adp.matcher import AdpPlayerMatcher
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import PipelineRun, PlayerAdp, Season, SourceHealth
from ff_pipeline.repository.upsert import upsert

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.adp.endpoints import AdpEntry, AdpSource

log = get_logger(__name__)

DEFAULT_TEAMS = 12
_CONFLICT_COLS = ("season_id", "source", "source_player_key")


@dataclass(frozen=True, slots=True)
class AdpSourceOutcome:
    """What one source contributed for one season."""

    source: str
    status: str  # 'success' | 'no_data' | 'failed'
    requested_format: str
    actual_format: str | None
    format_fallback: bool
    rows_added: int
    rows_updated: int
    matched: int
    unresolved: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AdpRunResult:
    """Outcome of one ADP run (one season, all sources)."""

    year: int
    season_id: int | None
    outcomes: list[AdpSourceOutcome]
    duration_ms: int


def run_adp(
    session: Session,
    *,
    league_id: str,
    year: int,
    sources: list[AdpSource] | None = None,
    teams: int = DEFAULT_TEAMS,
    mode: str = "full_sync",
) -> AdpRunResult:
    """Pull + store ADP for one season. Caller commits."""
    run = PipelineRun(status="running", mode=mode)
    session.add(run)
    session.flush()  # populate run.run_id for source_health FK
    start = time.perf_counter()

    season_id = session.execute(
        select(Season.season_id).where(Season.league_id == league_id, Season.year == year)
    ).scalar_one_or_none()

    try:
        with ExitStack() as stack:
            active = sources if sources is not None else _live_sources(stack)
            result = _do_run(
                session,
                run,
                start,
                year=year,
                season_id=season_id,
                sources=active,
                teams=teams,
            )
        return result
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        run.status = "failed"
        run.finished_at = datetime.now(tz=UTC)
        run.error_summary = f"{type(exc).__name__}: {exc}"
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source="adp",
                status="failed",
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        )
        raise


def _live_sources(stack: ExitStack) -> list[AdpSource]:
    """Default production sources: FFC + MFL + Sleeper.

    Imported lazily so the httpx-backed clients aren't constructed when a caller
    injects its own (fixture) sources — the common path in tests.
    """
    from ff_pipeline.crawlers.adp.ffc import LiveFfcSource
    from ff_pipeline.crawlers.adp.mfl import LiveMflSource
    from ff_pipeline.crawlers.adp.sleeper import LiveSleeperAdpSource

    return [
        stack.enter_context(LiveFfcSource()),
        stack.enter_context(LiveMflSource()),
        stack.enter_context(LiveSleeperAdpSource()),
    ]


def _do_run(
    session: Session,
    run: PipelineRun,
    start: float,
    *,
    year: int,
    season_id: int | None,
    sources: list[AdpSource],
    teams: int,
) -> AdpRunResult:
    requested = requested_format_for_year(year)

    if season_id is None:
        # No season row to attach ADP to — store nothing, say so plainly.
        duration_ms = int((time.perf_counter() - start) * 1000)
        run.status = "success"
        run.finished_at = datetime.now(tz=UTC)
        run.sources_summary = {"adp": {"year": year, "season_id": None, "sources": {}}}
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source="adp",
                status="no_data",
                error_message=f"no season row for year {year}",
                duration_ms=duration_ms,
            )
        )
        log.warning("adp run: no season row; nothing stored", year=year)
        return AdpRunResult(year=year, season_id=None, outcomes=[], duration_ms=duration_ms)

    matcher = AdpPlayerMatcher(session)
    outcomes: list[AdpSourceOutcome] = []
    summary: dict[str, object] = {}

    for source in sources:
        outcome = _run_one_source(
            session,
            source,
            year=year,
            season_id=season_id,
            requested=requested,
            teams=teams,
            matcher=matcher,
            run_id=run.run_id,
        )
        outcomes.append(outcome)
        summary[source.name] = {
            "status": outcome.status,
            "requested_format": outcome.requested_format,
            "actual_format": outcome.actual_format,
            "format_fallback": outcome.format_fallback,
            "rows_added": outcome.rows_added,
            "rows_updated": outcome.rows_updated,
            "matched": outcome.matched,
            "unresolved": outcome.unresolved,
        }

    duration_ms = int((time.perf_counter() - start) * 1000)
    run.status = "success"
    run.finished_at = datetime.now(tz=UTC)
    run.sources_summary = {"adp": {"year": year, "season_id": season_id, "sources": summary}}

    log.info("adp run complete", year=year, season_id=season_id, duration_ms=duration_ms)
    return AdpRunResult(year=year, season_id=season_id, outcomes=outcomes, duration_ms=duration_ms)


def _run_one_source(
    session: Session,
    source: AdpSource,
    *,
    year: int,
    season_id: int,
    requested: str,
    teams: int,
    matcher: AdpPlayerMatcher,
    run_id: int,
) -> AdpSourceOutcome:
    src_start = time.perf_counter()
    try:
        actual, entries = _fetch_with_fallback(source, year=year, requested=requested, teams=teams)
    except Exception as exc:
        log.warning("adp source failed", source=source.name, year=year, error=str(exc))
        session.add(
            SourceHealth(
                run_id=run_id,
                source=f"adp:{source.name}",
                status="failed",
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - src_start) * 1000),
            )
        )
        return AdpSourceOutcome(
            source=source.name,
            status="failed",
            requested_format=requested,
            actual_format=None,
            format_fallback=False,
            rows_added=0,
            rows_updated=0,
            matched=0,
            unresolved=0,
            error=str(exc),
        )

    if actual is None or not entries:
        session.add(
            SourceHealth(
                run_id=run_id,
                source=f"adp:{source.name}",
                status="no_data",
                rows_added=0,
                rows_updated=0,
                duration_ms=int((time.perf_counter() - src_start) * 1000),
            )
        )
        log.info("adp source no data", source=source.name, year=year, requested=requested)
        return AdpSourceOutcome(
            source=source.name,
            status="no_data",
            requested_format=requested,
            actual_format=None,
            format_fallback=False,
            rows_added=0,
            rows_updated=0,
            matched=0,
            unresolved=0,
        )

    fallback = actual != requested
    if fallback:
        log.warning(
            "adp format fallback (not the league's target format)",
            source=source.name,
            year=year,
            requested=requested,
            actual=actual,
        )

    now = datetime.now(tz=UTC)
    rows: list[dict[str, object]] = []
    matched = 0
    unresolved = 0
    for entry in entries:
        pid = matcher.match(entry, year=year)
        if pid is not None:
            matched += 1
        else:
            unresolved += 1
        rows.append(
            _row(
                entry,
                season_id=season_id,
                player_id=pid,
                requested=requested,
                actual=actual,
                fallback=fallback,
                teams=teams,
                run_id=run_id,
                now=now,
            )
        )

    counts = upsert(session, PlayerAdp, rows, conflict_cols=_CONFLICT_COLS)
    session.add(
        SourceHealth(
            run_id=run_id,
            source=f"adp:{source.name}",
            status="success",
            rows_added=counts.rows_added,
            rows_updated=counts.rows_updated,
            parse_failures=unresolved,  # reuse the column for "unresolved players"
            duration_ms=int((time.perf_counter() - src_start) * 1000),
        )
    )
    log.info(
        "adp source stored",
        source=source.name,
        year=year,
        actual_format=actual,
        format_fallback=fallback,
        rows_added=counts.rows_added,
        rows_updated=counts.rows_updated,
        matched=matched,
        unresolved=unresolved,
    )
    return AdpSourceOutcome(
        source=source.name,
        status="success",
        requested_format=requested,
        actual_format=actual,
        format_fallback=fallback,
        rows_added=counts.rows_added,
        rows_updated=counts.rows_updated,
        matched=matched,
        unresolved=unresolved,
    )


def _fetch_with_fallback(
    source: AdpSource, *, year: int, requested: str, teams: int
) -> tuple[str | None, list[AdpEntry]]:
    """Walk the format chain; first format the source serves wins."""
    for fmt in fallback_chain(requested):
        entries = source.fetch(year=year, fmt=fmt, teams=teams)
        if entries:
            return fmt, entries
    return None, []


def _row(
    entry: AdpEntry,
    *,
    season_id: int | None,
    player_id: int | None,
    requested: str,
    actual: str,
    fallback: bool,
    teams: int,
    run_id: int,
    now: datetime,
) -> dict[str, object]:
    return {
        "season_id": season_id,
        "player_id": player_id,
        "source": entry.source,
        "source_player_key": entry.source_player_key,
        "source_player_name": entry.name,
        "source_position": entry.position,
        "source_nfl_team": entry.nfl_team,
        "requested_format": requested,
        "actual_format": actual,
        "format_fallback": fallback,
        "teams": teams,
        "adp": entry.adp,
        "adp_stdev": entry.adp_stdev,
        "adp_high": entry.adp_high,
        "adp_low": entry.adp_low,
        "times_drafted": entry.times_drafted,
        "pulled_at": now,
        "run_id": run_id,
    }


__all__ = ["AdpRunResult", "AdpSourceOutcome", "run_adp"]
