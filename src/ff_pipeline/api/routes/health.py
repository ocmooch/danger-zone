"""``GET /health`` (liveness) and ``GET /status`` (detail)."""

from __future__ import annotations

from fastapi import APIRouter

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.schemas import (
    Envelope,
    HealthResponse,
    SourceHealthSummary,
    StatusSummary,
)
from ff_pipeline.repository.queries import latest_pipeline_run, source_health_for_run

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/status", response_model=Envelope[StatusSummary])
def status(
    session: SessionDep,
) -> Envelope[StatusSummary]:
    run = latest_pipeline_run(session)
    if run is None:
        summary = StatusSummary()
    else:
        sources = source_health_for_run(session, run.run_id)
        summary = StatusSummary(
            last_run_id=run.run_id,
            last_run_status=run.status,
            last_run_started_at=run.started_at,
            last_run_finished_at=run.finished_at,
            sources=[
                SourceHealthSummary(
                    source=s.source,
                    status=s.status,
                    rows_added=s.rows_added,
                    rows_updated=s.rows_updated,
                    parse_failures=s.parse_failures,
                    error_message=s.error_message,
                    duration_ms=s.duration_ms,
                )
                for s in sources
            ],
        )
    return Envelope(data=summary, meta=build_meta(session))
