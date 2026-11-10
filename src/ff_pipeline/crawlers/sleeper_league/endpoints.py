"""Sleeper league endpoints and the per-run request plan (S2).

The plan is pure: given ``/state/nfl``, the league payload and the drafts
list, it returns every request one capture run makes. ``capture.py`` fetches
them and archives the bytes; nothing here touches the network or the DB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

BASE_APP = "https://api.sleeper.app/v1"
BASE_COM = "https://api.sleeper.com"

# Highest NFL week Sleeper serves league data for (17-game season + week 18).
NFL_MAX_WEEK = 18

SOURCE = "sleeper_league"


class Endpoint(StrEnum):
    STATE = "state"
    LEAGUE = "league"
    USERS = "users"
    ROSTERS = "rosters"
    MATCHUPS = "matchups"
    TRANSACTIONS = "transactions"
    TRADED_PICKS = "traded_picks"
    WINNERS_BRACKET = "winners_bracket"
    LOSERS_BRACKET = "losers_bracket"
    DRAFTS = "drafts"
    DRAFT = "draft"
    DRAFT_PICKS = "draft_picks"
    PLAYERS = "players"
    STATS_WEEK = "stats_week"
    USER_LEAGUES = "user_leagues"  # only fetched to discover a renewed league (S9-D4)


@dataclass(frozen=True, slots=True)
class PayloadRequest:
    """One GET a capture run makes, plus the index metadata for its row."""

    endpoint: Endpoint
    url: str
    league_id: str | None = None
    season_year: int | None = None
    week: int | None = None

    @property
    def fixture_name(self) -> str:
        """Stable file name for this request in a fixture directory.

        Derived from the URL path + query, e.g.
        ``v1/league/1/matchups/3`` → ``v1_league_1_matchups_3.json``.
        """
        path = self.url.split("://", 1)[-1].split("/", 1)[-1]
        return re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") + ".json"


# ----- request builders -----------------------------------------------------


def state_request() -> PayloadRequest:
    return PayloadRequest(Endpoint.STATE, f"{BASE_APP}/state/nfl")


def league_request(league_id: str, season_year: int | None = None) -> PayloadRequest:
    return PayloadRequest(Endpoint.LEAGUE, f"{BASE_APP}/league/{league_id}", league_id, season_year)


def users_request(league_id: str, season_year: int | None = None) -> PayloadRequest:
    return PayloadRequest(
        Endpoint.USERS, f"{BASE_APP}/league/{league_id}/users", league_id, season_year
    )


def user_leagues_request(user_id: str, season_year: int) -> PayloadRequest:
    """A user's leagues for one season: where a renewed league shows up (S9-D4)."""
    return PayloadRequest(
        Endpoint.USER_LEAGUES,
        f"{BASE_APP}/user/{user_id}/leagues/nfl/{season_year}",
        None,
        season_year,
    )


def players_request() -> PayloadRequest:
    return PayloadRequest(Endpoint.PLAYERS, f"{BASE_APP}/players/nfl")


def drafts_request(league_id: str, season_year: int) -> PayloadRequest:
    return PayloadRequest(
        Endpoint.DRAFTS, f"{BASE_APP}/league/{league_id}/drafts", league_id, season_year
    )


def draft_requests(league_id: str, season_year: int, draft_id: str) -> list[PayloadRequest]:
    return [
        PayloadRequest(Endpoint.DRAFT, f"{BASE_APP}/draft/{draft_id}", league_id, season_year),
        PayloadRequest(
            Endpoint.DRAFT_PICKS, f"{BASE_APP}/draft/{draft_id}/picks", league_id, season_year
        ),
    ]


def league_requests(league_id: str, season_year: int, max_week: int) -> list[PayloadRequest]:
    """Every league-scoped request except ``league`` / ``drafts`` / draft detail."""
    base = f"{BASE_APP}/league/{league_id}"
    reqs = [
        PayloadRequest(Endpoint.USERS, f"{base}/users", league_id, season_year),
        PayloadRequest(Endpoint.ROSTERS, f"{base}/rosters", league_id, season_year),
        PayloadRequest(Endpoint.TRADED_PICKS, f"{base}/traded_picks", league_id, season_year),
        PayloadRequest(Endpoint.WINNERS_BRACKET, f"{base}/winners_bracket", league_id, season_year),
        PayloadRequest(Endpoint.LOSERS_BRACKET, f"{base}/losers_bracket", league_id, season_year),
    ]
    for w in range(1, max_week + 1):
        reqs.append(
            PayloadRequest(Endpoint.MATCHUPS, f"{base}/matchups/{w}", league_id, season_year, w)
        )
    for r in range(1, max_week + 1):
        reqs.append(
            PayloadRequest(
                Endpoint.TRANSACTIONS, f"{base}/transactions/{r}", league_id, season_year, r
            )
        )
    for w in range(1, max_week + 1):
        reqs.append(
            PayloadRequest(
                Endpoint.STATS_WEEK,
                f"{BASE_COM}/stats/nfl/{season_year}/{w}?season_type=regular",
                None,
                season_year,
                w,
            )
        )
    return reqs


# ----- planning -------------------------------------------------------------


def weeks_to_capture(state: dict[str, Any], league: dict[str, Any]) -> int:
    """How many weeks (1..N) of matchups / transactions / stats to fetch.

    * Before the draft: 0.
    * The league's season is the current NFL season in the regular season:
      the current week (in progress included).
    * Otherwise (NFL postseason, or a past season): every week.
    """
    status = str(league.get("status") or "")
    if status in {"pre_draft", "drafting"}:
        return 0
    league_season = _as_int(league.get("season"))
    state_season = _as_int(state.get("season"))
    if league_season is not None and league_season == state_season:
        season_type = str(state.get("season_type") or "")
        if season_type == "pre":
            return 0
        if season_type == "regular":
            week = _as_int(state.get("week")) or 0
            return max(0, min(week, NFL_MAX_WEEK))
    return NFL_MAX_WEEK


def draft_ids(league: dict[str, Any], drafts: Any) -> list[str]:
    """Draft ids from ``/league/{id}/drafts`` plus ``league.draft_id``, ordered, unique."""
    ids: list[str] = []
    if isinstance(drafts, list):
        for d in drafts:
            if isinstance(d, dict) and d.get("draft_id"):
                ids.append(str(d["draft_id"]))
    if league.get("draft_id"):
        ids.append(str(league["draft_id"]))
    return list(dict.fromkeys(ids))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BASE_APP",
    "BASE_COM",
    "NFL_MAX_WEEK",
    "SOURCE",
    "Endpoint",
    "PayloadRequest",
    "draft_ids",
    "draft_requests",
    "drafts_request",
    "league_request",
    "league_requests",
    "players_request",
    "state_request",
    "weeks_to_capture",
]
