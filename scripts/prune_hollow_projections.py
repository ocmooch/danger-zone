#!/usr/bin/env python3
"""Delete hollow projection rows from the DB.

Sleeper returns full rosters of all-zero rows for seasons before its projection
coverage begins (~2018) and for players it didn't project in a covered week. A
backfill persisted ~250k of these, which advertise empty coverage and render as
bogus ``0.0`` projections downstream. This prunes every projection row that
carries no real forecast (no nonzero ``projected_points`` and no nonzero stat),
keeping only real projections. Idempotent; the Sleeper loader now skips hollow
rows on ingest, so this is a one-time cleanup of already-loaded data.

Usage:
    uv run python scripts/prune_hollow_projections.py
    uv run python scripts/prune_hollow_projections.py --dry-run
    uv run python scripts/prune_hollow_projections.py --db sqlite:///./data/fantasy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _is_hollow(stats: dict[str, float] | None, points: float | None) -> bool:
    if points not in (None, 0, 0.0):
        return False
    return not (
        stats and any(isinstance(v, (int, float)) and v not in (0, 0.0) for v in stats.values())
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune hollow projection rows.")
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    parser.add_argument("--dry-run", action="store_true", help="Report, but do not delete")
    args = parser.parse_args()

    from sqlalchemy import delete, func, select
    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.models import Projection
    from ff_pipeline.settings import get_settings

    db_url = args.db or get_settings().database_url
    engine = create_app_engine(db_url)

    hollow_ids: list[int] = []
    with Session(engine) as session:
        total = int(session.execute(select(func.count(Projection.projection_id))).scalar_one() or 0)
        for row in session.execute(
            select(
                Projection.projection_id,
                Projection.projected_stats,
                Projection.projected_points,
            )
        ).all():
            if _is_hollow(row.projected_stats, row.projected_points):
                hollow_ids.append(int(row.projection_id))

        print(
            f"projections: {total} total, {len(hollow_ids)} hollow, {total - len(hollow_ids)} real"
        )
        if args.dry_run:
            print("dry-run: no rows deleted")
            return
        if hollow_ids:
            for start in range(0, len(hollow_ids), 5000):
                batch = hollow_ids[start : start + 5000]
                session.execute(delete(Projection).where(Projection.projection_id.in_(batch)))
            session.commit()
        remaining = int(
            session.execute(select(func.count(Projection.projection_id))).scalar_one() or 0
        )
        print(f"deleted {len(hollow_ids)} hollow rows; {remaining} projection rows remain")

    engine.dispose()


if __name__ == "__main__":
    main()
