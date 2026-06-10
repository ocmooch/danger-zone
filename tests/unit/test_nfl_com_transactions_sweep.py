"""Unit tests for the paginated transactions sweep."""

from __future__ import annotations

from ff_pipeline.crawlers.nfl_com.transactions import sweep_transactions


class _StubFetcher:
    """Returns pre-canned HTML per URL substring (offset marker)."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def get_html(self, url: str) -> str:
        self.calls.append(url)
        # The first page is param-less (no offset=) — match it explicitly.
        if "offset=" not in url and "__page0__" in self._mapping:
            return self._mapping["__page0__"]
        for needle, html in self._mapping.items():
            if needle in url:
                return html
        raise AssertionError(f"unexpected URL: {url}")


def _row(
    txn_id: str,
    txn_type: str,
    player_id: str,
    player_name: str,
    *,
    from_cell: str,
    to_cell: str,
    date: str = "Dec 28, 10:01am",
    week: int = 17,
) -> str:
    # The row CSS class uses NFL.com's bucket name ("roster" for lineup
    # moves) while the Type cell renders the human label.
    type_label = {"roster": "Lineup", "add": "Add", "drop": "Drop", "trade": "Trade"}[txn_type]
    return (
        f'<tr class="transaction-{txn_type}-{txn_id} odd">'
        f'<td class="transactionDate first">{date}</td>'
        f'<td class="transactionWeek">{week}</td>'
        f'<td class="transactionType">{type_label}</td>'
        f'<td class="playerNameAndInfo">'
        f'<a class="playerName" href="/players/card?playerId={player_id}">{player_name}</a>'
        f"<em>RB - KC</em></td>"
        f'<td class="transactionFrom">{from_cell}</td>'
        f'<td class="transactionTo">{to_cell}</td>'
        f'<td class="transactionOwner last"><span class="userName">owner</span></td>'
        f"</tr>"
    )


def _page(rows_html: str, *, next_offset: int | None) -> str:
    next_html = (
        f'<div class="pagination"><ul><li class="next"><a href="?offset={next_offset}">&gt;</a>'
        f"</li></ul></div>"
        if next_offset is not None
        else '<div class="pagination"><ul><li class="first"><span>1</span></li></ul></div>'
    )
    return (
        f'<html><body><table class="tableType-transaction"><tbody>{rows_html}</tbody></table>'
        f"{next_html}</body></html>"
    )


def _team(team_id: int) -> str:
    return f'<a href="/league/36271/history/2025/teamhome?teamId={team_id}" class="teamName">T{team_id}</a>'


def test_sweep_walks_two_pages_and_concats_rows() -> None:
    page0 = _page(
        _row("10", "add", "100", "Alpha", from_cell="Free Agents", to_cell=_team(1)),
        next_offset=21,
    )
    page1 = _page(
        _row("11", "drop", "200", "Bravo", from_cell=_team(2), to_cell="Waivers"),
        next_offset=None,
    )
    fetcher = _StubFetcher({"__page0__": page0, "offset=21": page1})
    result = sweep_transactions(fetcher, league_id="36271", year=2025)
    assert result.pages_fetched == 2
    names = [r.player_name for r in result.rows]
    assert names == ["Alpha", "Bravo"]
    assert result.rows[0].transaction_type == "free_agent_add"
    assert result.rows[1].transaction_type == "drop"


def test_sweep_dedupes_overlapping_boundary_row() -> None:
    shared = _row("10", "add", "100", "Alpha", from_cell="Free Agents", to_cell=_team(1))
    page0 = _page(shared, next_offset=21)
    page1 = _page(
        shared + _row("11", "drop", "200", "Bravo", from_cell=_team(2), to_cell="Waivers"),
        next_offset=None,
    )
    fetcher = _StubFetcher({"__page0__": page0, "offset=21": page1})
    result = sweep_transactions(fetcher, league_id="36271", year=2025)
    # Alpha appears on both pages but must land once.
    assert [r.player_name for r in result.rows] == ["Alpha", "Bravo"]


def test_sweep_stitches_trade_counterparts_across_pages() -> None:
    # One two-player trade (shared txn id 50): Charlie team3->team7 on page 0,
    # Echo team7->team3 on page 1. Each row emits an out + an in leg; the
    # stitch must pair counterparts even though the rows span a page break.
    page0 = _page(
        _row("50", "trade", "300", "Charlie", from_cell=_team(3), to_cell=_team(7)),
        next_offset=21,
    )
    page1 = _page(
        _row("50", "trade", "301", "Echo", from_cell=_team(7), to_cell=_team(3)),
        next_offset=None,
    )
    fetcher = _StubFetcher({"__page0__": page0, "offset=21": page1})
    result = sweep_transactions(fetcher, league_id="36271", year=2025)
    trades = [r for r in result.rows if r.transaction_type == "trade"]
    assert len(trades) == 4
    # Every leg's counterpart is the team on the other side of the trade.
    for leg in trades:
        assert leg.counterpart_team_id is not None
        assert leg.counterpart_team_id != leg.team_id
        assert {leg.team_id, leg.counterpart_team_id} == {3, 7}


def test_sweep_captures_lineup_change_payload() -> None:
    page0 = _page(
        _row("60", "roster", "400", "Delta", from_cell="RB", to_cell="BN"),
        next_offset=None,
    )
    fetcher = _StubFetcher({"__page0__": page0})
    result = sweep_transactions(fetcher, league_id="36271", year=2025)
    (row,) = result.rows
    assert row.transaction_type == "lineup_change"
    assert row.direction == "out"
    assert row.extra_data == {"from_slot": "RB", "to_slot": "BN"}


def test_sweep_respects_max_pages() -> None:
    class _InfFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def get_html(self, url: str) -> str:
            self.calls += 1
            offset = int(url.rsplit("offset=", 1)[1]) if "offset=" in url else 0
            return _page(
                _row(
                    str(self.calls),
                    "add",
                    str(self.calls),
                    "P",
                    from_cell="Free Agents",
                    to_cell=_team(1),
                ),
                next_offset=offset + 21,
            )

    fetcher = _InfFetcher()
    result = sweep_transactions(fetcher, league_id="36271", year=2025, max_pages=4)
    assert result.pages_fetched == 4
    assert fetcher.calls == 4
