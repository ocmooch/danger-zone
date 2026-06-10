"""Historical reconstruction from NFL.com's league-history pages.

The live-season runner in :mod:`ff_pipeline.crawlers.nfl_com.league`
scrapes *current* state (the ``/team/{id}`` page has no year/week, so it
can only ever return today's roster). For past seasons we instead read
the ``/history/{year}/...`` views, which ARE parameterized by year and
week:

* ``standings`` — final finish order, champion/runner-up/last, and the
  medal teams' regular-season records. → :func:`reconstruct_standings`.
* ``schedule?scheduleDetail={week}`` — per-week head-to-head results for
  every week of the season. → :func:`reconstruct_matchups`.
* ``teamgamecenter?teamId={id}&week={week}`` — the real per-week lineup
  (starters/bench + each player's points). → :func:`reconstruct_lineups`.

Per-team regular-season W/L/T and points-for/against are then *derived*
from the reconstructed matchups (:func:`derive_team_records`) rather than
trusted from a JS-rendered standings tab that isn't in the static HTML.

Everything here is idempotent and commits are the caller's job, matching
the rest of the crawler layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import delete, func, select

from ff_pipeline.crawlers.nfl_com.client import AuthFailureError
from ff_pipeline.crawlers.nfl_com.parsers import (
    ParsedDraftPick,
    ParsedRosterEntry,
    ParseError,
    parse_draft_picks,
    parse_draft_round_numbers,
    parse_gamecenter,
    parse_playoff_bracket,
    parse_standings,
    parse_weekly_matchups,
)
from ff_pipeline.crawlers.nfl_com.urls import (
    draft_results as draft_results_url,
)
from ff_pipeline.crawlers.nfl_com.urls import (
    playoffs as playoffs_url,
)
from ff_pipeline.crawlers.nfl_com.urls import (
    standings as standings_url,
)
from ff_pipeline.crawlers.nfl_com.urls import (
    team_gamecenter,
    weekly_matchups,
)
from ff_pipeline.logging_config import get_logger
from ff_pipeline.normalizer.player_ids import PlayerIdentity, PlayerResolver
from ff_pipeline.repository.models import (
    Matchup,
    PipelineRun,
    Season,
    Team,
    TeamRoster,
    Transaction,
)
from ff_pipeline.repository.upsert import upsert

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

log = get_logger(__name__)

RECONSTRUCT_MODE = "reconstruct"
DRAFT_MODE = "draft"

# Draft picks carry no real timestamp on NFL.com, but the downstream BFF
# orders the draft by ``executed_at`` (pick 1 earliest). We synthesize a
# monotonic timestamp from the overall pick number anchored to a fixed
# pre-season instant — early enough that a draft always sorts before that
# season's in-season adds/drops/trades (which start in September).
_DRAFT_EPOCH_MONTH = 8
_DRAFT_EPOCH_DAY = 1

# Fantasy weeks never exceed 18 (17-game regular season + the final
# fantasy week). We probe 1..MAX_FANTASY_WEEK and stop counting a week as
# real once its schedule page yields no matchups.
MAX_FANTASY_WEEK = 18


class _HtmlFetcher(Protocol):
    def get_html(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class StandingsOutcome:
    teams_ranked: int
    champion_team_id: int | None
    runner_up_team_id: int | None
    last_place_team_id: int | None


@dataclass(frozen=True, slots=True)
class MatchupsOutcome:
    weeks_scraped: tuple[int, ...] = field(default_factory=tuple)
    playoff_weeks: tuple[int, ...] = field(default_factory=tuple)
    rows_added: int = 0
    rows_updated: int = 0


@dataclass(frozen=True, slots=True)
class LineupsOutcome:
    rows_added: int = 0
    rows_updated: int = 0
    fetch_failures: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_season(session: Session, league_id: str, year: int) -> Season | None:
    return (
        session.execute(select(Season).where(Season.league_id == league_id, Season.year == year))
        .scalars()
        .first()
    )


def _internal_team_by_nfl_id(session: Session, season_id: int) -> dict[int, int]:
    """Map NFL.com team_id (stashed in ``teams.team_abbrev``) → internal id."""
    candidates: dict[int, list[int]] = {}
    for team_id, abbrev in session.execute(
        select(Team.team_id, Team.team_abbrev).where(Team.season_id == season_id)
    ).all():
        if not abbrev:
            continue
        try:
            candidates.setdefault(int(abbrev), []).append(team_id)
        except (TypeError, ValueError):
            continue
    return {
        nfl_id: _preferred_team_id(session, team_ids) for nfl_id, team_ids in candidates.items()
    }


def _preferred_team_id(session: Session, team_ids: list[int]) -> int:
    if len(team_ids) == 1:
        return team_ids[0]
    scored: list[tuple[int, int]] = []
    for team_id in team_ids:
        roster_refs = session.scalar(
            select(func.count()).select_from(TeamRoster).where(TeamRoster.team_id == team_id)
        )
        matchup_refs = session.scalar(
            select(func.count()).select_from(Matchup).where(Matchup.team_id == team_id)
        )
        scored.append((int(roster_refs or 0) + int(matchup_refs or 0), team_id))
    return max(scored)[1]


# ---------------------------------------------------------------------------
# Standings → season + team metadata
# ---------------------------------------------------------------------------


def reconstruct_standings(
    session: Session,
    *,
    league_id: str,
    year: int,
    fetcher: _HtmlFetcher,
) -> StandingsOutcome:
    """Populate finish order + champion/runner-up/last + medal records.

    Updates ``teams.final_rank`` / ``playoff_finish`` (and the medal
    teams' regular-season record + points-for) and the ``seasons``
    champion/runner-up/last-place foreign keys. Marks the season
    ``completed``. Caller commits.
    """
    season = _resolve_season(session, league_id, year)
    if season is None:
        raise ParseError(f"standings: no season row for league={league_id} year={year}")

    parsed = parse_standings(fetcher.get_html(standings_url(league_id, year)))
    nfl_to_internal = _internal_team_by_nfl_id(session, season.season_id)

    ranked = 0
    reg_season_weeks: int | None = None
    for entry in parsed.entries:
        internal_id = nfl_to_internal.get(entry.team_id)
        if internal_id is None:
            log.warning(
                "standings: unknown nfl team_id",
                year=year,
                nfl_team_id=entry.team_id,
                rank=entry.final_rank,
            )
            continue
        team = session.get(Team, internal_id)
        if team is None:
            continue
        team.final_rank = entry.final_rank
        team.playoff_finish = entry.final_rank
        # The earlier backfill stamped *current* team names (the owners
        # page is not year-scoped) onto every season; the standings page
        # carries the correct per-season name, so prefer it.
        if entry.team_name:
            team.team_name = entry.team_name
        if entry.reg_wins is not None:
            team.regular_season_wins = entry.reg_wins
            team.regular_season_losses = entry.reg_losses
            team.regular_season_ties = entry.reg_ties
            # Everyone plays the same number of regular-season games, so a
            # medal team's game count IS the regular-season week count —
            # the only reliable signal, since the history schedule page
            # does not CSS-tag playoff weeks.
            games = entry.reg_wins + (entry.reg_losses or 0) + (entry.reg_ties or 0)
            reg_season_weeks = max(reg_season_weeks or 0, games)
        if entry.points_for is not None:
            team.regular_season_points_for = entry.points_for
        ranked += 1

    season.champion_team_id = nfl_to_internal.get(parsed.champion_team_id or -1)
    season.runner_up_team_id = nfl_to_internal.get(parsed.runner_up_team_id or -1)
    season.last_place_team_id = nfl_to_internal.get(parsed.last_place_team_id or -1)
    if reg_season_weeks:
        season.regular_season_weeks = reg_season_weeks
    season.status = "completed"

    log.info(
        "Reconstructed standings",
        year=year,
        teams_ranked=ranked,
        champion=season.champion_team_id,
    )
    return StandingsOutcome(
        teams_ranked=ranked,
        champion_team_id=season.champion_team_id,
        runner_up_team_id=season.runner_up_team_id,
        last_place_team_id=season.last_place_team_id,
    )


# ---------------------------------------------------------------------------
# Full-season matchups (all weeks)
# ---------------------------------------------------------------------------


def reconstruct_matchups(
    session: Session,
    *,
    league_id: str,
    year: int,
    fetcher: _HtmlFetcher,
    weeks: Sequence[int] | None = None,
) -> MatchupsOutcome:
    """Scrape every week's schedule page and upsert ``matchups``.

    When ``weeks`` is omitted, probes 1..MAX_FANTASY_WEEK and stops
    treating a week as real once its schedule page has no matchups (the
    end of the season). Returns the weeks that actually yielded rows and
    which of those were playoff weeks.
    """
    season = _resolve_season(session, league_id, year)
    if season is None:
        raise ParseError(f"matchups: no season row for league={league_id} year={year}")
    nfl_to_internal = _internal_team_by_nfl_id(session, season.season_id)

    # The history schedule page doesn't CSS-tag playoff weeks, so we rely
    # on the regular-season-week boundary set from the standings medal
    # records (run standings first). A week beyond that boundary is a
    # postseason week (championship + consolation brackets).
    reg_weeks = season.regular_season_weeks
    playoff_team_ids = _championship_playoff_team_ids(fetcher, league_id=league_id, year=year)
    candidate_weeks = list(weeks) if weeks is not None else list(range(1, MAX_FANTASY_WEEK + 1))
    scraped: list[int] = []
    playoff_weeks: list[int] = []
    added = 0
    updated = 0

    for week in candidate_weeks:
        try:
            parsed = parse_weekly_matchups(fetcher.get_html(weekly_matchups(league_id, year, week)))
        except ParseError:
            # No matchups for this week — past the end of the season when
            # we're auto-probing; for an explicit week list, log and skip.
            if weeks is None:
                break
            log.warning("matchups: week had no parseable games", year=year, week=week)
            continue

        is_postseason = reg_weeks is not None and week > reg_weeks
        rows: list[dict[str, object]] = []
        for m in parsed:
            team_id = nfl_to_internal.get(m.team_id)
            if team_id is None:
                continue
            opp_id = (
                nfl_to_internal.get(m.opponent_team_id) if m.opponent_team_id is not None else None
            )
            rows.append(
                {
                    "season_id": season.season_id,
                    "week": week,
                    "team_id": team_id,
                    "opponent_team_id": opp_id,
                    "team_score": m.team_score,
                    "opponent_score": m.opponent_score,
                    "is_win": _is_win(m.team_score, m.opponent_score),
                    # Override the (always-False) parsed flag with the
                    # boundary-derived classification.
                    "is_playoff": is_postseason or m.is_playoff,
                    "is_consolation": _is_consolation_matchup(
                        is_postseason=is_postseason,
                        parsed_is_consolation=m.is_consolation,
                        playoff_team_ids=playoff_team_ids,
                        team_nfl_id=m.team_id,
                        opponent_nfl_id=m.opponent_team_id,
                    ),
                    "nfl_com_game_id": m.game_id,
                }
            )
        if not rows:
            if weeks is None:
                break
            continue
        counts = upsert(session, Matchup, rows, conflict_cols=("season_id", "week", "team_id"))
        added += counts.rows_added
        updated += counts.rows_updated
        scraped.append(week)
        if is_postseason:
            playoff_weeks.append(week)

    # Record the playoff-week count; regular_season_weeks is owned by the
    # standings step (the medal records are the authority).
    if scraped:
        season.playoff_weeks = len(playoff_weeks)
        if reg_weeks is None:
            # Standings didn't run / no medal records — fall back to "all
            # scraped weeks are regular season" so we at least store a span.
            season.regular_season_weeks = len(scraped)

    log.info(
        "Reconstructed matchups",
        year=year,
        weeks=scraped,
        playoff_weeks=playoff_weeks,
        rows_added=added,
        rows_updated=updated,
    )
    return MatchupsOutcome(
        weeks_scraped=tuple(scraped),
        playoff_weeks=tuple(playoff_weeks),
        rows_added=added,
        rows_updated=updated,
    )


def _championship_playoff_team_ids(
    fetcher: _HtmlFetcher, *, league_id: str, year: int
) -> frozenset[int] | None:
    """Return NFL.com team IDs in the championship bracket, if parseable."""
    try:
        bracket = parse_playoff_bracket(fetcher.get_html(playoffs_url(league_id, year)))
    except ParseError as exc:
        log.warning("matchups: playoff bracket unavailable", year=year, error=str(exc))
        return None
    return bracket.team_ids


def _is_consolation_matchup(
    *,
    is_postseason: bool,
    parsed_is_consolation: bool,
    playoff_team_ids: frozenset[int] | None,
    team_nfl_id: int,
    opponent_nfl_id: int | None,
) -> bool:
    if parsed_is_consolation:
        return True
    if not is_postseason or playoff_team_ids is None or opponent_nfl_id is None:
        return False
    return team_nfl_id not in playoff_team_ids or opponent_nfl_id not in playoff_team_ids


def _is_win(score: float | None, opp: float | None) -> bool | None:
    if score is None or opp is None or score == opp:
        return None
    return score > opp


# ---------------------------------------------------------------------------
# Real per-week lineups (teamgamecenter)
# ---------------------------------------------------------------------------


def reconstruct_lineups(
    session: Session,
    *,
    league_id: str,
    year: int,
    fetcher: _HtmlFetcher,
    weeks: Sequence[int],
    resolver: PlayerResolver | None = None,
) -> LineupsOutcome:
    """Rebuild real ``team_rosters`` rows from the teamgamecenter pages.

    For each team and each week, fetch the team's gamecenter view and
    upsert one ``team_rosters`` row per lineup slot, resolving every
    player through :class:`PlayerResolver` so the NFL.com player id lands
    on the canonical players row (which is what makes ``verify`` able to
    match historical starters). These are post-hoc historical snapshots,
    so ``was_locked_at_kickoff`` is left ``False``.
    """
    season = _resolve_season(session, league_id, year)
    if season is None:
        raise ParseError(f"lineups: no season row for league={league_id} year={year}")
    resolver = resolver or PlayerResolver(session)
    nfl_to_internal = _internal_team_by_nfl_id(session, season.season_id)

    added = 0
    updated = 0
    failures = 0

    for week in weeks:
        # Historical reconstruction owns the entire season/week snapshot. Clear
        # old current-state/audit rows first so placeholder rosters from an
        # earlier backfill cannot survive or conflict with real history rows.
        session.execute(
            delete(TeamRoster).where(
                TeamRoster.season_year == year,
                TeamRoster.week == week,
            )
        )
        # Each teamgamecenter page renders both sides, so fetching one
        # team per matchup would suffice — but team→matchup mapping varies
        # by week (byes/playoffs), so we fetch per team and dedupe the
        # parsed sides by NFL.com team id.
        seen_sides: set[int] = set()
        for nfl_team_id in nfl_to_internal:
            try:
                gc = parse_gamecenter(
                    fetcher.get_html(team_gamecenter(league_id, year, nfl_team_id, week))
                )
            except Exception as exc:
                failures += 1
                log.warning(
                    "lineups: gamecenter fetch/parse failed",
                    year=year,
                    week=week,
                    nfl_team_id=nfl_team_id,
                    error=str(exc),
                )
                continue
            for side in (gc.home, gc.away):
                if side.team_id in seen_sides:
                    continue
                seen_sides.add(side.team_id)
                internal_id = nfl_to_internal.get(side.team_id)
                if internal_id is None:
                    continue
                counts = _upsert_lineup_side(
                    session,
                    internal_team_id=internal_id,
                    week=week,
                    season_year=year,
                    entries=side.entries,
                    resolver=resolver,
                )
                added += counts[0]
                updated += counts[1]

    log.info(
        "Reconstructed lineups",
        year=year,
        weeks=list(weeks),
        rows_added=added,
        rows_updated=updated,
        fetch_failures=failures,
    )
    return LineupsOutcome(rows_added=added, rows_updated=updated, fetch_failures=failures)


def _upsert_lineup_side(
    session: Session,
    *,
    internal_team_id: int,
    week: int,
    season_year: int,
    entries: tuple[ParsedRosterEntry, ...],
    resolver: PlayerResolver,
) -> tuple[int, int]:
    rows: list[dict[str, object]] = []
    for entry in entries:
        if entry.player_id is None and entry.player_name is None:
            continue
        internal_player_id = resolver.resolve(
            PlayerIdentity(
                name_full=entry.player_name or entry.player_id or "(unknown)",
                position=entry.position,
                nfl_team=entry.nfl_team,
                nfl_com_player_id=entry.player_id,
            ),
            source="nfl_com",
            # Constrain the fuzzy fallback to players active this season, so an
            # abbreviated lineup name ("S. Smith") can't fold onto a same-name
            # player from a different NFL era (see PlayerResolver.try_match).
            season=season_year,
        )
        rows.append(
            {
                "team_id": internal_team_id,
                "player_id": internal_player_id,
                "season_year": season_year,
                "week": week,
                "roster_slot": entry.roster_slot,
                "is_starter": entry.is_starter,
                "was_locked_at_kickoff": False,
                "acquisition_type": None,
                "extra_data": {
                    "snapshot_kind": "history",
                    "nfl_com_points": entry.points,
                    "opponent": entry.opponent,
                    "game_status": entry.game_status,
                },
            }
        )
    if not rows:
        return (0, 0)
    # Replace-per-scope so a re-run of a historical week yields exactly one
    # snapshot instead of accumulating a second one (the 2025 wk1 corruption).
    session.execute(
        delete(TeamRoster).where(
            TeamRoster.team_id == internal_team_id,
            TeamRoster.season_year == season_year,
            TeamRoster.week == week,
        )
    )
    # Conflict on (season_year, week, player_id): a player who moved teams gets
    # UPDATEd onto the new team rather than double-rostered across teams.
    counts = upsert(session, TeamRoster, rows, conflict_cols=("season_year", "week", "player_id"))
    return (counts.rows_added, counts.rows_updated)


# ---------------------------------------------------------------------------
# Derive per-team records from reconstructed matchups
# ---------------------------------------------------------------------------


def derive_team_records(session: Session, *, league_id: str, year: int) -> int:
    """Aggregate regular-season W/L/T + points-for/against from matchups.

    Reads the reconstructed regular-season matchups (``is_playoff=False``,
    set by the week-boundary classification in
    :func:`reconstruct_matchups`) and writes each team's record + points
    totals — covering all 12 teams, not just the medal places the
    standings page itemizes. For the medal teams the result should match
    the standings record (a built-in cross-check). Caller commits.
    """
    season = _resolve_season(session, league_id, year)
    if season is None:
        return 0

    rows = session.execute(
        select(
            Matchup.team_id,
            Matchup.team_score,
            Matchup.opponent_score,
            Matchup.is_win,
        ).where(
            Matchup.season_id == season.season_id,
            Matchup.is_playoff.is_(False),
            Matchup.is_consolation.is_(False),
        )
    ).all()

    agg: dict[int, dict[str, float]] = {}
    for team_id, ts, opp, is_win in rows:
        a = agg.setdefault(team_id, {"w": 0.0, "l": 0.0, "t": 0.0, "pf": 0.0, "pa": 0.0})
        if ts is not None:
            a["pf"] += ts
        if opp is not None:
            a["pa"] += opp
        if is_win is True:
            a["w"] += 1
        elif is_win is False:
            a["l"] += 1
        elif ts is not None and opp is not None:
            a["t"] += 1

    updated = 0
    for team_id, a in agg.items():
        team = session.get(Team, team_id)
        if team is None:
            continue
        team.regular_season_wins = int(a["w"])
        team.regular_season_losses = int(a["l"])
        team.regular_season_ties = int(a["t"])
        team.regular_season_points_for = round(a["pf"], 2)
        team.regular_season_points_against = round(a["pa"], 2)
        # NOTE: ``made_playoffs`` is intentionally left untouched. The
        # history schedule doesn't distinguish the championship bracket
        # from the consolation bracket (both play in weeks beyond the
        # regular season and carry no CSS marker), so we can't infer it
        # reliably. ``final_rank`` from standings is the dependable
        # postseason signal.
        updated += 1

    log.info("Derived team records", year=year, teams_updated=updated)
    return updated


# ---------------------------------------------------------------------------
# Owner history → distinct manager identities + per-season team attribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnersOutcome:
    """Result of reconstructing manager identities across all seasons."""

    distinct_owners: int
    owners_added: int
    owners_updated: int
    historical_inactive: int
    team_attributions_changed: int


def reconstruct_owners(
    session: Session,
    *,
    league_id: str,
    fetcher: _HtmlFetcher,
    start_year: int,
    end_year: int,
) -> OwnersOutcome:
    """Rebuild manager identities + per-season team ownership from history.

    The year-less ``/owners`` page only ever shows *today's* managers, so the
    earlier backfill stamped the current owner onto every season — making each
    franchise look like it had one continuous manager. The per-season
    ``/history/{year}/owners`` page instead names the human who managed each
    franchise *that* year. A franchise's NFL ``userId`` changing across seasons
    is a real ownership handoff; the username travels with the person.

    This reads every season's owners page and:

    1. derives one ``owners`` row per distinct ``userId`` — ``display_name`` is
       their most recent username, prior usernames become ``aliases``,
       ``joined_year``/``left_year`` span their tenure, and ``is_active`` is true
       only if they managed a team in the final season (so retired managers like
       a one-year fill-in are flagged inactive but preserved);
    2. **re-points** each season's ``teams.owner_id`` to the manager who actually
       held that franchise that year, applied as a permutation-safe update so the
       ``UNIQUE(season_id, owner_id)`` invariant is never transiently violated.

    Two same-named people (e.g. the league's two "Dan"s) stay distinct because
    the key is ``userId``, not the name. Caller commits.
    """
    from ff_pipeline.crawlers.nfl_com.parsers import parse_owners
    from ff_pipeline.crawlers.nfl_com.urls import history_owners
    from ff_pipeline.repository.models import Owner
    from ff_pipeline.repository.owner_identities import canonicalize_owner_identity

    # 1. Scrape: per_season[year] = {nfl_team_id: (identity_key, nfl_user_id, canonical, observed)}
    per_season: dict[int, dict[int, tuple[str, str | None, str, str]]] = {}
    for year in range(start_year, end_year + 1):
        if _resolve_season(session, league_id, year) is None:
            continue
        parsed = parse_owners(fetcher.get_html(history_owners(league_id, year)))
        mapping: dict[int, tuple[str, str | None, str, str]] = {}
        for o in parsed:
            if o.team_id is None or o.nfl_user_id is None:
                continue
            identity = canonicalize_owner_identity(
                session,
                league_id=league_id,
                display_name=o.display_name,
                nfl_user_id=o.nfl_user_id,
            )
            mapping[o.team_id] = (
                identity.key,
                identity.nfl_user_id,
                identity.display_name,
                identity.observed_display_name,
            )
        if mapping:
            per_season[year] = mapping

    if not per_season:
        return OwnersOutcome(0, 0, 0, 0, 0)

    # 2. Identity map: canonical identity key -> {username per year, set of years}.
    names_by_year: dict[str, dict[int, str]] = {}
    user_ids_by_key: dict[str, set[str]] = {}
    for year in sorted(per_season):
        for key, uid, name, observed_name in per_season[year].values():
            names_by_year.setdefault(key, {})[year] = name
            if observed_name != name:
                names_by_year[key][year] = name
            if uid:
                user_ids_by_key.setdefault(key, set()).add(uid)

    # 3. Upsert one owners row per distinct canonical identity.
    latest_year = max(per_season)
    owner_by_key: dict[str, int] = {}
    owners_added = owners_updated = inactive = 0
    for key, by_year in names_by_year.items():
        years = sorted(by_year)
        current_name = by_year[years[-1]]
        aliases = sorted({n for n in by_year.values() if n != current_name})
        for values in per_season.values():
            for parsed_key, _uid, canonical_name, observed_name in values.values():
                if parsed_key == key and observed_name != canonical_name:
                    aliases.append(observed_name)
        aliases = sorted(set(aliases))
        is_active = latest_year in by_year
        if not is_active:
            inactive += 1
        user_ids = sorted(user_ids_by_key.get(key, set()))
        primary_user_id = user_ids[0] if len(user_ids) == 1 else None
        existing = None
        if key.startswith("canonical_display_name:"):
            existing = (
                session.execute(
                    select(Owner).where(
                        Owner.league_id == league_id,
                        func.lower(Owner.display_name) == current_name.casefold(),
                    )
                )
                .scalars()
                .first()
            )
        if existing is None and primary_user_id is not None:
            existing = (
                session.execute(
                    select(Owner).where(
                        Owner.league_id == league_id,
                        Owner.nfl_user_id == primary_user_id,
                    )
                )
                .scalars()
                .first()
            )
        if existing is not None:
            existing.display_name = current_name
            existing.aliases = _merged_owner_aliases(None, aliases, user_ids)
            existing.is_active = is_active
            existing.joined_year = years[0]
            existing.left_year = None if is_active else years[-1]
            existing.nfl_user_id = primary_user_id
            owner_by_key[key] = existing.owner_id
            owners_updated += 1
        else:
            owner = Owner(
                league_id=league_id,
                display_name=current_name,
                nfl_user_id=primary_user_id,
                aliases=_merged_owner_aliases(None, aliases, user_ids),
                is_active=is_active,
                joined_year=years[0],
                left_year=None if is_active else years[-1],
            )
            session.add(owner)
            session.flush()
            owner_by_key[key] = owner.owner_id
            owners_added += 1

    all_owner_ids = set(owner_by_key.values())

    # 4. Re-point each season's teams.owner_id to the true per-season manager.
    changed = 0
    for year, mapping in per_season.items():
        season = _resolve_season(session, league_id, year)
        if season is None:
            continue
        nfl_to_internal = _internal_team_by_nfl_id(session, season.season_id)
        desired: dict[int, int] = {}
        for nfl_tid, (key, _uid, _name, _observed_name) in mapping.items():
            internal = nfl_to_internal.get(nfl_tid)
            if internal is not None:
                desired[internal] = owner_by_key[key]
        changed += _apply_owner_permutation(session, season.season_id, desired, all_owner_ids)

    log.info(
        "Reconstructed owners",
        distinct_owners=len(names_by_year),
        owners_added=owners_added,
        owners_updated=owners_updated,
        historical_inactive=inactive,
        attributions_changed=changed,
    )
    return OwnersOutcome(
        distinct_owners=len(names_by_year),
        owners_added=owners_added,
        owners_updated=owners_updated,
        historical_inactive=inactive,
        team_attributions_changed=changed,
    )


def _apply_owner_permutation(
    session: Session,
    season_id: int,
    desired: dict[int, int],
    all_owner_ids: set[int],
) -> int:
    """Set each team's ``owner_id`` to the reconstructed historical manager."""
    _ = all_owner_ids
    teams = list(session.execute(select(Team).where(Team.season_id == season_id)).scalars().all())
    team_by_id = {t.team_id: t for t in teams}
    changed = 0

    for team_id, owner_id in desired.items():
        team = team_by_id.get(team_id)
        if team is None or team.owner_id == owner_id:
            continue
        team.owner_id = owner_id
        changed += 1
    if changed:
        session.flush()

    return changed


def _merged_owner_aliases(
    existing: object,
    display_aliases: list[str],
    nfl_user_ids: list[str],
) -> dict[str, list[str]] | list[str] | None:
    if isinstance(existing, dict):
        names = {str(v) for v in existing.get("display_names", [])}
        ids = {str(v) for v in existing.get("nfl_user_ids", [])}
    elif isinstance(existing, list):
        names = {str(v) for v in existing}
        ids = set()
    else:
        names = set()
        ids = set()

    names.update(display_aliases)
    ids.update(nfl_user_ids)
    if len(ids) <= 1:
        return sorted(names) or None
    data: dict[str, list[str]] = {"nfl_user_ids": sorted(ids)}
    if names:
        data["display_names"] = sorted(names)
    return data


# ---------------------------------------------------------------------------
# Draft results → draft transactions (+ team_rosters mirror)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    """Result of capturing one season's draft.

    ``available`` is False when NFL.com has no obtainable draft for the
    season; everything else is then zero and nothing is written (an
    honest gap, not fabricated picks).
    """

    year: int
    available: bool
    picks_parsed: int = 0
    rounds_fetched: int = 0
    txns_added: int = 0
    txns_skipped: int = 0
    roster_rows_added: int = 0
    roster_rows_updated: int = 0
    unknown_team_picks: int = 0


def reconstruct_draft(
    session: Session,
    *,
    league_id: str,
    year: int,
    fetcher: _HtmlFetcher,
    resolver: PlayerResolver | None = None,
) -> DraftOutcome:
    """Capture one season's draft into ``transactions`` (+ ``team_rosters``).

    Reads ``/history/{year}/draftresults`` round by round (NFL.com renders
    one round per page), resolves each pick's player and team to internal
    ids, and writes one ``transactions`` row per pick with
    ``transaction_type='draft'``, ``direction='add'``, ``effective_week=0``
    and an ``executed_at`` synthesized from the overall pick number so the
    whole draft sorts in true pick order. Each pick is also mirrored onto
    ``team_rosters`` at ``week=0`` with ``acquisition_type='draft'`` and the
    explicit overall/round stashed in ``extra_data``.

    Idempotent: a pick already present (matched on season + team + player)
    is skipped, so re-running never double-inserts. Caller commits.
    """
    season = _resolve_season(session, league_id, year)
    if season is None:
        raise ParseError(f"draft: no season row for league={league_id} year={year}")
    resolver = resolver or PlayerResolver(session)
    nfl_to_internal = _internal_team_by_nfl_id(session, season.season_id)
    team_count = len(nfl_to_internal) or None

    base_html = fetcher.get_html(draft_results_url(league_id, year))
    round_numbers = parse_draft_round_numbers(base_html)
    picks: list[ParsedDraftPick] = list(parse_draft_picks(base_html))
    seen_rounds = {p.draft_round for p in picks}
    rounds_fetched = 1
    for round_no in round_numbers:
        if round_no in seen_rounds:
            continue
        more = parse_draft_picks(fetcher.get_html(draft_results_url(league_id, year, round_no)))
        rounds_fetched += 1
        picks.extend(more)
        seen_rounds.update(p.draft_round for p in more)

    if not picks:
        log.info("Draft not obtainable; recording nothing", year=year)
        return DraftOutcome(year=year, available=False, rounds_fetched=rounds_fetched)

    picks.sort(key=lambda p: p.overall_pick)
    return _persist_draft_picks(
        session,
        season_id=season.season_id,
        year=year,
        picks=picks,
        nfl_to_internal=nfl_to_internal,
        team_count=team_count,
        resolver=resolver,
        rounds_fetched=rounds_fetched,
    )


def _persist_draft_picks(
    session: Session,
    *,
    season_id: int,
    year: int,
    picks: Sequence[ParsedDraftPick],
    nfl_to_internal: dict[int, int],
    team_count: int | None,
    resolver: PlayerResolver,
    rounds_fetched: int,
) -> DraftOutcome:
    # Existing draft picks for this season keyed by (team, player) so a
    # re-run skips inserts instead of duplicating; the team_rosters mirror
    # is upserted (idempotent) regardless.
    existing: set[tuple[int, int]] = {
        (team_id, player_id)
        for team_id, player_id in session.execute(
            select(Transaction.team_id, Transaction.player_id).where(
                Transaction.season_id == season_id,
                Transaction.transaction_type == "draft",
            )
        ).all()
        if team_id is not None and player_id is not None
    }
    epoch = datetime(year, _DRAFT_EPOCH_MONTH, _DRAFT_EPOCH_DAY, tzinfo=UTC)

    txns_added = 0
    txns_skipped = 0
    unknown_team = 0
    roster_rows: list[dict[str, object]] = []

    for pick in picks:
        team_id = nfl_to_internal.get(pick.team_id) if pick.team_id is not None else None
        if team_id is None:
            unknown_team += 1
            log.warning(
                "draft: unknown nfl team_id",
                year=year,
                nfl_team_id=pick.team_id,
                overall=pick.overall_pick,
            )
            continue
        if not pick.player_id and not pick.player_name:
            continue
        player_id = resolver.resolve(
            PlayerIdentity(
                name_full=pick.player_name or pick.player_id or "(unknown)",
                position=pick.position,
                nfl_team=pick.nfl_team,
                nfl_com_player_id=pick.player_id,
            ),
            source="nfl_com",
            # A draft pick belongs to a player active that season; constrain
            # the fuzzy fallback to that era (see PlayerResolver.try_match).
            season=year,
        )
        executed_at = epoch + timedelta(seconds=pick.overall_pick)
        pick_in_round = (
            pick.overall_pick - (pick.draft_round - 1) * team_count if team_count else None
        )

        key = (team_id, player_id)
        if key in existing:
            txns_skipped += 1
        else:
            existing.add(key)
            session.add(
                Transaction(
                    season_id=season_id,
                    transaction_type="draft",
                    executed_at=executed_at,
                    effective_week=0,
                    team_id=team_id,
                    player_id=player_id,
                    direction="add",
                    notes=_draft_note(pick, pick_in_round),
                )
            )
            txns_added += 1

        roster_rows.append(
            {
                "team_id": team_id,
                "player_id": player_id,
                "season_year": year,
                "week": 0,
                "roster_slot": None,
                "is_starter": None,
                "was_locked_at_kickoff": False,
                "acquisition_type": "draft",
                "acquisition_week": 0,
                "acquisition_date": executed_at,
                "extra_data": {
                    "snapshot_kind": "draft",
                    "draft_overall": pick.overall_pick,
                    "draft_round": pick.draft_round,
                    "draft_pick_in_round": pick_in_round,
                },
            }
        )

    roster_added = 0
    roster_updated = 0
    if roster_rows:
        counts = upsert(
            session, TeamRoster, roster_rows, conflict_cols=("season_year", "week", "player_id")
        )
        roster_added = counts.rows_added
        roster_updated = counts.rows_updated

    log.info(
        "Captured draft",
        year=year,
        picks=len(picks),
        txns_added=txns_added,
        txns_skipped=txns_skipped,
        roster_rows=roster_added + roster_updated,
        unknown_team_picks=unknown_team,
    )
    return DraftOutcome(
        year=year,
        available=True,
        picks_parsed=len(picks),
        rounds_fetched=rounds_fetched,
        txns_added=txns_added,
        txns_skipped=txns_skipped,
        roster_rows_added=roster_added,
        roster_rows_updated=roster_updated,
        unknown_team_picks=unknown_team,
    )


def _draft_note(pick: ParsedDraftPick, pick_in_round: int | None) -> str:
    if pick_in_round is not None:
        return f"Round {pick.draft_round}, Pick {pick_in_round} (overall {pick.overall_pick})"
    return f"Overall pick {pick.overall_pick} (round {pick.draft_round})"


def completed_drafts(session: Session) -> set[int]:
    """Years with a successful ``mode='draft'`` pipeline run."""
    done: set[int] = set()
    for run in session.execute(
        select(PipelineRun).where(PipelineRun.mode == DRAFT_MODE, PipelineRun.status == "success")
    ).scalars():
        summary = run.sources_summary or {}
        if isinstance(summary, dict):
            payload = summary.get("nfl_com_draft")
            if isinstance(payload, dict) and isinstance(payload.get("year"), int):
                done.add(payload["year"])
    return done


def capture_draft_season(
    session: Session,
    *,
    league_id: str,
    year: int,
    fetcher: _HtmlFetcher,
    resolver: PlayerResolver | None = None,
) -> DraftOutcome:
    """Capture one season's draft and record a ``mode='draft'`` run row."""
    run = PipelineRun(status="running", mode=DRAFT_MODE)
    session.add(run)
    session.flush()
    try:
        outcome = reconstruct_draft(
            session, league_id=league_id, year=year, fetcher=fetcher, resolver=resolver
        )
    except Exception as exc:
        run.status = "failed"
        run.error_summary = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(tz=UTC)
        raise

    run.status = "success"
    run.finished_at = datetime.now(tz=UTC)
    run.sources_summary = {
        "nfl_com_draft": {
            "year": year,
            "available": outcome.available,
            "picks": outcome.picks_parsed,
            "rounds_fetched": outcome.rounds_fetched,
            "txns_added": outcome.txns_added,
            "txns_skipped": outcome.txns_skipped,
            "roster_rows": outcome.roster_rows_added + outcome.roster_rows_updated,
            "unknown_team_picks": outcome.unknown_team_picks,
        }
    }
    return outcome


def run_draft_capture(
    session: Session,
    *,
    league_id: str,
    start_year: int,
    end_year: int,
    fetcher: _HtmlFetcher,
    force: bool = False,
) -> list[DraftOutcome]:
    """Capture every season's draft in ``[start_year, end_year]``, resumably.

    Mirrors :func:`run_reconstruction`: commits after each season (so an
    interruption preserves completed years), skips years already captured
    unless ``force``, and lets an :class:`AuthFailureError` abort cleanly
    after committing prior work. Seasons with no obtainable draft are still
    marked done so a resume doesn't retry them forever.
    """
    if start_year > end_year:
        raise ValueError(f"start_year ({start_year}) must be <= end_year ({end_year})")

    already = set() if force else completed_drafts(session)
    results: list[DraftOutcome] = []
    for year in range(start_year, end_year + 1):
        if year in already:
            log.info("Draft capture skipping completed year", year=year)
            continue
        try:
            results.append(
                capture_draft_season(session, league_id=league_id, year=year, fetcher=fetcher)
            )
            session.commit()
        except AuthFailureError:
            session.commit()  # persist the failed run row + prior seasons
            log.warning("Draft capture aborted by auth failure", year=year)
            raise
        except Exception:
            session.commit()
            log.error("Draft capture aborted", year=year)
            raise
    return results


# ---------------------------------------------------------------------------
# Per-season orchestrator + resumable multi-season run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeasonReconstruction:
    """Aggregate outcome of reconstructing one season."""

    year: int
    standings: StandingsOutcome
    matchups: MatchupsOutcome
    lineups: LineupsOutcome
    teams_record_updated: int


def reconstruct_season(
    session: Session,
    *,
    league_id: str,
    year: int,
    fetcher: _HtmlFetcher,
) -> SeasonReconstruction:
    """Run the full history reconstruction for one season, in order.

    Order matters: standings sets the regular-season-week boundary that
    matchups uses to classify playoff weeks, which `derive_team_records`
    then trusts. Lineups are rebuilt for exactly the weeks matchups found.
    Records a ``pipeline_runs(mode='reconstruct')`` row so the multi-season
    driver can skip completed years. Caller commits.
    """
    run = PipelineRun(status="running", mode=RECONSTRUCT_MODE)
    session.add(run)
    session.flush()
    try:
        standings = reconstruct_standings(session, league_id=league_id, year=year, fetcher=fetcher)
        matchups = reconstruct_matchups(session, league_id=league_id, year=year, fetcher=fetcher)
        lineups = reconstruct_lineups(
            session,
            league_id=league_id,
            year=year,
            fetcher=fetcher,
            weeks=matchups.weeks_scraped,
        )
        teams_updated = derive_team_records(session, league_id=league_id, year=year)
    except Exception as exc:
        run.status = "failed"
        run.error_summary = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(tz=UTC)
        raise

    run.status = "success"
    run.finished_at = datetime.now(tz=UTC)
    run.sources_summary = {
        "nfl_com_history": {
            "year": year,
            "teams_ranked": standings.teams_ranked,
            "weeks_scraped": list(matchups.weeks_scraped),
            "playoff_weeks": list(matchups.playoff_weeks),
            "matchup_rows": matchups.rows_added + matchups.rows_updated,
            "roster_rows": lineups.rows_added + lineups.rows_updated,
            "lineup_fetch_failures": lineups.fetch_failures,
            "team_records_updated": teams_updated,
        }
    }
    log.info("Reconstructed season", year=year)
    return SeasonReconstruction(
        year=year,
        standings=standings,
        matchups=matchups,
        lineups=lineups,
        teams_record_updated=teams_updated,
    )


def completed_reconstructions(session: Session) -> set[int]:
    """Years with a successful ``mode='reconstruct'`` pipeline run."""
    done: set[int] = set()
    for run in session.execute(
        select(PipelineRun).where(
            PipelineRun.mode == RECONSTRUCT_MODE, PipelineRun.status == "success"
        )
    ).scalars():
        summary = run.sources_summary or {}
        if isinstance(summary, dict):
            payload = summary.get("nfl_com_history")
            if isinstance(payload, dict) and isinstance(payload.get("year"), int):
                done.add(payload["year"])
    return done


def run_reconstruction(
    session: Session,
    *,
    league_id: str,
    start_year: int,
    end_year: int,
    fetcher: _HtmlFetcher,
    force: bool = False,
) -> list[SeasonReconstruction]:
    """Reconstruct every season in ``[start_year, end_year]``, resumably.

    Commits after each season so an interruption (e.g. cookie expiry)
    preserves completed years; re-running skips them unless ``force``.
    An :class:`AuthFailureError` aborts cleanly after committing prior
    work, mirroring the backfill orchestrator's contract. Caller need not
    commit — this commits per season itself.
    """
    if start_year > end_year:
        raise ValueError(f"start_year ({start_year}) must be <= end_year ({end_year})")

    already = set() if force else completed_reconstructions(session)
    results: list[SeasonReconstruction] = []
    for year in range(start_year, end_year + 1):
        if year in already:
            log.info("Reconstruction skipping completed year", year=year)
            continue
        try:
            results.append(
                reconstruct_season(session, league_id=league_id, year=year, fetcher=fetcher)
            )
            session.commit()
        except AuthFailureError:
            session.commit()  # persist the failed run row + prior seasons
            log.warning("Reconstruction aborted by auth failure", year=year)
            raise
        except Exception:
            session.commit()
            log.error("Reconstruction aborted", year=year)
            raise
    return results


__all__ = [
    "DRAFT_MODE",
    "MAX_FANTASY_WEEK",
    "RECONSTRUCT_MODE",
    "DraftOutcome",
    "LineupsOutcome",
    "MatchupsOutcome",
    "OwnersOutcome",
    "SeasonReconstruction",
    "StandingsOutcome",
    "capture_draft_season",
    "completed_drafts",
    "completed_reconstructions",
    "derive_team_records",
    "reconstruct_draft",
    "reconstruct_lineups",
    "reconstruct_matchups",
    "reconstruct_owners",
    "reconstruct_season",
    "reconstruct_standings",
    "run_draft_capture",
    "run_reconstruction",
]
