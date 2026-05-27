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


def settings(league_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}/settings"


def draft_results(league_id: str, year: int) -> str:
    return f"{BASE_URL}/league/{league_id}/history/{year}/draftresults"


def standings(league_id: str, year: int) -> str:
    return f"{BASE_URL}/league/{league_id}/history/{year}/standings"


def weekly_matchups(league_id: str, year: int, week: int) -> str:
    return f"{BASE_URL}/league/{league_id}/history/{year}/schedule?scheduleDetail={week}"


def gamecenter(league_id: str, game_id: str) -> str:
    return f"{BASE_URL}/league/{league_id}/gamecenter?gameId={game_id}"


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
    "league_history",
    "league_home",
    "league_players",
    "owners",
    "season_home",
    "settings",
    "standings",
    "team_home",
    "transactions",
    "waivers",
    "weekly_matchups",
]
