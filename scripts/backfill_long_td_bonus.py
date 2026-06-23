#!/usr/bin/env python3
"""Backfill nflverse long-TD bonus counts into ``player_stats_raw`` and re-score.

The scoring engine, the per-season ``scoring_rules`` (including the long-TD tiers
``*_bonus_long_td_40`` = +1 and ``*_bonus_long_td_50`` = +3, which stack), and
the crawler's PBP-derived long-TD merge (``NflverseClient.player_stats``) are all
already correct. But the historical ``player_stats_raw`` rows for 2010-2024 were
ingested *before* that long-TD wiring landed, so their stats dicts carry no
``*_bonus_long_td_*`` keys — the long-TD per-unit rules therefore score 0 and
``total_points`` understates every game with a 40+/50+ yard offensive TD. (2025
was crawled after the wiring and is already correct, which is the control: it has
zero offensive divergence from ``nfl_com_points``.)

This is the offensive half of the bonus-scoring fidelity gap (F-27 / see
``dz-dashboard/docs/handoffs/bonus-scoring-rescore.md``). It derives the long-TD
counts from nflverse play-by-play (the same ``derive_long_td_bonus_counts`` the
crawler uses), merges them into the existing offensive raw rows, and re-scores —
closing the QB/WR/RB/TE divergence to within the verify tolerance of the
authoritative ``team_rosters.extra_data.nfl_com_points``. Raw *game facts* only
(TD-distance counts) are added; no bonus points are written onto raw rows. The
DST (DEF) divergence and the reconstruction-over-count (negative-delta) class are
separate gaps and are deliberately out of scope here.

Idempotent: merging the same counts and re-scoring converge. Run against a copy
first; validate the canary (Vick 2010 wk10 -> 63.32) before touching live data.

Usage:
    uv run python scripts/backfill_long_td_bonus.py --dry-run
    uv run python scripts/backfill_long_td_bonus.py --start 2010 --end 2024
    uv run python scripts/backfill_long_td_bonus.py --db sqlite:////tmp/copy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill long-TD bonus counts and re-score.")
    parser.add_argument("--start", type=int, default=2010, help="First season year (default 2010)")
    parser.add_argument("--end", type=int, default=2024, help="Last season year (default 2024)")
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing"
    )
    parser.add_argument(
        "--no-rescore", action="store_true", help="Merge raw counts only; skip the re-score"
    )
    args = parser.parse_args()

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.client import LiveNflverseSource
    from ff_pipeline.crawlers.nflverse.long_td_bonus import (
        LONG_TD_BONUS_ZEROES,
        derive_long_td_bonus_counts,
    )
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.repository.models import Player, PlayerStatsRaw
    from ff_pipeline.scoring.rescore import rescore_seasons
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

    years = list(range(args.start, args.end + 1))
    total_rows_merged = 0
    touched_years: list[int] = []

    with Session(engine) as session:
        gsis_to_pid = {
            gsis: pid
            for pid, gsis in session.execute(
                select(Player.player_id, Player.gsis_id).where(Player.gsis_id.is_not(None))
            ).all()
        }

        for year in years:
            counts = derive_long_td_bonus_counts(source.load_pbp([year]).iter_rows(named=True))
            if not counts:
                print(f"  {year}: no long-TD plays derived — skipped")
                continue

            # Index this season's offensive raw rows by (player_id, week).
            raw_by_key: dict[tuple[int, int], PlayerStatsRaw] = {}
            for raw in (
                session.execute(
                    select(PlayerStatsRaw).where(
                        PlayerStatsRaw.season_year == year,
                        PlayerStatsRaw.source == "nflverse",
                    )
                )
                .scalars()
                .all()
            ):
                raw_by_key[(raw.player_id, raw.week)] = raw

            merged = 0
            unmatched_gsis = 0
            for key, key_counts in counts.items():
                pid = gsis_to_pid.get(key.gsis_id)
                if pid is None:
                    unmatched_gsis += 1
                    continue
                raw = raw_by_key.get((pid, key.week))
                if raw is None or not isinstance(raw.stats, dict):
                    continue
                # Game facts only: TD-distance counts (zeros for the keys this row
                # lacks, then the real counts). No bonus points touch raw.
                new_stats = {**raw.stats, **LONG_TD_BONUS_ZEROES, **key_counts}
                if new_stats != raw.stats:
                    if not args.dry_run:
                        raw.stats = new_stats
                    merged += 1
            total_rows_merged += merged
            touched_years.append(year)
            print(f"  {year}: merged {merged} raw rows (unmatched gsis: {unmatched_gsis})")

        if not args.dry_run:
            session.commit()

        if not args.dry_run and not args.no_rescore and touched_years:
            print(f"Re-scoring {touched_years[0]}-{touched_years[-1]} ...")
            result = rescore_seasons(session, season_years=touched_years)
            session.commit()
            print(
                f"  rescored rows: {result.rows_scored}, "
                f"updated: {result.rows_updated}, unchanged: {result.rows_unchanged}"
            )

    verb = "would merge" if args.dry_run else "merged"
    print(f"\nDone — {verb} {total_rows_merged} raw rows across {len(touched_years)} seasons.")


if __name__ == "__main__":
    main()
