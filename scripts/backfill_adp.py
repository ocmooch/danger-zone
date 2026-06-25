#!/usr/bin/env python3
"""Backfill ``player_adp`` for every season from consensus ADP sources.

Pulls Average Draft Position from Fantasy Football Calculator + MyFantasyLeague
(Sleeper ADP is a deferred follow-up) for each season, resolves source players to
canonical ``players`` rows, and stores raw per-source rows. The dashboard blends
the sources and derives the reach/value delta downstream.

ADP is format-specific: 2010 is pulled half-PPR, every later season full-PPR, with
a loud fallback (half → standard) recorded on the row when a source can't serve
the target format. Historical ADP is immutable, so this is safe to re-run
(idempotent upsert). Hits the network — run against a copy first:

    cp data/fantasy.db /tmp/adp_copy.db
    uv run python scripts/backfill_adp.py --db sqlite:////tmp/adp_copy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill player_adp from FFC + MFL.")
    parser.add_argument("--start", type=int, default=2010, help="First season year (default 2010)")
    parser.add_argument("--end", type=int, default=2025, help="Last season year (default 2025)")
    parser.add_argument("--teams", type=int, default=12, help="League size for ADP (default 12)")
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    args = parser.parse_args()

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.adp.runner import run_adp
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db_url = args.db or settings.database_url
    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)

    try:
        with Session(engine) as session:
            for year in range(args.start, args.end + 1):
                result = run_adp(
                    session,
                    league_id=settings.nfl_league_id,
                    year=year,
                    teams=args.teams,
                )
                session.commit()
                if not result.outcomes:
                    print(f"{year}: no season row; skipped")
                    continue
                for o in result.outcomes:
                    fb = f" fallback→{o.actual_format}" if o.format_fallback else ""
                    fmt = o.actual_format or o.requested_format
                    print(
                        f"{year} {o.source} [{fmt}{fb}]: +{o.rows_added}~{o.rows_updated}, "
                        f"matched {o.matched}, unresolved {o.unresolved} ({o.status})"
                    )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
