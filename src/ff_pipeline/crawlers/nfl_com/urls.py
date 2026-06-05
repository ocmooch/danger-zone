"""URL templates for NFL.com fantasy pages.

One place for every URL we hit, so when the league host or path shape
changes there's a single edit point. Builders are pure functions of the
inputs (league_id, season, week, ...). No I/O.
"""

from __future__ import annotations

BASE_URL = "https://fantasy.nfl.com"


def league_home(league_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}"


def league_history(league_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}/history"


def season_home(league_id: str, year: int) -> str:
    return f"{BASE_URL}/league/{league_id}/history/{year}"


def owners(league_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}/owners"


def history_owners(league_id: str, year: int) -> str:
    """Per-season owners page — the human manager of each franchise *that year*.

    Unlike the year-less ``/owners`` page (today's managers only), this view is
    parameterized by season, so it reveals ownership handoffs: a franchise's
    ``userId`` changing across seasons is a transfer to a different person.
    """
    return f"{BASE_URL}/league/{league_id}/history/{year}/owners"


def settings(league_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}/settings"


def draft_results(league_id: str, year: int, round_: int | None = None) -> str:
    """Draft-results page for a season.

    With ``round_`` omitted, returns the default "By Round" view (round 1
    in the static HTML). With ``round_=N``, returns that round's view —
    NFL.com only renders one round's picks per page, so capturing the
    whole draft means hitting each round in turn. Every round page's
    ``span.count`` carries the *overall* pick number (snake order), so
    ordering survives the per-round fetch.
    """
    base = f"{BASE_URL}/league/{league_id}/history/{year}/draftresults"
    if round_ is None:
        return base
    return f"{base}?draftResultsDetail={round_}&draftResultsTab=round&draftResultsType=results"


def standings(league_id: str, year: int) -> str:
    return f"{BASE_URL}/league/{league_id}/history/{year}/standings"


def weekly_matchups(league_id: str, year: int, week: int) -> str:
    return f"{BASE_URL}/league/{league_id}/history/{year}/schedule?scheduleDetail={week}"


def gamecenter(league_id: str, game_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}/gamecenter?gameId={game_id}"


def team_gamecenter(league_id: str, year: int, team_id: int | str, week: int) -> str:
    """Per-team-per-week historical lineup view.

    Used by the M9 verifier to read NFL.com's stored per-player point
    totals for one side of a matchup; the parser handles both
    ``gamecenter`` and ``teamgamecenter`` markup.
    """
    return (
        f"{BASE_URL}/league/{league_id}/history/{year}/teamgamecenter?teamId={team_id}&week={week}"
    )


def transactions(league_id: str, year: int) -> str:
    return f"{BASE_URL}/league/{league_id}/history/{year}/transactions"


def team_home(league_id: str, team_id: int | str) -> str:
    return f"{BASE_URL}/league/{league_id}/team/{team_id}"


def league_players(league_id: str, year: int, week: int, offset: int = 0) -> str:
    """League-wide player universe page. Drives `player_availability`.

    ``offset`` is the 0-based row offset for pagination (NFL.com renders 25
    rows per page; offset advances by 25 per page).
    """
    return (
        f"{BASE_URL}/league/{league_id}/players"
        f"?statCategory=stats&statSeason={year}&statType=weekStats&statWeek={week}"
        f"&offset={offset}"
    )


def waivers(league_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}/waivers"


__all__ = [
    "BASE_URL",
    "draft_results",
    "gamecenter",
    "history_owners",
    "league_history",
    "league_home",
    "league_players",
    "owners",
    "season_home",
    "settings",
    "standings",
    "team_gamecenter",
    "team_home",
    "transactions",
    "waivers",
    "weekly_matchups",
]
