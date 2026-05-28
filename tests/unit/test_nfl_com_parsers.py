"""Unit tests for the NFL.com fantasy HTML parsers.

Every fixture under ``tests/fixtures/nfl_com_html/`` is exercised here.
When NFL.com changes its DOM in the future, the failing parser test
names the missing selector — the fix loop is "update fixture + parser
together, re-run."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ff_pipeline.crawlers.nfl_com.parsers import (
    ParseError,
    parse_availability_page,
    parse_league_home,
    parse_owners,
    parse_team_roster,
    parse_transactions,
    parse_weekly_matchups,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "nfl_com_html"


def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_league_home
# ---------------------------------------------------------------------------


def test_parse_league_home_extracts_name_id_week_year() -> None:
    parsed = parse_league_home(_read("league_home.html"))
    assert parsed.league_id == "36271"
    assert parsed.league_name == "The Danger Zone"
    assert parsed.current_week == 17
    assert parsed.current_season_year == 2025
    # window.analyticsData also exposes the viewer's own user + team id.
    assert parsed.nfl_user_id == "168722"
    assert parsed.current_team_id == 4


def test_parse_league_home_missing_name_raises() -> None:
    # Provide a league_id anchor so the league_id check passes; the name
    # check is what we want to exercise here.
    html = "<html><body><a href='/league/9/team/1'>X</a></body></html>"
    with pytest.raises(ParseError, match="leagueName"):
        parse_league_home(html)


def test_parse_league_home_missing_links_raises() -> None:
    html = "<html><body><h1 class='title'>Lonely</h1></body></html>"
    with pytest.raises(ParseError, match="league_id"):
        parse_league_home(html)


# ---------------------------------------------------------------------------
# parse_owners
# ---------------------------------------------------------------------------


def test_parse_owners_returns_twelve_rows() -> None:
    owners = parse_owners(_read("owners.html"))
    assert len(owners) == 12
    by_team = {o.team_id: o for o in owners}
    # Viewer's own team — cross-checked against analyticsData on the
    # league_home fixture (userID=168722, teamID=4).
    assert by_team[4].team_name == "The Wizard Of BAA'z"
    assert by_team[4].display_name == "sully"
    assert by_team[4].nfl_user_id == "168722"
    # First team in standings rank
    assert by_team[1].team_name == "Cream of the C"
    assert by_team[1].display_name == "harry"
    assert by_team[1].nfl_user_id == "102530"


def test_parse_owners_missing_table_raises() -> None:
    with pytest.raises(ParseError, match="tableType-team"):
        parse_owners("<html><body><h1>No table</h1></body></html>")


# ---------------------------------------------------------------------------
# parse_team_roster
# ---------------------------------------------------------------------------


def test_parse_team_roster_slot_classification() -> None:
    roster = parse_team_roster(_read("team_roster_1.html"))
    # Fixture is team 4 ("The Wizard Of BAA'z") on the live page.
    assert roster.team_id == 4
    assert roster.team_name == "The Wizard Of BAA'z"
    # Starters (QB, RB x2, WR x2, TE, R/W/T, K, DEF) + 4 bench + 1 reserve
    # + 2 bench defenses = 16 entries total.
    assert len(roster.entries) == 16

    # The QB starter should be the season's actual starter on team 4.
    by_pos_and_slot = {(e.roster_slot, e.player_name): e for e in roster.entries}
    qb_entry = next(e for e in roster.entries if e.roster_slot == "QB")
    assert qb_entry.is_starter is True
    assert qb_entry.player_name == "Joe Burrow"
    assert qb_entry.position == "QB"
    assert qb_entry.nfl_team == "CIN"
    assert qb_entry.player_id == "2563722"

    flex = next(e for e in roster.entries if e.roster_slot == "R/W/T")
    assert flex.is_starter is True
    assert flex.player_name == "Jaylen Waddle"

    bench_entries = [e for e in roster.entries if e.roster_slot == "BN"]
    assert len(bench_entries) == 6  # 4 offensive + 2 D/ST bench
    assert all(e.is_starter is False for e in bench_entries)

    res_entries = [e for e in roster.entries if e.roster_slot == "RES"]
    assert len(res_entries) == 1
    assert res_entries[0].is_starter is False
    assert ("RES", "MarShawn Lloyd") in by_pos_and_slot


def test_parse_team_roster_missing_table_raises() -> None:
    html = "<html><body><a class='teamImg teamId-1' href='/league/1/team/1'><img alt='X'/></a></body></html>"
    with pytest.raises(ParseError, match="tableType-player"):
        parse_team_roster(html)


# ---------------------------------------------------------------------------
# parse_weekly_matchups
# ---------------------------------------------------------------------------


def test_parse_weekly_matchups_emits_two_rows_per_game() -> None:
    rows = parse_weekly_matchups(_read("weekly_matchups_w7.html"))
    # 6 matchups x 2 rows each = 12. The fixture is a real Week 17
    # schedule capture covering all 12 teams in the league.
    assert len(rows) == 12

    # Spot-check the first matchup: team 4 (84.60) vs team 10 (159.12).
    by_team = {r.team_id: r for r in rows}
    assert by_team[4].team_name == "The Wizard Of BAA'z"
    assert by_team[4].team_score == pytest.approx(84.60)
    assert by_team[4].opponent_team_id == 10
    assert by_team[4].opponent_score == pytest.approx(159.12)
    # Mirror row
    assert by_team[10].team_score == pytest.approx(159.12)
    assert by_team[10].opponent_team_id == 4

    # The schedule page does not embed gameId hrefs; that field is
    # populated by the gamecenter parser, not this one.
    assert all(r.game_id is None for r in rows)


def test_parse_weekly_matchups_missing_matchups_raises() -> None:
    with pytest.raises(ParseError, match="matchup"):
        parse_weekly_matchups("<html><body></body></html>")


# ---------------------------------------------------------------------------
# parse_transactions
# ---------------------------------------------------------------------------


def test_parse_transactions_maps_types_and_direction() -> None:
    txns = parse_transactions(_read("transactions.html"))
    assert len(txns) == 3
    types = [t.transaction_type for t in txns]
    assert types == ["free_agent_add", "drop", "waiver_add"]
    assert txns[0].team_id == 1
    assert txns[0].player_id == "777"
    assert txns[0].player_name == "Free Agent Joe"
    assert txns[0].direction == "in"
    assert txns[0].effective_week == 2
    assert txns[1].direction == "out"
    assert txns[2].team_id == 2
    assert txns[2].player_id == "999"


def test_parse_transactions_missing_table_raises() -> None:
    with pytest.raises(ParseError, match="rosterTrades"):
        parse_transactions("<html><body></body></html>")


# ---------------------------------------------------------------------------
# parse_availability_page
# ---------------------------------------------------------------------------


def test_parse_availability_page_owned_free_agent_and_waivers() -> None:
    page = parse_availability_page(_read("availability_page_0.html"))
    assert page.total_count == 5
    assert page.next_offset == 25
    assert len(page.rows) == 3
    by_id = {r.player_id: r for r in page.rows}
    assert by_id["100"].status == "OWNED"
    assert by_id["100"].owning_team_id == 1
    assert by_id["777"].status == "FREE_AGENT"
    assert by_id["666"].status == "ON_WAIVERS"
    assert by_id["666"].waiver_claim_deadline == "Wed 4:00 AM"


def test_parse_availability_page_last_page_has_no_next() -> None:
    page = parse_availability_page(_read("availability_page_25.html"))
    assert page.next_offset is None
    assert page.total_count == 5
    assert len(page.rows) == 2
