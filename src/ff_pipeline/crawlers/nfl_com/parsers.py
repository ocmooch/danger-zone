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
    """One slot on a team's roster (one player or an empty bench slot).

    ``points`` is populated only when the row carries a ``.playerTotal``
    span (i.e. gamecenter pages). Regular team-roster pages emit ``None``.
    """

    roster_slot: str
    is_starter: bool
    player_id: str | None
    player_name: str | None
    position: str | None
    nfl_team: str | None
    opponent: str | None
    game_status: str | None
    points: float | None = None


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


@dataclass(frozen=True, slots=True)
class ParsedStandingEntry:
    """One team's final-standings row from /history/{year}/standings.

    ``final_rank`` is the playoff-finish position (1 = champion). The
    regular-season record + ``points_for`` are only rendered for the
    medal places (top 3) in the server-side HTML; for everyone else they
    are ``None`` and the runner derives them from the reconstructed
    ``matchups`` instead.
    """

    final_rank: int
    team_id: int
    team_name: str | None
    owner_name: str | None
    reg_wins: int | None
    reg_losses: int | None
    reg_ties: int | None
    points_for: float | None


@dataclass(frozen=True, slots=True)
class ParsedStandings:
    """Final standings for one season."""

    entries: tuple[ParsedStandingEntry, ...]
    champion_team_id: int | None
    runner_up_team_id: int | None
    last_place_team_id: int | None


@dataclass(frozen=True, slots=True)
class ParsedDraftPick:
    """One pick from /history/{year}/draftresults (By Round view).

    ``overall_pick`` is NFL.com's own global pick number (the ``.count``
    cell renders "13." on the first pick of round 2 of a 12-team draft),
    so it already encodes snake order and is the authority for sequencing
    — the runner does not reconstruct it from round + team count.
    ``draft_round`` comes from the round header; ``team_id`` /
    ``player_id`` are the NFL.com ids the runner resolves to internal rows.
    """

    overall_pick: int
    draft_round: int
    team_id: int | None
    player_id: str | None
    player_name: str | None
    position: str | None
    nfl_team: str | None


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
_ANALYTICS_DATA_RE = re.compile(r"window\.analyticsData\s*=\s*(\{.*?\});\s*\n", re.DOTALL)
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


def _extract_week_and_season(html: str, soup: BeautifulSoup) -> tuple[int | None, int | None]:
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


_TEAM_ID_FROM_HREF = re.compile(r"\bteamId=(\d+)|/team/(\d+)")
_USER_ID_FROM_HREF = re.compile(r"/user/(\d+)")
# Live NFL.com sometimes carries the team id only in a CSS class
# (``teamName teamId-NNN``); the schedule + transactions pages use
# ``teamhome?teamId=NNN`` in the href, the league-home playoff bracket
# uses ``/team/NNN``. Either is acceptable.
_TEAM_ID_FROM_CLASS = re.compile(r"teamId-(\d+)")
# Live NFL.com renders the owner as ``<span class="userName userId-NNN">``
# rather than an anchor; the user id is embedded in the CSS class.
_USER_ID_FROM_CLASS = re.compile(r"userId-(\d+)")


def parse_owners(html: str) -> list[ParsedOwner]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.tableType-team") or soup.select_one("table.tableType-owners")
    if table is None:
        raise ParseError("owners: missing table.tableType-team")

    out: list[ParsedOwner] = []
    for tr in table.select("tbody tr"):
        team_anchor = tr.select_one("a.teamName") or _first_anchor_with_href(tr, "/team/")
        owner_node = tr.select_one("span.userName") or tr.select_one("a.userName")
        if owner_node is None:
            owner_node = _first_anchor_with_href(tr, "/user/")
        if team_anchor is None and owner_node is None:
            continue
        team_id = _id_from_anchor(team_anchor, _TEAM_ID_FROM_HREF) if team_anchor else None
        user_id = _user_id_from_node(owner_node) if owner_node is not None else None
        display_name = owner_node.get_text(strip=True) if owner_node else ""
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


def _user_id_from_node(node: Tag) -> str | None:
    """Extract a user id from a ``<span class="userName userId-NNN">`` or an anchor."""
    href = node.get("href")
    if isinstance(href, str):
        match = _USER_ID_FROM_HREF.search(href)
        if match:
            return match.group(1)
    classes: list[str] | str = node.get("class") or []
    if isinstance(classes, list):
        for cls in classes:
            match = _USER_ID_FROM_CLASS.search(cls)
            if match:
                return match.group(1)
    return None


def _first_anchor_with_href(node: Tag, substring: str) -> Tag | None:
    for a in node.find_all("a", href=True):
        if substring in a["href"]:
            return a
    return None


def _id_from_anchor(anchor: Tag | None, pattern: re.Pattern[str]) -> int | None:
    if anchor is None:
        return None
    href = anchor.get("href", "")
    if isinstance(href, str):
        match = pattern.search(href)
        if match:
            # Some patterns (e.g. _TEAM_ID_FROM_HREF) have alternative
            # capture groups; return the first one that matched.
            for grp in match.groups():
                if grp:
                    return int(grp)
    # Fallback: pull the id from a ``teamId-NNN`` class if available.
    classes: list[str] | str = anchor.get("class") or []
    if pattern is _TEAM_ID_FROM_HREF and isinstance(classes, list):
        for cls in classes:
            class_match = _TEAM_ID_FROM_CLASS.search(cls)
            if class_match:
                return int(class_match.group(1))
    return None


# ---------------------------------------------------------------------------
# Team roster
# ---------------------------------------------------------------------------


_PLAYER_ID_FROM_HREF = re.compile(r"playerId=(\d+)|/player/(\d+)")
# Live NFL.com player anchors carry the id redundantly in a CSS class
# (``playerNameId-NNN``). Used as a last-resort fallback when the href
# is missing or shaped differently in an A/B variant.
_PLAYER_ID_FROM_CLASS = re.compile(r"playerNameId-(\d+)")


_NON_STARTER_SLOTS = ("BN", "IR", "RES")

#: Real NFL.com position codes. The roster/availability tables render a
#: player's position in a bare ``<em>`` ("QB - CIN", or "DEF" alone), but
#: that same ``<em>`` slot is reused for non-position notes when the
#: position cell is empty — inactive players show "Season is Over - Add to
#: Watch List", and an unfilled flex slot can surface its slot label
#: ("R/W/T"). Those leak in as bogus positions, and a bogus position then
#: blocks the normalizer's name+position fuzzy match (it filters candidates
#: by position), spawning a duplicate player row that never merges with the
#: nflverse stats row. We whitelist known codes and treat anything else as
#: "position unknown" (None) so the resolver can still match by name.
_KNOWN_POSITIONS = frozenset(
    {
        # Offense + DST + kicking
        "QB",
        "RB",
        "FB",
        "WR",
        "TE",
        "K",
        "PK",
        "P",
        "DEF",
        "DST",
        # IDP / defensive (availability pages expose these)
        "DL",
        "DE",
        "DT",
        "NT",
        "EDGE",
        "LB",
        "OLB",
        "ILB",
        "MLB",
        "DB",
        "CB",
        "S",
        "SS",
        "FS",
    }
)


def _clean_position(raw: str | None) -> str | None:
    """Return ``raw`` only if it is a recognized NFL position code.

    Whitespace is collapsed and the token upper-cased before the
    whitelist check. Unrecognized text (UI notes, slot labels) becomes
    ``None`` rather than a fake position. See ``_KNOWN_POSITIONS``.
    """
    if raw is None:
        return None
    candidate = re.sub(r"\s+", "", raw).upper()
    return candidate if candidate in _KNOWN_POSITIONS else None


def parse_team_roster(html: str) -> ParsedTeamRoster:
    soup = BeautifulSoup(html, "lxml")

    team_id, team_name = _extract_team_header(soup)
    if team_id is None:
        raise ParseError("team_roster: team_id not extractable from page header")

    tables = soup.select("table.tableType-player") or soup.select("table.tableType-roster")
    if not tables:
        raise ParseError("team_roster: missing table.tableType-player / tableType-roster")

    entries: list[ParsedRosterEntry] = []
    for table in tables:
        for tr in table.select("tbody tr"):
            entry = _parse_roster_row(tr)
            if entry is not None:
                entries.append(entry)

    if not entries:
        raise ParseError("team_roster: roster tables had no parseable rows")

    return ParsedTeamRoster(team_id=team_id, team_name=team_name, entries=tuple(entries))


def _extract_team_header(soup: BeautifulSoup) -> tuple[int | None, str | None]:
    """Read the team id + name from the page header.

    Live NFL.com puts the team name in the team-image anchor's ``<img alt>``
    rather than the anchor's text, and the header doesn't render an
    ``a.teamName`` element. We try ``a.teamImg`` first, then fall back to
    any anchor whose href looks like ``/team/NNN``.
    """
    candidate = soup.select_one("a.teamImg") or soup.select_one(".teamWrap a.teamName")
    if candidate is None:
        candidate = _first_anchor_with_href(soup, "/team/")
    if candidate is None:
        return None, None
    team_id = _id_from_anchor(candidate, _TEAM_ID_FROM_HREF)
    name_text = candidate.get_text(strip=True)
    if not name_text:
        img = candidate.select_one("img[alt]")
        if img is not None:
            alt = img.get("alt")
            if isinstance(alt, str):
                name_text = alt.strip()
    return team_id, name_text or None


def _parse_roster_row(tr: Tag) -> ParsedRosterEntry | None:
    slot_node = tr.select_one(".teamPosition") or tr.select_one("td.teamPosition")
    if slot_node is None:
        return None
    roster_slot = slot_node.get_text(strip=True)
    if not roster_slot:
        return None
    is_starter = not any(roster_slot.startswith(prefix) for prefix in _NON_STARTER_SLOTS)

    player_anchor = tr.select_one("a.playerName") or _first_player_anchor(tr)
    player_id = _player_id_from_anchor(player_anchor) if player_anchor is not None else None
    player_name = player_anchor.get_text(strip=True) if player_anchor else None

    position, nfl_team = _position_and_team_from_row(tr)
    opp_node = tr.select_one(".playerOpponent")
    status_node = tr.select_one(".playerGameStatus")
    points_node = tr.select_one("span.playerTotal")
    points = _parse_float(points_node.get_text(strip=True)) if points_node else None

    return ParsedRosterEntry(
        roster_slot=roster_slot,
        is_starter=is_starter,
        player_id=str(player_id) if player_id is not None else None,
        player_name=player_name,
        position=position,
        nfl_team=nfl_team,
        opponent=opp_node.get_text(strip=True) if opp_node else None,
        game_status=status_node.get_text(strip=True) if status_node else None,
        points=points,
    )


def _position_and_team_from_row(tr: Tag) -> tuple[str | None, str | None]:
    """Pull position + NFL team from a roster row.

    Live NFL.com renders both in a single ``<em>`` (``"QB - CIN"`` for
    offense, ``"DEF"`` alone for team defenses). Fall back to the older
    ``.playerPosition`` / ``.playerTeam`` classes if present.
    """
    pos_node = tr.select_one("em.playerPosition") or tr.select_one(".playerPosition")
    team_node = tr.select_one("em.playerTeam") or tr.select_one(".playerTeam")
    if pos_node is not None or team_node is not None:
        return (
            pos_node.get_text(strip=True) if pos_node else None,
            team_node.get_text(strip=True) if team_node else None,
        )
    em = tr.select_one("em")
    if em is None:
        return None, None
    text = em.get_text(" ", strip=True)
    if " - " in text:
        position, _, nfl_team = text.partition(" - ")
        cleaned = _clean_position(position)
        # An invalid position prefix means the whole "X - Y" cell is a UI
        # note (e.g. "Season is Over - Add to Watch List"), not a
        # position/team line — drop both rather than leak "Y" as a team.
        if cleaned is None:
            return None, None
        return cleaned, nfl_team.strip() or None
    # Bare ``<em>`` — only "DEF"-style position-only cells are real; any
    # other free text (UI notes, slot labels) is not a position.
    return _clean_position(text), None


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
# Standings (league history)
# ---------------------------------------------------------------------------


_PLACE_FROM_CLASS = re.compile(r"\bplace-(\d+)\b")
# "Reg. Season: 8-6-0, 1,765.40 Points For" — wins-losses-ties + points-for.
# Points-for carries comma thousands separators; W/L/T do not.
_REG_SEASON_RE = re.compile(
    r"Reg\.\s*Season:\s*(\d+)-(\d+)-(\d+),\s*([\d,]+(?:\.\d+)?)\s*Points For",
    re.IGNORECASE,
)


def parse_standings(html: str) -> ParsedStandings:
    """Parse /league/{LID}/history/{year}/standings.

    The final standings are a server-rendered ``<li class="place-N">``
    list (1 = champion … 12 = last). Each row carries the team anchor
    (``a.teamName.teamId-N``) and owner; only the medal places also
    render the regular-season record + points-for. The ``.st-standings``
    tab with full per-team records is populated client-side and is not
    present in the static HTML, so per-team W/L/points for non-medal
    teams is left to the matchups-derived rollup.
    """
    soup = BeautifulSoup(html, "lxml")
    items = soup.select('li[class*="place-"]')
    if not items:
        raise ParseError("standings: no li.place-N elements")

    entries: list[ParsedStandingEntry] = []
    for li in items:
        classes: str | list[str] = li.get("class") or []
        rank: int | None = None
        for cls in classes if isinstance(classes, list) else [classes]:
            match = _PLACE_FROM_CLASS.search(cls)
            if match:
                rank = int(match.group(1))
                break
        if rank is None:
            continue
        anchor = li.select_one("a.teamName") or _first_anchor_with_href(li, "teamhome")
        team_id = _id_from_anchor(anchor, _TEAM_ID_FROM_HREF)
        if team_id is None:
            continue
        team_name = anchor.get_text(strip=True) if anchor else None

        owner_name: str | None = None
        wins = losses = ties = None
        points_for: float | None = None
        for em in li.find_all("em"):
            text = em.get_text(" ", strip=True)
            if not text:
                continue
            rec = _REG_SEASON_RE.search(text)
            if rec:
                wins = int(rec.group(1))
                losses = int(rec.group(2))
                ties = int(rec.group(3))
                points_for = float(rec.group(4).replace(",", ""))
            elif owner_name is None:
                owner_name = text

        entries.append(
            ParsedStandingEntry(
                final_rank=rank,
                team_id=team_id,
                team_name=team_name or None,
                owner_name=owner_name,
                reg_wins=wins,
                reg_losses=losses,
                reg_ties=ties,
                points_for=points_for,
            )
        )

    if not entries:
        raise ParseError("standings: no parseable place rows")

    by_rank = {e.final_rank: e.team_id for e in entries}
    last_rank = max(e.final_rank for e in entries)
    return ParsedStandings(
        entries=tuple(sorted(entries, key=lambda e: e.final_rank)),
        champion_team_id=by_rank.get(1),
        runner_up_team_id=by_rank.get(2),
        last_place_team_id=by_rank.get(last_rank),
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


# Map the literal NFL.com transaction-type strings to our enum values.
# Keys are the lowercased text from the ``.transactionType`` cell.
# Add/Waiver Claim is disambiguated downstream by the "From" cell
# ("Free Agents" vs "Waivers"), so both share the "add" entry here.
_TXN_TYPE_MAP = {
    "add": "free_agent_add",
    "added": "free_agent_add",
    "waiver claim": "waiver_add",
    "claimed off waivers": "waiver_add",
    "drop": "drop",
    "dropped": "drop",
    "released": "drop",
    "trade": "trade",
    "traded": "trade",
    "draft pick": "draft",
    "drafted": "draft",
    "ir placement": "ir_placement",
    "moved to ir": "ir_placement",
    "activated from ir": "ir_activation",
    "ir activation": "ir_activation",
}
# "Lineup" (slot reassignment) is captured by the team_rosters table
# instead — it has no place in the transactions log.
_TXN_TYPES_TO_SKIP = {"lineup", "starter swap"}
# Row classes look like ``transaction-{add|drop|roster}-NNN[-N]`` — the
# inner numeric is the NFL.com transaction id (shared across the legs
# of a trade or simultaneous add+drop).
_TXN_ID_FROM_CLASS = re.compile(r"^transaction-[a-z_]+-(\d+)(?:-\d+)?$")
_WAIVERS_TEXT_RE = re.compile(r"waivers?", re.IGNORECASE)
_FREE_AGENT_TEXT_RE = re.compile(r"free\s+agents?", re.IGNORECASE)


def parse_transactions(html: str) -> list[ParsedTransaction]:
    soup = BeautifulSoup(html, "lxml")
    table = (
        soup.select_one("table.tableType-transaction")
        or soup.select_one("table.tableType-rosterTrades")
        or soup.select_one("table.tableType-transactions")
    )
    if table is None:
        raise ParseError("transactions: missing table.tableType-transaction")

    out: list[ParsedTransaction] = []
    for tr in table.select("tbody tr"):
        out.extend(_parse_transaction_row(tr))
    if not out:
        raise ParseError("transactions: tableType-transaction had no parseable rows")
    return out


def _parse_transaction_row(tr: Tag) -> list[ParsedTransaction]:
    """Map one row of the live transactions log to ParsedTransaction(s).

    Real NFL.com history-page row shape (one ``<tr>`` per move):
        Date | Week | Type | Player(s) | From | To | By

    Add / Drop emit a single record; Trade rows emit two records (one
    for each side of the move) and rely on the runner to stitch
    ``counterpart_team_id`` together via the shared NFL.com txn id
    pulled from the row's CSS class. Lineup-change rows are *skipped*
    here — they belong in team_rosters, not transactions.
    """
    type_node = tr.select_one(".transactionType")
    raw_type = (type_node.get_text(strip=True) if type_node else "").lower()
    if not raw_type or raw_type in _TXN_TYPES_TO_SKIP:
        return []
    txn_type = _TXN_TYPE_MAP.get(raw_type) or _fuzzy_txn_type(raw_type)
    if txn_type is None:
        log.warning("Unknown transaction type", raw_type=raw_type)
        return []

    from_node = tr.select_one(".transactionFrom")
    to_node = tr.select_one(".transactionTo")

    # "Add" vs "Waiver Claim" — disambiguate via the From cell text.
    if txn_type == "free_agent_add" and from_node is not None:
        from_text = from_node.get_text(" ", strip=True)
        if _WAIVERS_TEXT_RE.search(from_text):
            txn_type = "waiver_add"

    date_node = tr.select_one(".transactionDate")
    week_node = tr.select_one(".transactionWeek")
    by_node = tr.select_one(".transactionOwner")
    player_node = tr.select_one(".playerNameAndInfo")

    executed_at = date_node.get_text(strip=True) if date_node else None
    effective_week = _parse_int(week_node.get_text(strip=True) if week_node else None)
    notes = by_node.get_text(" ", strip=True) if by_node else None

    player_anchor = (
        player_node.select_one("a.playerName") if player_node is not None else None
    ) or (_first_player_anchor(player_node) if player_node is not None else None)
    player_id = _player_id_from_anchor(player_anchor) if player_anchor else None
    player_name = player_anchor.get_text(strip=True) if player_anchor else None

    from_anchor = from_node.select_one("a.teamName") if from_node is not None else None
    to_anchor = to_node.select_one("a.teamName") if to_node is not None else None
    from_team_id = _id_from_anchor(from_anchor, _TEAM_ID_FROM_HREF) if from_anchor else None
    to_team_id = _id_from_anchor(to_anchor, _TEAM_ID_FROM_HREF) if to_anchor else None
    nfl_txn_id = _txn_id_from_row_classes(tr)

    if txn_type == "trade":
        # Two records, one per side of the move. The runner stitches
        # counterpart_team_id by matching nfl_transaction_id.
        return [
            _make_txn(
                nfl_txn_id,
                "trade",
                executed_at,
                effective_week,
                team_id=from_team_id,
                player_id=player_id,
                player_name=player_name,
                direction="out",
                notes=notes,
            ),
            _make_txn(
                nfl_txn_id,
                "trade",
                executed_at,
                effective_week,
                team_id=to_team_id,
                player_id=player_id,
                player_name=player_name,
                direction="in",
                notes=notes,
            ),
        ]

    # Add / Waiver-add: player goes To a team; team_id comes from the To cell.
    # Drop: player comes From a team; team_id comes from the From cell.
    if txn_type == "drop":
        team_id, direction = from_team_id, "out"
    else:
        team_id, direction = to_team_id, "in"

    return [
        _make_txn(
            nfl_txn_id,
            txn_type,
            executed_at,
            effective_week,
            team_id=team_id,
            player_id=player_id,
            player_name=player_name,
            direction=direction,
            notes=notes,
        )
    ]


def _make_txn(
    nfl_txn_id: str | None,
    txn_type: str,
    executed_at: str | None,
    effective_week: int | None,
    *,
    team_id: int | None,
    player_id: str | None,
    player_name: str | None,
    direction: str | None,
    notes: str | None,
) -> ParsedTransaction:
    return ParsedTransaction(
        nfl_transaction_id=nfl_txn_id,
        transaction_type=txn_type,
        executed_at=executed_at,
        effective_week=effective_week,
        team_id=team_id,
        counterpart_team_id=None,  # filled in by runner for trades
        player_id=player_id,
        player_name=player_name,
        direction=direction,
        notes=notes,
    )


def _txn_id_from_row_classes(tr: Tag) -> str | None:
    classes: list[str] | str = tr.get("class") or []
    if not isinstance(classes, list):
        return None
    for cls in classes:
        match = _TXN_ID_FROM_CLASS.match(cls)
        if match:
            return match.group(1)
    return None


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


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


# Backwards compatibility — _direction_from_text is no longer used by
# the column-based parser, but kept for any callers (and tests) that may
# rely on the text-pattern heuristic for older fixture formats.
def _direction_from_text(text: str) -> str | None:
    lowered = text.lower()
    if "removed" in lowered or "dropped" in lowered or "out:" in lowered:
        return "out"
    if "added" in lowered or "in:" in lowered or "claimed" in lowered:
        return "in"
    return None


# ---------------------------------------------------------------------------
# Draft results
# ---------------------------------------------------------------------------


_ROUND_FROM_TEXT = re.compile(r"Round\s+(\d+)", re.IGNORECASE)
_DRAFT_ROUND_DETAIL_RE = re.compile(r"draftResultsDetail=(\d+)")


def parse_draft_round_numbers(html: str) -> tuple[int, ...]:
    """Round numbers offered by the draft page's round navigation.

    The "By Round" view only renders one round at a time, so the runner
    needs this list to know how many round pages to fetch. Returns an
    empty tuple when the page carries no draft navigation (e.g. a season
    whose draft NFL.com never recorded).
    """
    soup = BeautifulSoup(html, "lxml")
    rounds: list[int] = []
    seen: set[int] = set()
    for anchor in soup.select("div.detailNav a"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        match = _DRAFT_ROUND_DETAIL_RE.search(href)
        if match:
            value = int(match.group(1))
            if value not in seen:
                seen.add(value)
                rounds.append(value)
    return tuple(rounds)


def parse_draft_picks(html: str) -> list[ParsedDraftPick]:
    """Parse every draft pick rendered on one draft-results page.

    Handles both the default page (round 1) and a per-round page; each
    ``div.results div.wrap`` is one round (a header + a list of picks).
    Returns ``[]`` when the page has no draft module or no picks — the
    runner treats that as "no obtainable draft" and records nothing,
    rather than fabricating.
    """
    soup = BeautifulSoup(html, "lxml")
    results = soup.select_one("div.results")
    if results is None:
        return []

    picks: list[ParsedDraftPick] = []
    for wrap in results.select("div.wrap"):
        header = wrap.find("h4")
        round_match = _ROUND_FROM_TEXT.search(header.get_text(" ", strip=True)) if header else None
        if round_match is None:
            continue
        draft_round = int(round_match.group(1))
        roster = wrap.find("ul")
        if roster is None:
            continue
        for li in roster.find_all("li", recursive=False):
            pick = _parse_draft_pick_row(li, draft_round)
            if pick is not None:
                picks.append(pick)
    return picks


def _parse_draft_pick_row(li: Tag, draft_round: int) -> ParsedDraftPick | None:
    overall = _parse_int(_text_or_none(li.select_one("span.count")))
    if overall is None:
        return None

    player_anchor = li.select_one("a.playerName") or _first_player_anchor(li)
    player_id = _player_id_from_anchor(player_anchor) if player_anchor else None
    player_name = player_anchor.get_text(strip=True) if player_anchor else None

    position, nfl_team = _split_position_team(_text_or_none(li.select_one("em")))

    team_anchor = li.select_one("a.teamName")
    team_id = _team_id_from_node(team_anchor) if team_anchor else None

    return ParsedDraftPick(
        overall_pick=overall,
        draft_round=draft_round,
        team_id=team_id,
        player_id=player_id,
        player_name=player_name,
        position=position,
        nfl_team=nfl_team,
    )


def _text_or_none(node: Tag | None) -> str | None:
    return node.get_text(" ", strip=True) if node is not None else None


def _split_position_team(text: str | None) -> tuple[str | None, str | None]:
    """Split an "RB - SF" descriptor into (position, nfl_team)."""
    if not text:
        return (None, None)
    parts = [p.strip() for p in text.split("-", 1)]
    position = _clean_position(parts[0]) if parts else None
    nfl_team = parts[1].upper() if len(parts) > 1 and parts[1] else None
    return (position, nfl_team)


def _team_id_from_node(node: Tag) -> int | None:
    """NFL.com team id from a team anchor's ``teamId-N`` class or href."""
    classes: list[str] | str = node.get("class") or []
    if isinstance(classes, list):
        for cls in classes:
            match = _TEAM_ID_FROM_CLASS.search(cls)
            if match:
                return int(match.group(1))
    return _id_from_anchor(node, _TEAM_ID_FROM_HREF)


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
    table = soup.select_one("table.tableType-player") or soup.select_one(
        "table.tableType-playerStats"
    )
    if table is None:
        raise ParseError("availability: missing table.tableType-player")

    rows: list[ParsedAvailability] = []
    for tr in table.select("tbody tr"):
        parsed = _parse_availability_row(tr)
        if parsed is not None:
            rows.append(parsed)
    if not rows:
        raise ParseError("availability: tableType-player had no parseable rows")

    total_count = _extract_availability_total(soup)
    next_offset = _extract_next_offset(soup)
    return ParsedAvailabilityPage(
        rows=tuple(rows),
        total_count=total_count,
        next_offset=next_offset,
    )


def _parse_availability_row(tr: Tag) -> ParsedAvailability | None:
    info_cell = tr.select_one(".playerNameAndInfo")
    container: Tag = info_cell or tr
    player_anchor = container.select_one("a.playerName") or _first_player_anchor(container)
    if player_anchor is None:
        return None
    player_id = _player_id_from_anchor(player_anchor)
    if player_id is None:
        return None
    player_name = player_anchor.get_text(strip=True)

    position, nfl_team = _availability_position_and_team(container)
    owner_cell = tr.select_one(".playerOwner")
    # Owner anchor must come from the playerOwner cell only — every row
    # contains a watch-list anchor under /league/.../team/<viewer>/...
    # which would otherwise be misread as the player's owning team.
    owner_anchor = owner_cell.select_one("a.teamName") if owner_cell else None
    if owner_anchor is None and owner_cell is not None:
        owner_anchor = _first_anchor_with_href(owner_cell, "/team/")
        if owner_anchor is None:
            owner_anchor = _first_anchor_with_href(owner_cell, "teamId=")
    deadline_node = tr.select_one(".waiverClaimDeadline") or tr.select_one(".claimDate")

    status, owning_team_id = _resolve_availability_status(owner_cell, owner_anchor)
    return ParsedAvailability(
        player_id=player_id,
        player_name=player_name,
        position=position,
        nfl_team=nfl_team,
        status=status,
        owning_team_id=owning_team_id,
        waiver_claim_deadline=deadline_node.get_text(strip=True) if deadline_node else None,
    )


def _availability_position_and_team(container: Tag) -> tuple[str | None, str | None]:
    """Pull position + NFL team out of the player-name cell.

    Live NFL.com renders both in a single ``<em>`` ("QB - KC", "DEF").
    Older fixture variants used ``.playerPosition`` / ``.playerTeam``
    classes; we honour either.
    """
    pos_node = container.select_one("em.playerPosition") or container.select_one(".playerPosition")
    team_node = container.select_one("em.playerTeam") or container.select_one(".playerTeam")
    if pos_node is not None or team_node is not None:
        return (
            pos_node.get_text(strip=True) if pos_node else None,
            team_node.get_text(strip=True) if team_node else None,
        )
    em = container.select_one("em")
    if em is None:
        return None, None
    text = em.get_text(" ", strip=True)
    if " - " in text:
        position, _, nfl_team = text.partition(" - ")
        cleaned = _clean_position(position)
        if cleaned is None:
            return None, None
        return cleaned, nfl_team.strip() or None
    return _clean_position(text), None


def _resolve_availability_status(
    owner_cell: Tag | None, owner_anchor: Tag | None
) -> tuple[str, int | None]:
    """Decide OWNED / FREE_AGENT / ON_WAIVERS for a row.

    Owner anchor wins ("they're rostered → OWNED"). Otherwise we map
    the ``.playerOwner`` text via ``_AVAILABILITY_STATUS_MAP`` and fall
    back to FREE_AGENT when nothing else matches — better to default
    free-agent than to treat unknown text as OWNED.
    """
    if owner_anchor is not None:
        owning_team_id = _id_from_anchor(owner_anchor, _TEAM_ID_FROM_HREF)
        if owning_team_id is not None:
            return "OWNED", owning_team_id
    text = (owner_cell.get_text(strip=True) if owner_cell else "").lower()
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
    """Parse a two-team matchup view (gamecenter or teamgamecenter).

    Live NFL.com lays out the page as several disjoint sibling
    ``.teamWrap-1`` / ``.teamWrap-2`` blocks: the first pair carries the
    team-header anchors and totals, while later pairs hold the roster
    tables. We therefore identify the home / away teams from the first
    pair and *separately* group every ``tableType-player`` table by the
    nearest ``teamWrap-N`` ancestor.
    """
    soup = BeautifulSoup(html, "lxml")
    home_blocks = soup.select(".teamWrap.teamWrap-1")
    away_blocks = soup.select(".teamWrap.teamWrap-2")
    if not home_blocks or not away_blocks:
        # Older /gamecenter view used .gamecenterTeamWrap.
        gc_sides = soup.select(".gamecenterTeamWrap")
        if len(gc_sides) >= 2:
            return ParsedGamecenter(
                home=_parse_gamecenter_side(gc_sides[0], tables_under=[gc_sides[0]]),
                away=_parse_gamecenter_side(gc_sides[1], tables_under=[gc_sides[1]]),
            )
        raise ParseError("gamecenter: missing teamWrap-1 / teamWrap-2 blocks")

    return ParsedGamecenter(
        home=_parse_gamecenter_side(home_blocks[0], tables_under=home_blocks),
        away=_parse_gamecenter_side(away_blocks[0], tables_under=away_blocks),
    )


def _parse_gamecenter_side(header_block: Tag, *, tables_under: list[Tag]) -> ParsedGamecenterSide:
    team_anchor = (
        header_block.select_one("a.teamImg")
        or header_block.select_one("a.teamName")
        or _first_anchor_with_href(header_block, "/team/")
        or _first_anchor_with_href(header_block, "teamhome")
    )
    if team_anchor is None:
        raise ParseError("gamecenter: side has no team anchor")
    team_id = _id_from_anchor(team_anchor, _TEAM_ID_FROM_HREF)
    if team_id is None:
        raise ParseError("gamecenter: side team_id missing from anchor / class")
    team_name = team_anchor.get_text(strip=True) or None
    if not team_name:
        img = team_anchor.select_one("img[alt]")
        if img is not None:
            alt = img.get("alt")
            if isinstance(alt, str):
                team_name = alt.strip() or None

    total_node = header_block.select_one(".teamTotal") or header_block.select_one(".totalPts")
    total = _parse_float(total_node.get_text(strip=True) if total_node else None)

    entries: list[ParsedRosterEntry] = []
    seen_rows: set[int] = set()
    for block in tables_under:
        for tr in block.select("table.tableType-player tbody tr, table.tableType-roster tbody tr"):
            row_key = id(tr)
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            entry = _parse_roster_row(tr)
            if entry is not None:
                entries.append(entry)

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
    "ParsedDraftPick",
    "ParsedGamecenter",
    "ParsedGamecenterSide",
    "ParsedLeagueHome",
    "ParsedMatchup",
    "ParsedOwner",
    "ParsedRosterEntry",
    "ParsedStandingEntry",
    "ParsedStandings",
    "ParsedTeamRoster",
    "ParsedTransaction",
    "parse_availability_page",
    "parse_draft_picks",
    "parse_draft_round_numbers",
    "parse_gamecenter",
    "parse_league_home",
    "parse_owners",
    "parse_settings_scoring",
    "parse_standings",
    "parse_team_roster",
    "parse_transactions",
    "parse_weekly_matchups",
]
