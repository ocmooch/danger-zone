"""Build the ``Meta`` envelope object for API responses.

The contract requires ``last_updated``, ``source``, and
``pipeline_run_id`` on every response. We derive these from the most
recent ``pipeline_runs`` row plus an optional caller-supplied entity
timestamp (which takes precedence over the run's timestamp).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ff_pipeline.api.schemas import Meta
from ff_pipeline.repository.queries import latest_pipeline_run

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session


def build_meta(
    session: Session,
    *,
    entity_updated_at: datetime | None = None,
    source: str = "pipeline",
) -> Meta:
    run = latest_pipeline_run(session)
    last_updated = entity_updated_at
    run_source = source
    if run is not None:
        if last_updated is None:
            last_updated = run.finished_at or run.started_at
        if source == "pipeline" and run.mode:
            run_source = run.mode
    return Meta(
        last_updated=last_updated,
        source=run_source,
        pipeline_run_id=run.run_id if run else None,
    )
