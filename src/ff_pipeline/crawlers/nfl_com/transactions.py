"""Paginated scrape of NFL.com's per-season transactions log.

Drives the ``transactions`` table. The history transactions page is
paginated by NFL.com's shared ``?offset=`` widget, and only page 1 was
ever fetched — so the historical log was missing nearly every in-season
add/drop and every trade. This sweep walks each page's "next" offset
until the page stops offering one (or ``MAX_PAGES`` trips the safety net).

Mirrors ``availability.sweep_availability``: a stub client whose
``get_html(url)`` returns canned pages exercises the loop without network.
The runner (``league.run_nfl_com``) resolves the parsed NFL.com ids to
internal rows and upserts; this module only fetches + de-duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from ff_pipeline.crawlers.nfl_com.parsers import (
    ParsedTransaction,
    ParsedTransactionsPage,
    parse_transactions_page,
)
from ff_pipeline.crawlers.nfl_com.urls import transactions
from ff_pipeline.logging_config import get_logger

log = get_logger(__name__)


# Safety net even if NFL.com keeps handing us a next-offset. A busy
# 12-team season logs a few hundred moves → low tens of pages at most.
MAX_PAGES = 250


class _HtmlFetcher(Protocol):
    """Subset of ``NflComClient`` we need — keeps tests injectable."""

    def get_html(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class TransactionsSweepResult:
    """Aggregate output of ``sweep_transactions``."""

    rows: tuple[ParsedTransaction, ...]
    pages_fetched: int


def sweep_transactions(
    fetcher: _HtmlFetcher,
    *,
    league_id: str,
    year: int,
    max_pages: int = MAX_PAGES,
) -> TransactionsSweepResult:
    """Walk every offset page and return de-duplicated transaction legs.

    De-dup spans page boundaries (NFL.com sometimes overlaps a row): a
    leg is identified by ``(nfl_transaction_id, type, player, direction,
    executed_at)`` — the txn id distinguishes otherwise-identical lineup
    moves, while type/player/direction keep the two legs of a single trade
    (same txn id) as separate rows. Trade legs are then stitched so each
    carries its ``counterpart_team_id``.
    """
    seen: set[tuple[object, ...]] = set()
    accumulated: list[ParsedTransaction] = []
    offset = 0

    for page_idx in range(max_pages):
        url = transactions(league_id, year, offset=offset)
        page = parse_transactions_page(fetcher.get_html(url))
        accumulated.extend(_dedupe_rows(page, seen))

        next_offset = _next_offset(page, current_offset=offset)
        if next_offset is None:
            log.info(
                "transactions sweep complete",
                league_id=league_id,
                year=year,
                pages=page_idx + 1,
                rows=len(accumulated),
            )
            return TransactionsSweepResult(
                rows=_stitch_trade_counterparts(accumulated),
                pages_fetched=page_idx + 1,
            )
        offset = next_offset

    log.warning(
        "transactions sweep hit MAX_PAGES",
        league_id=league_id,
        year=year,
        max_pages=max_pages,
    )
    return TransactionsSweepResult(
        rows=_stitch_trade_counterparts(accumulated),
        pages_fetched=max_pages,
    )


def _leg_key(t: ParsedTransaction) -> tuple[object, ...]:
    return (t.nfl_transaction_id, t.transaction_type, t.player_id, t.direction, t.executed_at)


def _dedupe_rows(
    page: ParsedTransactionsPage, seen: set[tuple[object, ...]]
) -> list[ParsedTransaction]:
    """Return only legs we haven't already accumulated on an earlier page."""
    new_rows: list[ParsedTransaction] = []
    for row in page.rows:
        key = _leg_key(row)
        if key in seen:
            continue
        seen.add(key)
        new_rows.append(row)
    return new_rows


def _next_offset(page: ParsedTransactionsPage, *, current_offset: int) -> int | None:
    """Next offset to fetch, or None to stop. Only trust a forward link."""
    if page.next_offset is not None and page.next_offset > current_offset:
        return page.next_offset
    return None


def _stitch_trade_counterparts(rows: list[ParsedTransaction]) -> tuple[ParsedTransaction, ...]:
    """Fill ``counterpart_team_id`` for trade legs sharing a txn id.

    A trade renders one row per player-per-direction under a single
    NFL.com txn id. For each leg we set the counterpart to the team on the
    *opposite* side of that same trade (the lone other distinct team_id),
    so a 2-for-1 still resolves both sides. Non-trade rows pass through.
    """
    by_txn: dict[str, list[int]] = {}
    for t in rows:
        if t.transaction_type == "trade" and t.nfl_transaction_id and t.team_id is not None:
            by_txn.setdefault(t.nfl_transaction_id, []).append(t.team_id)

    stitched: list[ParsedTransaction] = []
    for t in rows:
        if t.transaction_type != "trade" or not t.nfl_transaction_id or t.team_id is None:
            stitched.append(t)
            continue
        others = {tid for tid in by_txn.get(t.nfl_transaction_id, []) if tid != t.team_id}
        counterpart = next(iter(others)) if len(others) == 1 else None
        stitched.append(replace(t, counterpart_team_id=counterpart))
    return tuple(stitched)


__all__ = [
    "MAX_PAGES",
    "TransactionsSweepResult",
    "sweep_transactions",
]
