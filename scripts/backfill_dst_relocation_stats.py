#!/usr/bin/env python3
"""Rebuild team-defense (DST) raw stats so relocated-franchise games join, then re-score.

The team-defense rollup (``crawlers/nflverse/team_defense.py``) combines three
nflverse frames, but they disagree on relocated-franchise codes:
``load_team_stats`` and ``load_pbp`` normalize every season to the *current*
code (``LAC``/``LV``/``LA``) while ``load_schedules`` keeps the *era* code
(``SD`` pre-2017, ``OAK`` pre-2020, ``STL`` pre-2016). The schedule-sourced
opponent identity therefore never matched the team-stats index for those games,
so ``points_allowed`` / ``total_yards_allowed`` (and the opponent-sourced
``sacks``) silently dropped for both the relocated team **and** its opponents.
That left ~300 DEF ``player_stats_raw`` rows under-scored against the
authoritative ``team_rosters.extra_data.nfl_com_points`` — the bulk of the deep
``dst-yards-sacks-pipeline-gap``.

The fix (already landed in ``team_defense.py``) folds every frame's team code
through :func:`canonical_franchise`, so the two frames key on one stable code.
This script re-runs the corrected team-defense ingest for the requested seasons
(an idempotent upsert: deterministic non-relocation rows reproduce identically,
relocation rows gain their missing bracket stats) and re-scores. The DST TD
undercount (nflverse ``def_tds`` / ``special_teams_tds`` miss some scores) is a
*separate* gap and is deliberately out of scope here.

Idempotent. Run against a copy first and confirm the relocation canaries close
before touching live data:
    cp data/fantasy.db /tmp/dst_copy.db
    uv run python scripts/backfill_dst_relocation_stats.py --db sqlite:////tmp/dst_copy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild relocated-franchise DST stats and re-score."
    )
    parser.add_argument("--start", type=int, default=2010, help="First season year (default 2010)")
    parser.add_argument("--end", type=int, default=2025, help="Last season year (default 2025)")
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    parser.add_argument(
        "--no-rescore", action="store_true", help="Re-ingest DST raw rows only; skip the re-score"
    )
    args = parser.parse_args()

    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.runner import run_team_defense
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.scoring.rescore import rescore_seasons
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db_url = args.db or settings.database_url
    print(f"Database: {db_url}")

    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)

    years = list(range(args.start, args.end + 1))
    with Session(engine) as session:
        result = run_team_defense(session, seasons=years, mode="dst_relocation_backfill")
        session.commit()
        print(
            f"team-defense re-ingest: added {result.stats_added}, updated {result.stats_updated}, "
            f"matched {result.teams_matched}, unmatched {result.teams_unmatched}"
        )

        if not args.no_rescore:
            print(f"Re-scoring {years[0]}-{years[-1]} ...")
            rescore = rescore_seasons(session, season_years=years)
            session.commit()
            print(
                f"  rescored rows: {rescore.rows_scored}, updated: {rescore.rows_updated}, "
                f"unchanged: {rescore.rows_unchanged}"
            )

    print("\nDone.")


if __name__ == "__main__":
    main()
