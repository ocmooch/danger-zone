#!/usr/bin/env python3
"""Backfill ``transactions.effective_week`` where NFL.com left the week blank.

NFL.com's transaction pages render the week in a ``.transactionWeek`` cell, but
for some historical seasons (notably 2010's early weeks) that cell is empty, so
``effective_week`` is NULL even though ``executed_at`` is present. Downstream
consumers that compare a transaction's week to a roster snapshot week (e.g. the
dashboard's "roster drift" check) then misread those moves. This script
reconstructs the week from ``executed_at`` using each season's regular-season
schedule (nflverse ``load_schedules``).

Repeatable and idempotent: only rows with a NULL ``effective_week`` and a
non-NULL ``executed_at`` are touched, and re-running converges (filled rows no
longer match). Drafts (``effective_week = 0``) and post-season-dated moves
(which map to no regular-season week) are left untouched.

Usage:
    uv run python scripts/backfill_transaction_weeks.py --dry-run
    uv run python scripts/backfill_transaction_weeks.py
    uv run python scripts/backfill_transaction_weeks.py --start 2010 --end 2010
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the src layout importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill transaction effective_week from dates.")
    parser.add_argument("--start", type=int, default=2009, help="First season year (default 2009)")
    parser.add_argument(
        "--end", type=int, default=2025, help="Last season year inclusive (default 2025)"
    )
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing"
    )
    args = parser.parse_args()

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.client import LiveNflverseSource
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.normalizer.transaction_weeks import (
        effective_week_for_date,
        week_last_gamedays,
    )
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.repository.models import Season, Transaction
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db_url = args.db or settings.database_url
    print(f"Database: {db_url}")
    if args.dry_run:
        print("DRY RUN — no rows will be written.")

    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)
    source = LiveNflverseSource()

    total_filled = 0
    total_skipped = 0  # NULL-week rows whose date mapped to no regular-season week

    with Session(engine) as session:
        seasons = session.execute(
            select(Season.season_id, Season.year)
            .where(Season.year >= args.start, Season.year <= args.end)
            .order_by(Season.year)
        ).all()

        for season_id, year in seasons:
            pending = list(
                session.execute(
                    select(Transaction).where(
                        Transaction.season_id == season_id,
                        Transaction.effective_week.is_(None),
                        Transaction.executed_at.is_not(None),
                    )
                )
                .scalars()
                .all()
            )
            if not pending:
                continue

            schedule = source.load_schedules([year])
            last_gamedays = week_last_gamedays(schedule.to_dicts())
            if not last_gamedays:
                print(f"  {year}: {len(pending)} NULL-week rows but no schedule — skipped")
                continue

            filled = 0
            skipped = 0
            for txn in pending:
                week = effective_week_for_date(txn.executed_at.date(), last_gamedays)
                if week is None:
                    skipped += 1
                    continue
                if not args.dry_run:
                    txn.effective_week = week
                filled += 1

            total_filled += filled
            total_skipped += skipped
            print(f"  {year}: filled {filled}, skipped {skipped} (post-season-dated)")

        if not args.dry_run:
            session.commit()

    verb = "would fill" if args.dry_run else "filled"
    print(f"\nDone — {verb} {total_filled} transactions; {total_skipped} left NULL (post-season).")


if __name__ == "__main__":
    main()
