"""Unit tests for the paginated availability sweep."""

from __future__ import annotations

from pathlib import Path

from ff_pipeline.crawlers.nfl_com.availability import sweep_availability

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "nfl_com_html"


class _StubFetcher:
    """Returns pre-canned HTML per URL substring."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def get_html(self, url: str) -> str:
        self.calls.append(url)
        for needle, html in self._mapping.items():
            if needle in url:
                return html
        raise AssertionError(f"unexpected URL: {url}")


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _mini_availability_page(
    rows: list[tuple[str, str]],
    *,
    total: int,
    next_offset: int | None,
) -> str:
    """Build a minimal availability-page HTML that the real parser accepts.

    Matches the live ``tableType-player`` / ``.playerNameAndInfo`` /
    ``.playerOwner`` markup so the parser exercises the same code path
    as production, without the 875-row payload of the real fixture.
    """
    body_rows = "".join(
        f'<tr><td class="playerNameAndInfo">'
        f'<a class="playerName" href="/players/card?playerId={pid}">{name}</a>'
        f"<em>WR - DET</em></td>"
        f'<td class="playerOwner">FA</td></tr>'
        for pid, name in rows
    )
    next_html = (
        f'<div class="pagination"><span class="next">'
        f'<a href="?offset={next_offset}">Next</a></span></div>'
        if next_offset is not None
        else ""
    )
    return (
        f'<html><body><span class="paginationTitle">1 - {len(rows)} of {total}</span>'
        f'<table class="tableType-player"><tbody>{body_rows}</tbody></table>'
        f"{next_html}</body></html>"
    )


def test_sweep_walks_two_pages_and_returns_unique_rows() -> None:
    fetcher = _StubFetcher(
        {
            "offset=0": _mini_availability_page(
                [("100", "Alpha"), ("200", "Bravo"), ("300", "Charlie")],
                total=5,
                next_offset=3,
            ),
            "offset=3": _mini_availability_page(
                [("400", "Delta"), ("500", "Echo")],
                total=5,
                next_offset=None,
            ),
        }
    )
    result = sweep_availability(fetcher, league_id="36271", year=2025, week=7)
    assert result.pages_fetched == 2
    assert result.expected_total == 5
    player_ids = [r.player_id for r in result.rows]
    assert player_ids == ["100", "200", "300", "400", "500"]


def test_sweep_dedupes_when_same_player_on_both_pages() -> None:
    # Boundary case: NFL.com sometimes overlaps pages by one row.
    same_rows = [("100", "Alpha"), ("200", "Bravo"), ("300", "Charlie")]
    page_one = _mini_availability_page(same_rows, total=3, next_offset=3)
    page_two = _mini_availability_page(same_rows, total=3, next_offset=None)
    fetcher = _StubFetcher({"offset=0": page_one, "offset=3": page_two})
    result = sweep_availability(fetcher, league_id="36271", year=2025, week=7)
    # All three player_ids were on both pages -> deduped down to 3.
    assert len({r.player_id for r in result.rows}) == 3


def test_sweep_respects_max_pages_safety_net() -> None:
    # Synthetic infinite loop: every page points to a "next" higher offset.
    html_template = (
        '<html><body><span class="paginationSummary">1-1 of 9999</span>'
        '<table class="tableType-playerStats"><tbody>'
        '<tr><td><a class="playerName" href="/player/{pid}">P {pid}</a></td>'
        '<td><span class="playerStatus">FA</span></td></tr></tbody></table>'
        '<div class="pagination"><span class="next">'
        '<a href="?offset={next_off}">Next</a></span></div></body></html>'
    )

    class _InfFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def get_html(self, url: str) -> str:
            self.calls += 1
            # Parse current offset out of the URL.
            offset = 0
            if "offset=" in url:
                offset = int(url.rsplit("offset=", 1)[1])
            return html_template.format(pid=self.calls, next_off=offset + 25)

    fetcher = _InfFetcher()
    result = sweep_availability(fetcher, league_id="36271", year=2025, week=7, max_pages=4)
    assert result.pages_fetched == 4
    assert fetcher.calls == 4
