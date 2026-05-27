"""Paginated scrape of NFL.com's league-wide players page.

Drives the ``player_availability`` table. Walks ``offset=0, 25, 50, ...``
until either a page tells us no next offset, or we hit ``MAX_PAGES`` as
a safety net.

The runner (``league.run_nfl_com``) is responsible for taking these
``ParsedAvailability`` rows + the current player table, resolving each
NFL.com player_id to an internal ``players.player_id`` (creating a stub
if necessary), and writing the ``player_availability`` rows.

This module is fixture-testable: a stub client whose ``get_html(url)``
returns a sequence of canned HTML pages exercises the pagination loop
without any network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ff_pipeline.crawlers.nfl_com.parsers import (
    ParsedAvailability,
    ParsedAvailabilityPage,
    parse_availability_page,
)
from ff_pipeline.crawlers.nfl_com.urls import league_players
from ff_pipeline.logging_config import get_logger

log = get_logger(__name__)


# Safety net: this many distinct pages even if NFL.com keeps giving us
# next-offsets. Real leagues have ~3000 NFL players → ~120 pages of 25.
MAX_PAGES = 250
PAGE_SIZE = 25


class _HtmlFetcher(Protocol):
    """Subset of ``NflComClient`` we need — keeps tests injectable."""

    def get_html(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class AvailabilitySweepResult:
    """Aggregate output of ``sweep_availability``."""

    rows: tuple[ParsedAvailability, ...]
    pages_fetched: int
    expected_total: int | None


def sweep_availability(
    fetcher: _HtmlFetcher,
    *,
    league_id: str,
    year: int,
    week: int,
    max_pages: int = MAX_PAGES,
) -> AvailabilitySweepResult:
    """Walk every offset page and return de-duplicated availability rows.

    De-dup uses the NFL.com player_id; if the same player_id appears on
    multiple pages (boundary off-by-one as NFL.com renders), the first
    occurrence wins.
    """
    seen_player_ids: set[str] = set()
    accumulated: list[ParsedAvailability] = []
    offset = 0
    expected_total: int | None = None

    for page_idx in range(max_pages):
        url = league_players(league_id, year, week, offset=offset)
        html = fetcher.get_html(url)
        page = parse_availability_page(html)
        if expected_total is None:
            expected_total = page.total_count

        accumulated.extend(_dedupe_rows(page, seen_player_ids))

        next_offset = _next_offset(page, current_offset=offset, page_count=page_idx + 1)
        if next_offset is None:
            log.info(
                "availability sweep complete",
                league_id=league_id,
                year=year,
                week=week,
                pages=page_idx + 1,
                rows=len(accumulated),
                expected_total=expected_total,
            )
            return AvailabilitySweepResult(
                rows=tuple(accumulated),
                pages_fetched=page_idx + 1,
                expected_total=expected_total,
            )
        offset = next_offset

    log.warning(
        "availability sweep hit MAX_PAGES",
        league_id=league_id,
        year=year,
        week=week,
        max_pages=max_pages,
    )
    return AvailabilitySweepResult(
        rows=tuple(accumulated),
        pages_fetched=max_pages,
        expected_total=expected_total,
    )


def _dedupe_rows(page: ParsedAvailabilityPage, seen: set[str]) -> list[ParsedAvailability]:
    """Return only rows whose player_id we haven't already accumulated."""
    new_rows: list[ParsedAvailability] = []
    for row in page.rows:
        if row.player_id in seen:
            continue
        seen.add(row.player_id)
        new_rows.append(row)
    return new_rows


def _next_offset(
    page: ParsedAvailabilityPage, *, current_offset: int, page_count: int
) -> int | None:
    """Decide the next offset to fetch, or None to stop.

    Three stop conditions:

    1. Parser found a "next" link → use its offset
    2. Parser found no next link, but ``total_count`` says more pages
       remain → advance manually (page-size step)
    3. Otherwise stop
    """
    if page.next_offset is not None and page.next_offset > current_offset:
        return page.next_offset
    if page.total_count is not None and current_offset + len(page.rows) < page.total_count:
        return current_offset + PAGE_SIZE
    _ = page_count
    return None


__all__ = [
    "MAX_PAGES",
    "PAGE_SIZE",
    "AvailabilitySweepResult",
    "sweep_availability",
]
