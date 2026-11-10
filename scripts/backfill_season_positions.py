#!/usr/bin/env python3
"""Populate ``player_season_positions`` from nflverse weekly stats.

``players.position`` is a single current/last-known snapshot, so it misrepresents
any season before a position change (a 2014 WR shown as a later-career TE) or any
plain mislabel. ``player_season_positions`` is the season-aware counterpart: one
row per (player, season), derived from the position a player appears at in the
most weeks of nflverse's weekly ``player_stats`` that season (ties → latest week).

This script backfills the table for existing players without re-running the whole
nflverse ingest. The normal ``run --source nflverse`` path keeps it fresh going
forward (``runner._upsert_season_positions``); this is the one-time catch-up.

Idempotent. Run against a copy first and confirm the canaries before live:
    cp data/fantasy.db /tmp/pos_copy.db
    uv run python scripts/backfill_season_positions.py --db sqlite:////tmp/pos_copy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill season-correct player positions from nflverse weekly stats."
    )
    parser.add_argument("--start", type=int, default=2010, help="First season year (default 2010)")
    parser.add_argument("--end", type=int, default=2025, help="Last season year (default 2025)")
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    args = parser.parse_args()

    from dataclasses import dataclass

    import nflreadpy as nfl
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nflverse.runner import _upsert_season_positions
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.repository.models import Player
    from ff_pipeline.settings import get_settings

    @dataclass(frozen=True, slots=True)
    class _PosRow:
        """Minimal duck type for ``_upsert_season_positions`` (4 fields it reads)."""

        gsis_id: str
        season_year: int
        week: int
        position: str | None

    settings = get_settings()
    configure_logging(settings)
    db_url = args.db or settings.database_url
    print(f"Database: {db_url}")

    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)

    years = list(range(args.start, args.end + 1))
    # Read positions from nflverse *rosters*, the season-aware source: a player's
    # weekly ``player_stats`` position carries the same current/canonical value as
    # ``players.position`` (so it can't tell a 2014 WR from a later-career TE),
    # whereas the roster frame records the position the player was actually listed
    # at that season.
    print(f"Loading nflverse rosters for {years[0]}-{years[-1]} ...")
    frame = nfl.load_rosters(years).select(["gsis_id", "season", "week", "position"])
    stats = [
        _PosRow(
            gsis_id=str(r["gsis_id"]),
            season_year=int(r["season"]),
            week=int(r["week"]) if r["week"] is not None else 0,
            position=r["position"],
        )
        for r in frame.iter_rows(named=True)
        if r["gsis_id"] is not None
    ]

    with Session(engine) as session:
        # Resolve gsis -> player_id against existing players only; never stub.
        rows = session.execute(
            select(Player.gsis_id, Player.player_id).where(Player.gsis_id.isnot(None))
        ).all()
        gsis_to_player_id = dict(rows)
        counts = _upsert_season_positions(session, stats, gsis_to_player_id)
        session.commit()
        print(f"player_season_positions: added {counts.rows_added}, updated {counts.rows_updated}")

    print("\nDone.")


if __name__ == "__main__":
    main()
