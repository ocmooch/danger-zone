#!/usr/bin/env python3
"""Backfill ``transactions.extra_data.faab_bid`` from NFL.com waiver claims.

The league adopted FAAB (Free Agent Acquisition Budget) in 2021. NFL.com's
history transactions page renders the winning bid in the ``To`` cell as a
trailing parenthetical after the team anchor — ``<a class="teamName">Team</a>
(51 pts)`` — on ``From=Waivers`` legs only (free-agent adds are free, so they
carry none). The unit is mislabelled "pts" but is waiver-budget dollars (a $0
bid is an unopposed claim). The parser now captures it into
``extra_data["faab_bid"]``; this script re-runs the transactions sweep + ingest
for the FAAB-era seasons so claims first ingested before the parser existed gain
their bid.

It reuses the production sweep + upsert path (``sweep_transactions`` +
``_upsert_transactions``). That upsert is append-only with an in-place
``faab_bid`` enrich on fingerprint match, so this is idempotent: existing rows
are enriched (not duplicated), and re-running once bids are stored is a no-op.
Pre-2021 seasons have no bids and stay NULL.

Usage:
    uv run python scripts/backfill_faab_bids.py --dry-run
    uv run python scripts/backfill_faab_bids.py
    uv run python scripts/backfill_faab_bids.py --start 2021 --end 2025
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the src layout importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _faab_bid_count(session: object, season_id: int) -> int:
    """Number of stored transactions in the season carrying a faab_bid."""
    from sqlalchemy import func, select

    from ff_pipeline.repository.models import Transaction

    return int(
        session.execute(  # type: ignore[attr-defined]
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.season_id == season_id,
                func.json_extract(Transaction.extra_data, "$.faab_bid").is_not(None),
            )
        ).scalar_one()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill FAAB winning bids from NFL.com.")
    parser.add_argument("--start", type=int, default=2021, help="First season year (default 2021)")
    parser.add_argument(
        "--end", type=int, default=2025, help="Last season year inclusive (default 2025)"
    )
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl + parse and report bids found, but write nothing.",
    )
    args = parser.parse_args()

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.nfl_com.client import AuthFailureError
    from ff_pipeline.crawlers.nfl_com.league import (
        _team_id_lookup,
        _upsert_transactions,
        build_default_client,
    )
    from ff_pipeline.crawlers.nfl_com.transactions import sweep_transactions
    from ff_pipeline.logging_config import configure_logging
    from ff_pipeline.normalizer.player_ids import PlayerResolver
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.repository.models import Season
    from ff_pipeline.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db_url = args.db or settings.database_url
    league_id = settings.nfl_league_id
    print(f"Database: {db_url}")
    print(f"League:   {league_id}  ·  seasons {args.start}-{args.end}")
    if args.dry_run:
        print("DRY RUN — crawls NFL.com but writes nothing.")

    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)
    cookie_value = settings.nfl_cookie.get_secret_value()

    total_parsed_bids = 0
    try:
        with (
            build_default_client(cookie_value, settings.nfl_com_delay_seconds) as client,
            Session(engine) as session,
        ):
            seasons = session.execute(
                select(Season.season_id, Season.year)
                .where(Season.year >= args.start, Season.year <= args.end)
                .order_by(Season.year)
            ).all()

            for season_id, year in seasons:
                before = _faab_bid_count(session, season_id)
                sweep = sweep_transactions(client, league_id=league_id, year=year)
                parsed_bids = sum(
                    1
                    for t in sweep.rows
                    if t.extra_data is not None and "faab_bid" in t.extra_data
                )
                total_parsed_bids += parsed_bids

                if args.dry_run:
                    print(
                        f"  {year}: parsed {parsed_bids} waiver bids "
                        f"({len(sweep.rows)} legs); DB currently has {before}"
                    )
                    continue

                _upsert_transactions(
                    session,
                    season_id=season_id,
                    season_year=year,
                    parsed=sweep.rows,
                    team_id_by_nfl_team_id=_team_id_lookup(session, season_id),
                    warnings=[],
                    resolver=PlayerResolver(session),
                )
                session.flush()
                after = _faab_bid_count(session, season_id)
                print(
                    f"  {year}: parsed {parsed_bids} waiver bids; "
                    f"DB faab_bid {before} -> {after} (+{after - before})"
                )

            if not args.dry_run:
                session.commit()
    except AuthFailureError as exc:
        print(f"\nAUTH FAILURE: {exc}", file=sys.stderr)
        print("Refresh the cookie via `ff-pipeline cookie set` and re-run.", file=sys.stderr)
        raise SystemExit(77) from exc

    verb = "would capture" if args.dry_run else "captured"
    print(f"\nDone — {verb} {total_parsed_bids} waiver bids across {args.start}-{args.end}.")


if __name__ == "__main__":
    main()
