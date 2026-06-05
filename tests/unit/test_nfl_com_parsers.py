"""Unit tests for the NFL.com fantasy HTML parsers.

Every fixture under ``tests/fixtures/nfl_com_html/`` is exercised here.
When NFL.com changes its DOM in the future, the failing parser test
names the missing selector — the fix loop is "update fixture + parser
together, re-run."
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from ff_pipeline.crawlers.nfl_com.parsers import (
    ParseError,
    _position_and_team_from_row,
    parse_availability_page,
    parse_draft_picks,
    parse_draft_round_numbers,
    parse_gamecenter,
    parse_league_home,
    parse_owners,
    parse_standings,
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
    # Fixture has 4 Drops + 4 Adds + 16 Lineup rows. The full league diary
    # now captures all of them (lineup moves included), not just player moves.
    assert len(txns) == 24
    types = [t.transaction_type for t in txns]
    assert types.count("drop") == 4
    assert types.count("free_agent_add") == 4
    assert types.count("lineup_change") == 16

    # Lineup rows carry their slot move in extra_data and an in/out direction
    # (start vs sit) — but no team anchor, so team_id is None.
    london = next(t for t in txns if t.player_name == "Drake London")
    assert london.transaction_type == "lineup_change"
    assert london.direction == "out"  # R/W/T -> BN is a benching
    assert london.extra_data == {"from_slot": "R/W/T", "to_slot": "BN"}
    assert london.team_id is None

    # First two rows: drop + add for the same NFL.com txn id 2129
    # (the user's add+drop arrives as two adjacent rows sharing an id).
    assert txns[0].nfl_transaction_id == "2129"
    assert txns[0].transaction_type == "drop"
    assert txns[0].team_id == 1  # Cream of the C
    assert txns[0].player_name == "Taysom Hill"
    assert txns[0].player_id == "2558954"
    assert txns[0].direction == "out"
    assert txns[0].effective_week == 17

    assert txns[1].nfl_transaction_id == "2129"
    assert txns[1].transaction_type == "free_agent_add"
    assert txns[1].team_id == 1
    assert txns[1].player_name == "Grant Calcaterra"
    assert txns[1].direction == "in"

    # Date text is left raw for the runner to coerce to datetime —
    # the parser doesn't know the season year.
    assert txns[0].executed_at == "Dec 28, 10:01am"


def test_parse_transactions_maps_lm_commish_row_to_setting_change() -> None:
    # NFL.com history pages tag commissioner / league-management actions with
    # the type text "LM" (row class ``transaction-commish-NNN``). They have no
    # player or team; the change text lives in ``.playerNameAndInfo`` and the
    # actor in ``.transactionOwner``. Real shape captured from the 2011 log.
    html = """
    <table class="tableType-transaction"><tbody>
      <tr class="transaction-commish-1660 odd">
        <td class="transactionDate first">Dec 6, 7:01am</td>
        <td class="transactionWeek"></td>
        <td class="transactionType">LM</td>
        <td class="playerNameAndInfo" colspan="3">harry updated playoff teams</td>
        <td class="transactionOwner"><div class="teamOwner">
          <span class="userName userId-102530">harry</span></div></td>
      </tr>
    </tbody></table>
    """
    (txn,) = parse_transactions(html)
    assert txn.transaction_type == "setting_change"
    assert txn.team_id is None and txn.player_id is None and txn.direction is None
    assert txn.nfl_transaction_id == "1660"
    assert txn.notes == "harry"
    assert txn.extra_data == {"raw_type": "lm", "description": "harry updated playoff teams"}


def test_parse_transactions_missing_table_raises() -> None:
    with pytest.raises(ParseError, match="tableType-transaction"):
        parse_transactions("<html><body></body></html>")


# ---------------------------------------------------------------------------
# parse_availability_page
# ---------------------------------------------------------------------------


def test_parse_availability_page_real_pagination_and_status() -> None:
    page = parse_availability_page(_read("availability_page_0.html"))
    # Real Week 17 capture: 25 rows per page, 875 total available
    # (offensive) players in the league.
    assert page.total_count == 875
    assert page.next_offset == 26  # NFL.com's "Next" link is 1-indexed
    assert len(page.rows) == 25
    # All 25 page-0 rows are FA (championship-week capture; the live
    # league has dropped almost everyone by week 17), so we exercise
    # the FREE_AGENT branch only here.
    assert all(r.status == "FREE_AGENT" for r in page.rows)

    mahomes = next(r for r in page.rows if r.player_name == "Patrick Mahomes")
    assert mahomes.player_id == "2558125"
    assert mahomes.position == "QB"
    assert mahomes.nfl_team == "KC"
    assert mahomes.owning_team_id is None


def test_parse_availability_page_second_page_returns_different_players() -> None:
    page = parse_availability_page(_read("availability_page_25.html"))
    assert len(page.rows) == 25
    # The user captured offset=25; verify it isn't the same first row
    # as page 0 (Mahomes), proving the two pages aren't duplicates.
    assert page.rows[0].player_name != "Patrick Mahomes"
    assert page.total_count == 875


# ---------------------------------------------------------------------------
# parse_gamecenter
# ---------------------------------------------------------------------------


def test_parse_gamecenter_extracts_both_sides_and_full_rosters() -> None:
    # Real /league/.../history/2025/teamgamecenter?teamId=4 capture —
    # Week 17 matchup of team 4 (Wizard) vs team 10 (London).
    gc = parse_gamecenter(_read("teamgamecenter.html"))

    assert gc.home.team_id == 4
    assert gc.home.team_name == "The Wizard Of BAA'z"
    assert gc.home.total_points == pytest.approx(84.60)
    assert len(gc.home.entries) == 16

    assert gc.away.team_id == 10
    assert gc.away.team_name == "London on da Track"
    assert gc.away.total_points == pytest.approx(159.12)
    assert len(gc.away.entries) == 16

    # Home QB starter on team 4 in week 17 was Joe Burrow.
    qb = next(e for e in gc.home.entries if e.roster_slot == "QB")
    assert qb.is_starter is True
    assert qb.player_id == "2563722"
    assert qb.position == "QB"
    assert qb.nfl_team == "CIN"

    # Both sides include at least one bench and one reserve slot.
    home_slots = {e.roster_slot for e in gc.home.entries}
    assert "BN" in home_slots
    assert "RES" in home_slots


def test_parse_gamecenter_missing_team_wraps_raises() -> None:
    with pytest.raises(ParseError, match="teamWrap"):
        parse_gamecenter("<html><body><div>nothing here</div></body></html>")


# ---------------------------------------------------------------------------
# _position_and_team_from_row — junk-position rejection
# ---------------------------------------------------------------------------


def _row(em_html: str) -> object:
    tr = BeautifulSoup(f"<table><tr>{em_html}</tr></table>", "lxml").select_one("tr")
    assert tr is not None
    return tr


def test_position_parses_valid_em() -> None:
    assert _position_and_team_from_row(_row("<em>QB - CIN</em>")) == ("QB", "CIN")
    # Team defenses render the position alone, no team.
    assert _position_and_team_from_row(_row("<em>DEF</em>")) == ("DEF", None)


def test_position_rejects_ui_note_and_slot_label() -> None:
    # Inactive players: NFL.com reuses the <em> for a watch-list note.
    assert _position_and_team_from_row(_row("<em>Season is Over - Add to Watch List</em>")) == (
        None,
        None,
    )
    # Unfilled flex slot leaks its slot label into the position <em>.
    assert _position_and_team_from_row(_row("<em>R/W/ T</em>")) == (None, None)


# ---------------------------------------------------------------------------
# parse_standings
# ---------------------------------------------------------------------------


def test_parse_standings_extracts_full_finish_order() -> None:
    parsed = parse_standings(_read("standings_2024.html"))
    assert len(parsed.entries) == 12
    # Finish order is 1..12 with no gaps.
    assert [e.final_rank for e in parsed.entries] == list(range(1, 13))
    assert parsed.champion_team_id == 6
    assert parsed.runner_up_team_id == 4
    assert parsed.last_place_team_id == 11


def test_parse_standings_medal_rows_carry_record_and_points() -> None:
    parsed = parse_standings(_read("standings_2024.html"))
    champ = parsed.entries[0]
    assert champ.final_rank == 1
    assert champ.team_id == 6
    assert champ.owner_name == "Dave"
    assert (champ.reg_wins, champ.reg_losses, champ.reg_ties) == (8, 6, 0)
    assert champ.points_for == 1765.40
    assert champ.team_name == "Putting the CAP in CHAMP"


def test_parse_standings_non_medal_rows_have_no_record() -> None:
    parsed = parse_standings(_read("standings_2024.html"))
    # Places 4-12 only render rank + team in the static HTML.
    non_medal = [e for e in parsed.entries if e.final_rank >= 4]
    assert non_medal
    assert all(e.reg_wins is None and e.points_for is None for e in non_medal)
    assert all(e.team_id is not None for e in non_medal)


def test_parse_standings_handles_a_second_season() -> None:
    parsed = parse_standings(_read("standings_2018.html"))
    assert parsed.champion_team_id == 11
    assert parsed.runner_up_team_id == 3
    assert parsed.last_place_team_id == 12
    assert parsed.entries[0].points_for == 1858.34


def test_parse_standings_empty_raises() -> None:
    with pytest.raises(ParseError, match="place-N"):
        parse_standings("<html><body><p>no standings here</p></body></html>")


# ---------------------------------------------------------------------------
# Draft results
# ---------------------------------------------------------------------------


def test_parse_draft_round_numbers_lists_every_round() -> None:
    rounds = parse_draft_round_numbers(_read("draftresults_2024_round1.html"))
    assert rounds == tuple(range(1, 16))


def test_parse_draft_round_numbers_empty_when_no_nav() -> None:
    assert parse_draft_round_numbers("<html><body>no draft</body></html>") == ()


def test_parse_draft_picks_round_one_overall_numbering() -> None:
    picks = parse_draft_picks(_read("draftresults_2024_round1.html"))
    assert len(picks) == 12
    first = picks[0]
    assert first.overall_pick == 1
    assert first.draft_round == 1
    assert first.player_name == "Christian McCaffrey"
    assert first.player_id == "2557997"
    assert first.position == "RB"
    assert first.nfl_team == "SF"
    assert first.team_id == 3
    # The .count cell is the global pick number, so round 1 is 1..12.
    assert [p.overall_pick for p in picks] == list(range(1, 13))


def test_parse_draft_picks_round_two_continues_overall_count() -> None:
    picks = parse_draft_picks(_read("draftresults_2024_round2.html"))
    assert len(picks) == 12
    # Round 2 of a 12-team draft carries overall picks 13..24 and reverses
    # the round-1 team order (snake) — pick 13 is the team that picked last.
    assert all(p.draft_round == 2 for p in picks)
    assert [p.overall_pick for p in picks] == list(range(13, 25))
    assert picks[0].player_name == "Jahmyr Gibbs"
    assert picks[0].team_id == 9


def test_parse_draft_picks_empty_page_returns_empty() -> None:
    assert parse_draft_picks("<html><body><p>no draft module</p></body></html>") == []
