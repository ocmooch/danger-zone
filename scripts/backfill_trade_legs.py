"""One-off backfill: recover trade legs dropped by the first-anchor-only bug.

NFL.com renders one transactions row per *side* of a trade, and a side's
player cell can list several players (2-for-2, 2-for-3, …). The parser used
to read only the first anchor, so every additional player on a side was
silently dropped from the ``transactions`` table (the roster snapshots, being
independent scrapes, were unaffected and already correct).

The parser is now fixed; this script re-parses each season's trade-filtered
transactions page and runs the rows through the normal idempotent upsert. The
upsert fingerprints on ``(type, team, player, direction, executed_at)``, so
already-stored legs are skipped and only the previously-dropped legs insert.

Dry-run by default (rolls back). Set ``COMMIT=1`` to persist.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nfl_com.client import (
    AuthFailureError,
    NflComClient,
    NflComClientError,
)
from ff_pipeline.crawlers.nfl_com.league import (
    _team_id_lookup,
    _upsert_transactions,
)
from ff_pipeline.crawlers.nfl_com.parsers import ParseError, parse_transactions_page
from ff_pipeline.crawlers.nfl_com.transactions import (
    _leg_key,
    _stitch_trade_counterparts,
)
from ff_pipeline.crawlers.nfl_com.urls import BASE_URL
from ff_pipeline.normalizer.player_ids import PlayerResolver
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.models import Season
from ff_pipeline.settings import Settings


def _trade_url(league_id: str, year: int, offset: int) -> str:
    base = f"{BASE_URL}/league/{league_id}/history/{year}/transactions?transactionType=trade"
    return f"{base}&offset={offset}" if offset else base


def _sweep_trades(client: NflComClient, league_id: str, year: int) -> list:
    """Walk the trade-filtered pages, de-dup legs, stitch counterparts."""
    seen: set = set()
    rows: list = []
    offset = 0
    for _ in range(50):  # trades are few; generous safety net
        html = client.get_html(_trade_url(league_id, year, offset))
        try:
            page = parse_transactions_page(html)
        except ParseError:
            # A season with no trades renders an empty-state with no table.
            break
        for r in page.rows:
            key = _leg_key(r)
            if key not in seen:
                seen.add(key)
                rows.append(r)
        if page.next_offset is None or page.next_offset <= offset:
            break
        offset = page.next_offset
    return list(_stitch_trade_counterparts(rows))


def main() -> int:
    commit = os.environ.get("COMMIT") == "1"
    settings = Settings()
    league_id = settings.nfl_league_id
    engine = create_app_engine(settings.database_url)

    total_inserted = 0
    with (
        NflComClient(cookie=settings.nfl_cookie.get_secret_value()) as client,
        Session(engine) as session,
    ):
        years = session.execute(select(Season.year, Season.season_id).order_by(Season.year)).all()
        for year, season_id in years:
            try:
                parsed = _sweep_trades(client, league_id, year)
            except AuthFailureError:
                raise
            except NflComClientError as exc:
                # A future/unplayed season has no history page (302 → /).
                print(f"{year}: no history page, skipping ({exc})")
                continue
            trade_legs = [p for p in parsed if p.transaction_type == "trade"]
            if not trade_legs:
                continue
            team_map = _team_id_lookup(session, season_id)
            resolver = PlayerResolver(session)
            counts = _upsert_transactions(
                session,
                season_id=season_id,
                season_year=year,
                parsed=trade_legs,
                team_id_by_nfl_team_id=team_map,
                warnings=[],
                resolver=resolver,
            )
            # _upsert_transactions returns _Counts(inserted, skipped); the
            # second field is named rows_updated on the dataclass.
            total_inserted += counts.rows_added
            print(
                f"{year}: parsed {len(trade_legs)} trade legs -> "
                f"inserted {counts.rows_added}, skipped {counts.rows_updated}"
            )
        if commit:
            session.commit()
            print(f"\nCOMMITTED. total trade legs inserted: {total_inserted}")
        else:
            session.rollback()
            print(f"\nDRY-RUN (no COMMIT). would insert: {total_inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
