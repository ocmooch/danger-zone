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


def test_sweep_walks_two_pages_and_returns_unique_rows() -> None:
    fetcher = _StubFetcher(
        {
            "offset=0": _load("availability_page_0.html"),
            "offset=25": _load("availability_page_25.html"),
        }
    )
    result = sweep_availability(fetcher, league_id="36271", year=2025, week=7)
    assert result.pages_fetched == 2
    assert result.expected_total == 5
    player_ids = [r.player_id for r in result.rows]
    assert player_ids == ["100", "777", "666", "200", "555"]


def test_sweep_dedupes_when_same_player_on_both_pages() -> None:
    # Boundary case: NFL.com sometimes overlaps pages by one row.
    duplicate = _load("availability_page_0.html")
    fetcher = _StubFetcher(
        {
            "offset=0": duplicate,
            "offset=25": duplicate,  # will dedupe by player_id; same 3 rows
        }
    )
    result = sweep_availability(fetcher, league_id="36271", year=2025, week=7)
    # Sweep stops once the "next" link points to an offset we already saw.
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
