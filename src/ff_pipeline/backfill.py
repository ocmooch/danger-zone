"""Multi-season backfill orchestrator.

Walks ``start_year..end_year``, runs each enabled crawler per season, and
records progress in ``pipeline_runs`` so a partial run can be resumed.

Resumability contract
---------------------

The unit of work is one ``(source, year)`` tuple. For each tuple we
check whether a successful ``pipeline_runs`` row exists whose
``mode='backfill'`` *and* whose ``sources_summary`` records that source
+ year. If yes, skip (unless ``force=True``). If no, call the
corresponding runner with ``mode='backfill'``. Failures abort the
backfill cleanly — the partial DB state is intentionally preserved so
the next run picks up where the failure happened.

Auth-failure handling
---------------------

``AuthFailureError`` from the NFL.com client is the most likely
failure mode mid-backfill. It propagates out of the runner, which has
already rolled its own ``pipeline_runs`` row to ``failed`` via the
existing try/except. The orchestrator catches it once more so the
*caller* (CLI) sees a typed sentinel and can exit with the right code,
and commits the per-season transactions that did succeed before the
failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from ff_pipeline.crawlers.nfl_com.client import AuthFailureError, NflComClient
from ff_pipeline.crawlers.nfl_com.league import run_nfl_com
from ff_pipeline.crawlers.nflverse.runner import run_nflverse
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import PipelineRun

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

log = get_logger(__name__)

BackfillSource = Literal["nflverse", "nfl_com"]
DEFAULT_SOURCES: tuple[BackfillSource, ...] = ("nflverse", "nfl_com")
BACKFILL_MODE = "backfill"


@dataclass(frozen=True, slots=True)
class SeasonOutcome:
    """One ``(source, year)`` step's outcome."""

    source: BackfillSource
    year: int
    status: Literal["completed", "skipped", "failed"]
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Aggregate counts surfaced to the CLI.

    ``per_season`` preserves chronological order so the CLI can render a
    readable summary even when the run aborted partway through.
    """

    per_season: tuple[SeasonOutcome, ...] = field(default_factory=tuple)
    aborted_at: tuple[BackfillSource, int] | None = None

    @property
    def completed(self) -> int:
        return sum(1 for o in self.per_season if o.status == "completed")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.per_season if o.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.per_season if o.status == "failed")


def run_backfill(
    session: Session,
    *,
    league_id: str,
    start_year: int,
    end_year: int,
    cookie_value: str | None,
    delay_seconds: float = 2.0,
    sources: Sequence[BackfillSource] = DEFAULT_SOURCES,
    week: int = 1,
    force: bool = False,
    nfl_com_client_factory: object | None = None,
) -> BackfillResult:
    """Backfill every season in ``[start_year, end_year]`` for the given sources.

    Caller commits between successful seasons (we commit per-season so a
    partial failure preserves the work done up to that point).
    ``nfl_com_client_factory`` is a test seam — pass a callable returning
    a context-managed client to inject a stub.
    """

    if start_year > end_year:
        raise ValueError(
            f"start_year ({start_year}) must be <= end_year ({end_year}) for backfill"
        )

    already_done = _existing_backfill_progress(session)
    outcomes: list[SeasonOutcome] = []
    aborted_at: tuple[BackfillSource, int] | None = None

    nfl_com_needed = "nfl_com" in sources
    nfl_com_client_ctx: NflComClient | None = None
    if nfl_com_needed:
        if not cookie_value:
            raise ValueError("cookie_value is required when sources includes 'nfl_com'")
        if nfl_com_client_factory is None:
            nfl_com_client_ctx = NflComClient(cookie=cookie_value, delay_seconds=delay_seconds)
        else:
            # Test seam: factory is `(cookie, delay) -> context-managed client`.
            nfl_com_client_ctx = nfl_com_client_factory(cookie_value, delay_seconds)  # type: ignore[operator]

    try:
        for year in range(start_year, end_year + 1):
            for source in sources:
                key = (source, year)
                if not force and key in already_done:
                    outcomes.append(
                        SeasonOutcome(source=source, year=year, status="skipped",
                                      detail="prior backfill run succeeded")
                    )
                    log.info(
                        "Backfill skipping already-completed step",
                        source=source,
                        year=year,
                    )
                    continue
                try:
                    detail = _run_one(
                        session,
                        source=source,
                        year=year,
                        league_id=league_id,
                        week=week,
                        nfl_com_client=nfl_com_client_ctx,
                    )
                except AuthFailureError as exc:
                    # Per-season failure already wrote a failed pipeline_runs
                    # row; commit anything queued before this step ran,
                    # surface a clean abort to the CLI.
                    session.commit()
                    outcomes.append(
                        SeasonOutcome(source=source, year=year, status="failed",
                                      detail=f"AuthFailureError: {exc}")
                    )
                    aborted_at = key
                    log.warning(
                        "Backfill aborted by auth failure",
                        source=source,
                        year=year,
                    )
                    return BackfillResult(per_season=tuple(outcomes), aborted_at=aborted_at)
                except Exception as exc:
                    session.commit()
                    outcomes.append(
                        SeasonOutcome(source=source, year=year, status="failed",
                                      detail=f"{type(exc).__name__}: {exc}")
                    )
                    aborted_at = key
                    log.error(
                        "Backfill aborted",
                        source=source,
                        year=year,
                        error=str(exc),
                    )
                    return BackfillResult(per_season=tuple(outcomes), aborted_at=aborted_at)
                outcomes.append(
                    SeasonOutcome(source=source, year=year, status="completed", detail=detail)
                )
                # Commit per successful step so a later failure doesn't
                # roll back the work we just did.
                session.commit()
    finally:
        if nfl_com_client_ctx is not None:
            close = getattr(nfl_com_client_ctx, "close", None)
            if callable(close):
                close()

    return BackfillResult(per_season=tuple(outcomes), aborted_at=aborted_at)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_one(
    session: Session,
    *,
    source: BackfillSource,
    year: int,
    league_id: str,
    week: int,
    nfl_com_client: NflComClient | None,
) -> str:
    if source == "nflverse":
        nv_result = run_nflverse(session, seasons=[year], mode=BACKFILL_MODE)
        return (
            f"players +{nv_result.players_added}~{nv_result.players_updated}, "
            f"stats +{nv_result.stats_added}~{nv_result.stats_updated}"
        )
    if source == "nfl_com":
        if nfl_com_client is None:
            raise RuntimeError("nfl_com backfill requires a configured NflComClient")
        nc_result = run_nfl_com(
            session,
            league_id=league_id,
            year=year,
            week=week,
            fetcher=nfl_com_client,
            mode=BACKFILL_MODE,
        )
        return (
            f"owners +{nc_result.owners_added}~{nc_result.owners_updated}, "
            f"teams +{nc_result.teams_added}~{nc_result.teams_updated}, "
            f"rosters +{nc_result.rosters_added}~{nc_result.rosters_updated}, "
            f"matchups +{nc_result.matchups_added}~{nc_result.matchups_updated}, "
            f"transactions +{nc_result.transactions_added}~{nc_result.transactions_updated}, "
            f"availability +{nc_result.availability_added}~{nc_result.availability_updated}"
        )
    raise ValueError(f"Unknown backfill source: {source!r}")


def _existing_backfill_progress(session: Session) -> set[tuple[BackfillSource, int]]:
    """Return ``{(source, year)}`` tuples already completed successfully.

    Walks every ``pipeline_runs`` row with ``mode='backfill'`` and
    ``status='success'`` and reads each ``sources_summary`` entry. The
    nflverse runner stores ``"seasons": [year, ...]`` while the nfl_com
    runner stores ``"year": year`` directly; both shapes are decoded here.
    """

    out: set[tuple[BackfillSource, int]] = set()
    stmt = select(PipelineRun).where(
        PipelineRun.mode == BACKFILL_MODE,
        PipelineRun.status == "success",
    )
    for run in session.execute(stmt).scalars():
        summary = run.sources_summary or {}
        if not isinstance(summary, dict):
            continue
        for src_name, payload in summary.items():
            if not isinstance(payload, dict):
                continue
            if src_name == "nflverse":
                seasons = payload.get("seasons") or []
                if isinstance(seasons, list):
                    for y in seasons:
                        if isinstance(y, int):
                            out.add(("nflverse", y))
            elif src_name == "nfl_com":
                y = payload.get("year")
                if isinstance(y, int):
                    out.add(("nfl_com", y))
    return out


__all__ = [
    "BACKFILL_MODE",
    "DEFAULT_SOURCES",
    "BackfillResult",
    "BackfillSource",
    "SeasonOutcome",
    "run_backfill",
]
