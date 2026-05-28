"""HTML parsers for the NFL.com fantasy pages.

One pure function per page type. Each parser:

* Takes raw HTML (string)
* Returns a typed dataclass (or list thereof)
* Raises ``ParseError`` with the failing selector when the page shape
  doesn't match expectations (so 03_DATA_SOURCES.md's "log + skip"
  strategy stays in the *caller*, not buried in the parser)

Parsers DO NOT touch the network and DO NOT touch the database; they're
fully unit-testable against fixture HTML. Every "where in the DOM is
this?" lives here, so when NFL.com changes a CSS class we only edit
this file plus the fixtures.

Selector contract is anchored on table classes documented by the
upstream open-source scrapers and verified against current NFL.com
HTML:

    tableType-team             owners list
    tableType-roster           team roster
    tableType-rosterTrades     transactions log
    tableType-standings        season standings
    tableType-playerStats      league-wide players (availability)
    tableType-results          weekly matchup schedule
    tableType-fullRosterStats  gamecenter lineup blocks

When NFL.com renames a class, update the constant block at the bottom
of this module and re-run the fixture-based unit tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup

from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from bs4 import Tag

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ParseError(RuntimeError):
    """Raised when a parser can't find an expected element.

    The exception message includes the missing selector so callers can
    decide whether to log+skip or fail the run.
    """


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedLeagueHome:
    """Top-of-funnel league info. Drives ``leagues`` row + current week."""

    league_id: str
    league_name: str
    current_season_year: int | None
    current_week: int | None
    nfl_user_id: str | None = None
    current_team_id: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedOwner:
    """One row from the league owners page."""

    nfl_user_id: str | None
    display_name: str
    team_id: int | None
    team_name: str | None


@dataclass(frozen=True, slots=True)
class ParsedRosterEntry:
    """One slot on a team's roster (one player or an empty bench slot)."""

    roster_slot: str
    is_starter: bool
    player_id: str | None
    player_name: str | None
    position: str | None
    nfl_team: str | None
    opponent: str | None
    game_status: str | None


@dataclass(frozen=True, slots=True)
class ParsedTeamRoster:
    """A team's full roster as it appears on /league/{LID}/team/{TID}."""

    team_id: int
    team_name: str | None
    entries: tuple[ParsedRosterEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ParsedMatchup:
    """One side of a head-to-head matchup from the schedule page."""

    team_id: int
    team_name: str | None
    team_score: float | None
    opponent_team_id: int | None
    opponent_team_name: str | None
    opponent_score: float | None
    game_id: str | None
    is_playoff: bool
    is_consolation: bool


@dataclass(frozen=True, slots=True)
class ParsedTransaction:
    """One transaction row from the transactions log."""

    nfl_transaction_id: str | None
    transaction_type: str  # 'draft' | 'trade' | 'waiver_add' | 'free_agent_add' | 'drop' | 'ir_*'
    executed_at: str | None  # ISO-ish text; runner re-parses to datetime
    effective_week: int | None
    team_id: int | None
    counterpart_team_id: int | None
    player_id: str | None
    player_name: str | None
    direction: str | None  # 'in' | 'out'
    notes: str | None


@dataclass(frozen=True, slots=True)
class ParsedAvailability:
    """One row from the league-wide players page."""

    player_id: str
    player_name: str
    position: str | None
    nfl_team: str | None
    status: str  # 'OWNED' | 'FREE_AGENT' | 'ON_WAIVERS'
    owning_team_id: int | None
    waiver_claim_deadline: str | None  # raw text; runner parses


@dataclass(frozen=True, slots=True)
class ParsedAvailabilityPage:
    """One page (default 25 rows) of league-wide player availability."""

    rows: tuple[ParsedAvailability, ...]
    total_count: int | None
    next_offset: int | None


# ---------------------------------------------------------------------------
# League home
# ---------------------------------------------------------------------------


_LEAGUE_ID_FROM_HREF = re.compile(r"/league/(\d+)")
_WEEK_FROM_TEXT = re.compile(r"Week\s+(\d+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2}|21\d{2})\b")

# Live NFL.com league pages embed two reliable data sources we tap before
# falling back to DOM heuristics:
#   * ``window.analyticsData = { user: { leagueID, userID, teamID, ... } }``
#     — clean JSON, gives league + user + own-team IDs.
#   * ``Y.Scores.init(..., {leagueIds:[N],week:N,gameId:N, dp:false, delay:N, season:NNNN}, ...)``
#     — JS-literal syntax (unquoted keys), so we grab ``week`` / ``season``
#     via targeted regex rather than a JSON load.
_ANALYTICS_DATA_RE = re.compile(
    r"window\.analyticsData\s*=\s*(\{.*?\});\s*\n", re.DOTALL
)
_SCORES_INIT_WEEK_RE = re.compile(r"\bweek\s*:\s*(\d+)")
_SCORES_INIT_SEASON_RE = re.compile(r"\bseason\s*:\s*(\d+)")
_LEAGUE_NAME_HOME_SUFFIX_RE = re.compile(r"\s+Home$")


def parse_league_home(html: str) -> ParsedLeagueHome:
    soup = BeautifulSoup(html, "lxml")
    analytics = _extract_analytics_data(html)

    league_id = _league_id_from_analytics(analytics) or _league_id_from_soup(soup)
    if league_id is None:
        raise ParseError("league_home: could not extract league_id")

    league_name = _extract_league_name(soup)
    if league_name is None:
        raise ParseError("league_home: missing leagueName / h1.title / <title>")

    current_week, current_season_year = _extract_week_and_season(html, soup)
    nfl_user_id, current_team_id = _extract_user_and_team(analytics)

    return ParsedLeagueHome(
        league_id=league_id,
        league_name=league_name,
        current_season_year=current_season_year,
        current_week=current_week,
        nfl_user_id=nfl_user_id,
        current_team_id=current_team_id,
    )


def _extract_analytics_data(html: str) -> dict[str, Any] | None:
    """Pull ``window.analyticsData = {...}`` JSON out of the inline script.

    Returns ``None`` if the block is missing (e.g., logged-out or A/B
    variant) so the caller can fall back to DOM heuristics rather than
    treating its absence as a hard failure.
    """
    match = _ANALYTICS_DATA_RE.search(html)
    if not match:
        return None
    try:
        decoded: dict[str, Any] = json.loads(match.group(1))
    except json.JSONDecodeError:
        log.warning("league_home: analyticsData block found but failed to decode as JSON")
        return None
    return decoded


def _league_id_from_analytics(analytics: dict[str, Any] | None) -> str | None:
    if not analytics:
        return None
    user = analytics.get("user") or {}
    league_id = user.get("leagueID")
    return str(league_id) if league_id else None


def _league_id_from_soup(soup: BeautifulSoup) -> str | None:
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        match = _LEAGUE_ID_FROM_HREF.search(href)
        if match:
            return match.group(1)
    return None


def _extract_league_name(soup: BeautifulSoup) -> str | None:
    """Read the league name and strip NFL.com's "<League> Home" suffix.

    The live page renders ``<h1 class="title">{LeagueName} Home</h1>``;
    the synthetic fallback used ``.leagueName``. We try both and strip a
    trailing " Home" because the league name in the leagues table should
    not include the page label.
    """
    for selector in (".leagueName", "h1.title", "h1"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = node.get_text(strip=True)
        if text:
            return _LEAGUE_NAME_HOME_SUFFIX_RE.sub("", text)
    title_node = soup.select_one("title")
    if title_node is not None:
        title = title_node.get_text(strip=True)
        # ``<title>The Danger Zone Home | NFL Fantasy</title>``
        first_segment = title.split("|", 1)[0].strip()
        if first_segment:
            return _LEAGUE_NAME_HOME_SUFFIX_RE.sub("", first_segment)
    return None


def _extract_week_and_season(
    html: str, soup: BeautifulSoup
) -> tuple[int | None, int | None]:
    """Prefer the ``Y.Scores.init`` config block, fall back to DOM scans."""
    week_match = _SCORES_INIT_WEEK_RE.search(html)
    season_match = _SCORES_INIT_SEASON_RE.search(html)
    current_week = int(week_match.group(1)) if week_match else _extract_current_week(soup)
    current_season_year = (
        int(season_match.group(1)) if season_match else _extract_current_year(soup)
    )
    return current_week, current_season_year


def _extract_user_and_team(
    analytics: dict[str, Any] | None,
) -> tuple[str | None, int | None]:
    if not analytics:
        return None, None
    user = analytics.get("user") or {}
    user_id = user.get("userID")
    team_id = user.get("teamID")
    try:
        parsed_team_id = int(team_id) if team_id else None
    except (TypeError, ValueError):
        parsed_team_id = None
    return (str(user_id) if user_id else None, parsed_team_id)


def _extract_current_week(soup: BeautifulSoup) -> int | None:
    # NFL.com pages typically have a "Week N" indicator in the navigation bar.
    for node in soup.select(".week, .scheduleWeekNav, .currentWeek"):
        text = node.get_text(" ", strip=True)
        match = _WEEK_FROM_TEXT.search(text)
        if match:
            return int(match.group(1))
    # Fallback: scan body text once.
    match = _WEEK_FROM_TEXT.search(soup.get_text(" ", strip=True))
    return int(match.group(1)) if match else None


def _extract_current_year(soup: BeautifulSoup) -> int | None:
    for node in soup.select(".seasonNav, .currentSeason, h1, .leagueName"):
        match = _YEAR_RE.search(node.get_text(" ", strip=True))
        if match:
            return int(match.group(1))
    match = _YEAR_RE.search(soup.get_text(" ", strip=True))
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------


_TEAM_ID_FROM_HREF = re.compile(r"/team/(\d+)")
_USER_ID_FROM_HREF = re.compile(r"/user/(\d+)")


def parse_owners(html: str) -> list[ParsedOwner]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.tableType-team") or soup.select_one("table.tableType-owners")
    if table is None:
        raise ParseError("owners: missing table.tableType-team")

    out: list[ParsedOwner] = []
    for tr in table.select("tbody tr"):
        team_anchor = tr.select_one("a.teamName") or _first_anchor_with_href(tr, "/team/")
        owner_anchor = tr.select_one("a.userName") or _first_anchor_with_href(tr, "/user/")
        if team_anchor is None and owner_anchor is None:
            continue
        team_id = _id_from_anchor(team_anchor, _TEAM_ID_FROM_HREF) if team_anchor else None
        user_id = _id_from_anchor(owner_anchor, _USER_ID_FROM_HREF) if owner_anchor else None
        display_name = owner_anchor.get_text(strip=True) if owner_anchor else ""
        team_name = team_anchor.get_text(strip=True) if team_anchor else None
        out.append(
            ParsedOwner(
                nfl_user_id=str(user_id) if user_id else None,
                display_name=display_name or "(unknown)",
                team_id=team_id,
                team_name=team_name,
            )
        )
    if not out:
        raise ParseError("owners: tableType-team had no parseable rows")
    return out


def _first_anchor_with_href(node: Tag, substring: str) -> Tag | None:
    for a in node.find_all("a", href=True):
        if substring in a["href"]:
            return a
    return None


def _id_from_anchor(anchor: Tag | None, pattern: re.Pattern[str]) -> int | None:
    if anchor is None:
        return None
    href = anchor.get("href", "")
    if not isinstance(href, str):
        return None
    match = pattern.search(href)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Team roster
# ---------------------------------------------------------------------------


_PLAYER_ID_FROM_HREF = re.compile(r"playerId=(\d+)|/player/(\d+)")
# Live NFL.com player anchors carry the id redundantly in a CSS class
# (``playerNameId-NNN``). Used as a last-resort fallback when the href
# is missing or shaped differently in an A/B variant.
_PLAYER_ID_FROM_CLASS = re.compile(r"playerNameId-(\d+)")


def parse_team_roster(html: str) -> ParsedTeamRoster:
    soup = BeautifulSoup(html, "lxml")

    team_anchor = soup.select_one(".teamWrap a.teamName") or _first_anchor_with_href(soup, "/team/")
    if team_anchor is None:
        raise ParseError("team_roster: cannot find team header anchor")
    team_id = _id_from_anchor(team_anchor, _TEAM_ID_FROM_HREF)
    if team_id is None:
        raise ParseError("team_roster: team_id not extractable from anchor href")
    team_name = team_anchor.get_text(strip=True) or None

    table = soup.select_one("table.tableType-roster")
    if table is None:
        raise ParseError("team_roster: missing table.tableType-roster")

    entries: list[ParsedRosterEntry] = []
    for tr in table.select("tbody tr"):
        slot_node = tr.select_one(".teamPosition") or tr.select_one("td.teamPosition")
        if slot_node is None:
            # Some rows (separator headers) have no slot; skip.
            continue
        roster_slot = slot_node.get_text(strip=True)
        if not roster_slot:
            continue
        # Bench/IR slots have BN1..BN6 / IR1..IR2 etc. Anything not BN/IR is a starter.
        is_starter = not (roster_slot.startswith("BN") or roster_slot.startswith("IR"))

        player_anchor = tr.select_one("a.playerName") or _first_player_anchor(tr)
        player_id = _player_id_from_anchor(player_anchor) if player_anchor is not None else None
        player_name = player_anchor.get_text(strip=True) if player_anchor else None

        position_node = tr.select_one(".playerPosition") or tr.select_one("em.playerPosition")
        nfl_team_node = tr.select_one(".playerTeam") or tr.select_one("em.playerTeam")
        opp_node = tr.select_one(".playerOpponent")
        status_node = tr.select_one(".playerGameStatus")

        entries.append(
            ParsedRosterEntry(
                roster_slot=roster_slot,
                is_starter=is_starter,
                player_id=str(player_id) if player_id is not None else None,
                player_name=player_name,
                position=position_node.get_text(strip=True) if position_node else None,
                nfl_team=nfl_team_node.get_text(strip=True) if nfl_team_node else None,
                opponent=opp_node.get_text(strip=True) if opp_node else None,
                game_status=status_node.get_text(strip=True) if status_node else None,
            )
        )

    if not entries:
        raise ParseError("team_roster: tableType-roster had no rows")

    return ParsedTeamRoster(team_id=team_id, team_name=team_name, entries=tuple(entries))


def _first_player_anchor(node: Tag) -> Tag | None:
    """Find the first anchor that points at a player card.

    Live NFL.com uses ``/players/card?leagueId=X&playerId=NNN`` for the
    in-page player card link and ``/player/NNN`` for the standalone page.
    Either is fine — we just need *some* anchor that carries the player id.
    """
    for a in node.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        if "playerId=" in href or "/player/" in href:
            return a
    return None


def _player_id_from_anchor(anchor: Tag) -> str | None:
    href = anchor.get("href", "")
    if isinstance(href, str):
        match = _PLAYER_ID_FROM_HREF.search(href)
        if match:
            return match.group(1) or match.group(2)
    classes: list[str] | str = anchor.get("class") or []
    if isinstance(classes, list):
        for cls in classes:
            class_match = _PLAYER_ID_FROM_CLASS.search(cls)
            if class_match:
                return class_match.group(1)
    return None


# ---------------------------------------------------------------------------
# Weekly matchups
# ---------------------------------------------------------------------------


_GAME_ID_FROM_HREF = re.compile(r"gameId=(\d+)")
_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_weekly_matchups(html: str) -> list[ParsedMatchup]:
    """Parse the weekly schedule page.

    Each <li class="matchup"> holds two team blocks. We emit ONE
    ParsedMatchup per *team* (mirroring the `matchups` table's two-rows-
    per-game convention).
    """
    soup = BeautifulSoup(html, "lxml")
    matchups = soup.select("li.matchup, .matchupItem, .scheduleMatchupItem")
    if not matchups:
        raise ParseError("weekly_matchups: no .matchup elements")

    out: list[ParsedMatchup] = []
    for item in matchups:
        is_playoff = "playoff" in (item.get("class") or [])
        is_consolation = "consolation" in (item.get("class") or [])

        team_blocks = item.select(".teamWrap, .teamSection, .matchupTeam")
        if len(team_blocks) < 2:
            continue

        game_id = _game_id_from_anchors(item)
        a, b = team_blocks[0], team_blocks[1]
        a_parsed = _parse_matchup_team_block(a, game_id, is_playoff, is_consolation)
        b_parsed = _parse_matchup_team_block(b, game_id, is_playoff, is_consolation)
        if a_parsed is None or b_parsed is None:
            continue
        # Stitch in the opponent on each side.
        out.append(
            ParsedMatchup(
                team_id=a_parsed.team_id,
                team_name=a_parsed.team_name,
                team_score=a_parsed.team_score,
                opponent_team_id=b_parsed.team_id,
                opponent_team_name=b_parsed.team_name,
                opponent_score=b_parsed.team_score,
                game_id=game_id,
                is_playoff=is_playoff,
                is_consolation=is_consolation,
            )
        )
        out.append(
            ParsedMatchup(
                team_id=b_parsed.team_id,
                team_name=b_parsed.team_name,
                team_score=b_parsed.team_score,
                opponent_team_id=a_parsed.team_id,
                opponent_team_name=a_parsed.team_name,
                opponent_score=a_parsed.team_score,
                game_id=game_id,
                is_playoff=is_playoff,
                is_consolation=is_consolation,
            )
        )
    if not out:
        raise ParseError("weekly_matchups: every matchup failed to parse")
    return out


def _game_id_from_anchors(node: Tag) -> str | None:
    for a in node.find_all("a", href=True):
        href = a["href"]
        if not isinstance(href, str):
            continue
        match = _GAME_ID_FROM_HREF.search(href)
        if match:
            return match.group(1)
    return None


def _parse_matchup_team_block(
    block: Tag,
    game_id: str | None,
    is_playoff: bool,
    is_consolation: bool,
) -> ParsedMatchup | None:
    """Return a ParsedMatchup with the *opponent* fields left blank.

    Used as an intermediate by ``parse_weekly_matchups`` before the two
    halves are stitched together.
    """
    _ = (game_id, is_playoff, is_consolation)  # reserved for future schema details
    anchor = block.select_one("a.teamName") or _first_anchor_with_href(block, "/team/")
    if anchor is None:
        return None
    team_id = _id_from_anchor(anchor, _TEAM_ID_FROM_HREF)
    if team_id is None:
        return None
    team_name = anchor.get_text(strip=True) or None

    score_node = block.select_one(".teamTotal") or block.select_one(".totalPts")
    score = _parse_float(score_node.get_text(strip=True) if score_node else None)

    return ParsedMatchup(
        team_id=team_id,
        team_name=team_name,
        team_score=score,
        opponent_team_id=None,
        opponent_team_name=None,
        opponent_score=None,
        game_id=None,
        is_playoff=False,
        is_consolation=False,
    )


def _parse_float(text: str | None) -> float | None:
    if not text:
        return None
    match = _FLOAT_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


# Map the literal NFL.com transaction-type strings to our enum values.
_TXN_TYPE_MAP = {
    "draft pick": "draft",
    "drafted": "draft",
    "added": "free_agent_add",
    "waiver claim": "waiver_add",
    "claimed off waivers": "waiver_add",
    "dropped": "drop",
    "released": "drop",
    "trade": "trade",
    "traded": "trade",
    "ir placement": "ir_placement",
    "moved to ir": "ir_placement",
    "activated from ir": "ir_activation",
    "ir activation": "ir_activation",
}


def parse_transactions(html: str) -> list[ParsedTransaction]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.tableType-rosterTrades") or soup.select_one(
        "table.tableType-transactions"
    )
    if table is None:
        raise ParseError("transactions: missing table.tableType-rosterTrades")

    out: list[ParsedTransaction] = []
    for tr in table.select("tbody tr"):
        out.extend(_parse_transaction_row(tr))
    if not out:
        raise ParseError("transactions: tableType-rosterTrades had no parseable rows")
    return out


def _parse_transaction_row(tr: Tag) -> list[ParsedTransaction]:
    """One <tr> can describe one or more (player, direction) moves.

    Trades show up as a single row with multiple player anchors; we
    emit one ParsedTransaction per anchor with the right direction.
    """
    type_node = tr.select_one(".transactionType") or tr.select_one("td.transactionType")
    date_node = tr.select_one(".transactionDate") or tr.select_one("td.transactionDate")
    notes_node = tr.select_one(".transactionNote")
    week_node = tr.select_one(".transactionWeek")

    raw_type = (type_node.get_text(strip=True) if type_node else "").lower()
    txn_type = _TXN_TYPE_MAP.get(raw_type) or _fuzzy_txn_type(raw_type)
    executed_at = date_node.get_text(strip=True) if date_node else None
    effective_week = _parse_int(week_node.get_text(strip=True) if week_node else None)
    notes = notes_node.get_text(" ", strip=True) if notes_node else None
    nfl_txn_id = tr.get("data-transaction-id") or tr.get("id")

    if txn_type is None:
        log.warning("Unknown transaction type", raw_type=raw_type)
        return []

    moves: list[ParsedTransaction] = []
    for move_node in tr.select(".playerMove, .transactionItem, td.transactionPlayer"):
        team_anchor = _first_anchor_with_href(move_node, "/team/")
        player_anchor = _first_player_anchor(move_node)
        if player_anchor is None and team_anchor is None:
            continue
        direction = _direction_from_text(move_node.get_text(" ", strip=True))
        moves.append(
            ParsedTransaction(
                nfl_transaction_id=str(nfl_txn_id) if nfl_txn_id else None,
                transaction_type=txn_type,
                executed_at=executed_at,
                effective_week=effective_week,
                team_id=_id_from_anchor(team_anchor, _TEAM_ID_FROM_HREF) if team_anchor else None,
                counterpart_team_id=None,  # filled in by runner for trades
                player_id=_player_id_from_anchor(player_anchor) if player_anchor else None,
                player_name=player_anchor.get_text(strip=True) if player_anchor else None,
                direction=direction,
                notes=notes,
            )
        )
    return moves


def _fuzzy_txn_type(raw: str) -> str | None:
    """Best-effort fallback when the literal text doesn't hit ``_TXN_TYPE_MAP``.

    NFL.com sometimes capitalizes or pluralizes inconsistently; we look
    for the salient substring instead of an exact match.
    """
    for keyword, mapped in _TXN_TYPE_MAP.items():
        if keyword in raw:
            return mapped
    if "drop" in raw:
        return "drop"
    if "add" in raw:
        return "free_agent_add"
    return None


def _direction_from_text(text: str) -> str | None:
    lowered = text.lower()
    if "removed" in lowered or "dropped" in lowered or "out:" in lowered:
        return "out"
    if "added" in lowered or "in:" in lowered or "claimed" in lowered:
        return "in"
    return None


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


# ---------------------------------------------------------------------------
# Settings (scoring page)
# ---------------------------------------------------------------------------


def parse_settings_scoring(html: str) -> dict[str, Any]:
    """Extract the raw scoring-settings block as a key→value dict.

    Returns a *raw* mapping of label → text; conversion to ``ScoringRule``
    rows lives in ``scoring/scraper.py`` so the existing CSV-based
    loader and the HTML-based loader share one parse-to-rules step.

    Keys are NFL.com's label text verbatim (e.g. "Passing Yards",
    "300-399 Passing Yards Bonus"); section headers ("Offense",
    "Defense / Special Teams") are emitted as values keyed by their
    title, with no associated value, so the consumer can re-section
    them.
    """
    soup = BeautifulSoup(html, "lxml")
    section = (
        soup.select_one("#settings .scoringSettings")
        or soup.select_one(".scoringSettings")
        or soup.select_one("#scoringSettings")
    )
    if section is None:
        raise ParseError("settings: missing .scoringSettings container")

    flat: dict[str, str] = {}
    for row in section.select("li, tr, .settingRow"):
        label_node = row.select_one(".label") or row.select_one("th") or row.select_one("dt")
        value_node = row.select_one(".value") or row.select_one("td") or row.select_one("dd")
        if label_node is None or value_node is None:
            continue
        label = label_node.get_text(" ", strip=True).rstrip(":")
        value = value_node.get_text(" ", strip=True)
        if label and value:
            flat[label] = value
    if not flat:
        raise ParseError("settings: no label/value pairs found in .scoringSettings")
    return flat


# ---------------------------------------------------------------------------
# League-wide players (availability)
# ---------------------------------------------------------------------------


_OFFSET_FROM_HREF = re.compile(r"offset=(\d+)")
_AVAILABILITY_STATUS_MAP = {
    "fa": "FREE_AGENT",
    "free agent": "FREE_AGENT",
    "wa": "ON_WAIVERS",
    "waivers": "ON_WAIVERS",
    "on waivers": "ON_WAIVERS",
}


def parse_availability_page(html: str) -> ParsedAvailabilityPage:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.tableType-playerStats") or soup.select_one(
        "table.tableType-player"
    )
    if table is None:
        raise ParseError("availability: missing table.tableType-playerStats")

    rows: list[ParsedAvailability] = []
    for tr in table.select("tbody tr"):
        parsed = _parse_availability_row(tr)
        if parsed is not None:
            rows.append(parsed)
    if not rows:
        raise ParseError("availability: tableType-playerStats had no parseable rows")

    total_count = _extract_availability_total(soup)
    next_offset = _extract_next_offset(soup)
    return ParsedAvailabilityPage(
        rows=tuple(rows),
        total_count=total_count,
        next_offset=next_offset,
    )


def _parse_availability_row(tr: Tag) -> ParsedAvailability | None:
    player_anchor = tr.select_one("a.playerName") or _first_player_anchor(tr)
    if player_anchor is None:
        return None
    player_id = _player_id_from_anchor(player_anchor)
    if player_id is None:
        return None
    player_name = player_anchor.get_text(strip=True)

    pos_node = tr.select_one(".playerPosition") or tr.select_one("em.playerPosition")
    team_node = tr.select_one(".playerTeam") or tr.select_one("em.playerTeam")
    status_node = tr.select_one(".playerStatus") or tr.select_one(".playerOwner")
    owner_anchor = _first_anchor_with_href(tr, "/team/")
    deadline_node = tr.select_one(".waiverClaimDeadline") or tr.select_one(".claimDate")

    status, owning_team_id = _resolve_availability_status(status_node, owner_anchor)
    return ParsedAvailability(
        player_id=player_id,
        player_name=player_name,
        position=pos_node.get_text(strip=True) if pos_node else None,
        nfl_team=team_node.get_text(strip=True) if team_node else None,
        status=status,
        owning_team_id=owning_team_id,
        waiver_claim_deadline=deadline_node.get_text(strip=True) if deadline_node else None,
    )


def _resolve_availability_status(
    status_node: Tag | None, owner_anchor: Tag | None
) -> tuple[str, int | None]:
    """Decide OWNED / FREE_AGENT / ON_WAIVERS for a row.

    Owner anchor wins ("they're rostered → OWNED"). If absent, fall back
    to the status text. We default to FREE_AGENT only when the text is
    unrecognized — better than treating an unknown status as OWNED.
    """
    if owner_anchor is not None:
        owning_team_id = _id_from_anchor(owner_anchor, _TEAM_ID_FROM_HREF)
        if owning_team_id is not None:
            return "OWNED", owning_team_id
    text = (status_node.get_text(strip=True) if status_node else "").lower()
    for keyword, status in _AVAILABILITY_STATUS_MAP.items():
        if keyword in text:
            return status, None
    return "FREE_AGENT", None


def _extract_availability_total(soup: BeautifulSoup) -> int | None:
    # NFL.com renders "1-25 of 1287" in a .paginationSummary span.
    summary = soup.select_one(".paginationSummary") or soup.select_one(".paginationTitle")
    if summary is None:
        return None
    match = re.search(r"of\s+(\d+)", summary.get_text(" ", strip=True), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_next_offset(soup: BeautifulSoup) -> int | None:
    next_link = soup.select_one(".pagination .next a") or soup.select_one("a.next[href]")
    if next_link is None:
        return None
    href = next_link.get("href", "")
    if not isinstance(href, str):
        return None
    match = _OFFSET_FROM_HREF.search(href)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Gamecenter (lineup totals)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedGamecenterSide:
    """One team's lineup in a gamecenter view."""

    team_id: int
    team_name: str | None
    total_points: float | None
    entries: tuple[ParsedRosterEntry, ...]


@dataclass(frozen=True, slots=True)
class ParsedGamecenter:
    home: ParsedGamecenterSide
    away: ParsedGamecenterSide


def parse_gamecenter(html: str) -> ParsedGamecenter:
    soup = BeautifulSoup(html, "lxml")
    sides = soup.select(".teamWrap.teamWrap-1, .teamWrap.teamWrap-2") or soup.select(
        ".gamecenterTeamWrap"
    )
    if len(sides) < 2:
        raise ParseError("gamecenter: need two .teamWrap blocks")

    parsed_sides = [_parse_gamecenter_side(s) for s in sides[:2]]
    return ParsedGamecenter(home=parsed_sides[0], away=parsed_sides[1])


def _parse_gamecenter_side(block: Tag) -> ParsedGamecenterSide:
    team_anchor = block.select_one("a.teamName") or _first_anchor_with_href(block, "/team/")
    if team_anchor is None:
        raise ParseError("gamecenter: side has no team anchor")
    team_id = _id_from_anchor(team_anchor, _TEAM_ID_FROM_HREF)
    if team_id is None:
        raise ParseError("gamecenter: side team_id missing from anchor")
    team_name = team_anchor.get_text(strip=True) or None

    total_node = block.select_one(".teamTotal") or block.select_one(".totalPts")
    total = _parse_float(total_node.get_text(strip=True) if total_node else None)

    entries: list[ParsedRosterEntry] = []
    for tr in block.select(
        "table.tableType-fullRosterStats tbody tr, table.tableType-roster tbody tr"
    ):
        slot_node = tr.select_one(".teamPosition")
        if slot_node is None:
            continue
        slot = slot_node.get_text(strip=True)
        if not slot:
            continue
        is_starter = not (slot.startswith("BN") or slot.startswith("IR"))
        player_anchor = tr.select_one("a.playerName") or _first_player_anchor(tr)
        player_id = _player_id_from_anchor(player_anchor) if player_anchor else None
        pos_node = tr.select_one(".playerPosition")
        team_node = tr.select_one(".playerTeam")
        opp_node = tr.select_one(".playerOpponent")
        status_node = tr.select_one(".playerGameStatus")
        entries.append(
            ParsedRosterEntry(
                roster_slot=slot,
                is_starter=is_starter,
                player_id=player_id,
                player_name=player_anchor.get_text(strip=True) if player_anchor else None,
                position=pos_node.get_text(strip=True) if pos_node else None,
                nfl_team=team_node.get_text(strip=True) if team_node else None,
                opponent=opp_node.get_text(strip=True) if opp_node else None,
                game_status=status_node.get_text(strip=True) if status_node else None,
            )
        )

    return ParsedGamecenterSide(
        team_id=team_id,
        team_name=team_name,
        total_points=total,
        entries=tuple(entries),
    )


__all__ = [
    "ParseError",
    "ParsedAvailability",
    "ParsedAvailabilityPage",
    "ParsedGamecenter",
    "ParsedGamecenterSide",
    "ParsedLeagueHome",
    "ParsedMatchup",
    "ParsedOwner",
    "ParsedRosterEntry",
    "ParsedTeamRoster",
    "ParsedTransaction",
    "parse_availability_page",
    "parse_gamecenter",
    "parse_league_home",
    "parse_owners",
    "parse_settings_scoring",
    "parse_team_roster",
    "parse_transactions",
    "parse_weekly_matchups",
]
