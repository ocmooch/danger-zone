#!/usr/bin/env python3
"""Backfill nflverse ``fumbles_lost`` from play-by-play and re-score.

The scoring engine and the per-season ``scoring_rules`` already carry the
``fumbles_lost`` penalty (-2, ``misc`` category), but nflverse's *weekly* player
stats record ``fumbles_lost = 0`` for many players who actually lost a fumble, so
the penalty never applies and ``total_points`` runs **2 points high** per missing
fumble. This is the dominant offensive over-count vs the authoritative
``team_rosters.extra_data.nfl_com_points`` (the "negative-delta" class in
``dz-dashboard/docs/handoffs/bonus-scoring-rescore.md``): ~97 of ~120 offensive
over-counts are exactly -2.00 = one un-penalised fumble.

Play-by-play *does* carry the lost fumble (``fumble_lost`` + ``fumbled_1_player_id``).
This derives the per-(player, week) lost-fumble count from PBP and merges it into
the offensive raw rows, then re-scores — applying the -2 the league actually
charged. Game facts only (a fumble count); no points are written onto raw, and
the merge only ever **raises** ``fumbles_lost`` toward the PBP count (it never
removes a fumble nflverse already recorded), so a correct row is never broken.

Idempotent. The residual after this fix is a small tail PBP can't attribute
(e.g. some 2010 sack-fumbles) plus a genuine bad-data week (2011 wk13).

Usage:
    uv run python scripts/backfill_fumbles_lost.py --dry-run
    uv run python scripts/backfill_fumbles_lost.py --start 2010 --end 2024
    uv run python scripts/backfill_fumbles_lost.py --db sqlite:////tmp/copy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill fumbles_lost from PBP and re-score.")
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

    import polars as pl
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.client import LiveNflverseSource
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
            pbp = source.load_pbp([year])
            lost = (
                pbp.filter(pl.col("fumble_lost") == 1)
                .group_by(["fumbled_1_player_id", "week"])
                .len()
            )
            counts: dict[tuple[str, int], int] = {
                (r["fumbled_1_player_id"], int(r["week"])): int(r["len"])
                for r in lost.iter_rows(named=True)
                if r["fumbled_1_player_id"]
            }
            if not counts:
                print(f"  {year}: no lost fumbles derived — skipped")
                continue

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
            for (gsis, week), count in counts.items():
                pid = gsis_to_pid.get(gsis)
                if pid is None:
                    continue
                raw = raw_by_key.get((pid, week))
                if raw is None or not isinstance(raw.stats, dict):
                    continue
                current = raw.stats.get("fumbles_lost", 0.0)
                if not isinstance(current, (int, float)) or isinstance(current, bool):
                    current = 0.0
                # Only ever raise toward the PBP count — never remove a fumble
                # nflverse already recorded, so a correct row can't be broken.
                if count > current:
                    if not args.dry_run:
                        raw.stats = {**raw.stats, "fumbles_lost": float(count)}
                    merged += 1
            total_rows_merged += merged
            touched_years.append(year)
            print(f"  {year}: raised fumbles_lost on {merged} raw rows")

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

    verb = "would raise" if args.dry_run else "raised"
    print(f"\nDone — {verb} fumbles_lost on {total_rows_merged} raw rows across "
          f"{len(touched_years)} seasons.")


if __name__ == "__main__":
    main()
