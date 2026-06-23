#!/usr/bin/env python3
"""Backfill nflverse injury reports for all available seasons (2009-2025).

Repeatable and idempotent — safe to re-run if interrupted. Reads live data
from nflreadpy and upserts into player_injury_reports via ON CONFLICT semantics.

Usage:
    uv run python scripts/backfill_injury_reports.py
    uv run python scripts/backfill_injury_reports.py --start 2020 --end 2024
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make the src layout importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill injury reports from nflverse.")
    parser.add_argument("--start", type=int, default=2009, help="First season year (default 2009)")
    parser.add_argument(
        "--end", type=int, default=2025, help="Last season year inclusive (default 2025)"
    )
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    args = parser.parse_args()

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.injury_runner import run_injury_reports
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db_url = args.db or settings.database_url
    print(f"Database: {db_url}")

    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)

    seasons = list(range(args.start, args.end + 1))
    print(f"Backfilling injury reports for {len(seasons)} seasons: {seasons[0]}-{seasons[-1]}")

    total_added = 0
    total_updated = 0
    wall_start = time.perf_counter()

    # Process in batches of 5 seasons to keep memory reasonable and give
    # useful progress output.
    batch_size = 5
    for i in range(0, len(seasons), batch_size):
        batch = seasons[i : i + batch_size]
        print(f"  Fetching seasons {batch[0]}-{batch[-1]} ...", end=" ", flush=True)
        with Session(engine) as ss:
            try:
                result = run_injury_reports(ss, seasons=batch)
                ss.commit()
                total_added += result.rows_added
                total_updated += result.rows_updated
                print(f"+{result.rows_added} ~{result.rows_updated} ({result.duration_ms} ms)")
            except Exception as exc:
                print(f"FAILED: {exc}")
                engine.dispose()
                raise

    wall_ms = int((time.perf_counter() - wall_start) * 1000)
    engine.dispose()
    print(f"\nDone. Total rows added={total_added}, updated={total_updated} in {wall_ms} ms.")


if __name__ == "__main__":
    main()
