"""Cross-check our scoring engine against NFL.com's stored point totals.

Two modes:

* **Single-player** — given ``(player, season, week)``: load the raw
  stats we have for the player, score them with the engine, scrape
  NFL.com's gamecenter view for the matching matchup, find the player's
  points value, and assert ``|ours - theirs| <= tolerance``.
* **Season sweep** — given ``(season, weeks)``: walk every starter on
  every team in those weeks, build a per-row report, and surface the
  overall pass rate.

The verifier owns *no* business logic that belongs in the engine — it
strictly compares two existing numbers. When it fails, the breakdown
JSON from the engine tells you which category drifted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from ff_pipeline.crawlers.nfl_com.parsers import (
    ParsedGamecenter,
    ParsedRosterEntry,
    parse_gamecenter,
)
from ff_pipeline.crawlers.nfl_com.urls import team_gamecenter
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import (
    Matchup,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rescore import _load_rules

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

    from ff_pipeline.scoring.rules import ScoringRules

log = get_logger(__name__)

# Three canonical "known good" weeks the doc suggests sampling per season.
# Mid-week-1 (light correction risk), mid-season (steady-state), and a
# late regular-season week (before playoffs add the consolation noise).
DEFAULT_SWEEP_WEEKS: tuple[int, ...] = (1, 8, 15)


class _HtmlFetcher(Protocol):
    def get_html(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class VerifyComparison:
    """One ``(player, season, week)`` engine-vs-NFL.com comparison."""

    player_id: int | None
    player_name: str | None
    season_year: int
    week: int
    our_points: float | None
    nfl_com_points: float | None
    delta: float | None
    passed: bool
    note: str | None = None


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Aggregate verifier output."""

    comparisons: tuple[VerifyComparison, ...] = field(default_factory=tuple)
    tolerance: float = 0.1
    #: Set when the sweep produced no comparisons for a structural reason
    #: (season row absent, or no scoring rules loaded for it) rather than
    #: because every row passed. Lets the CLI distinguish "nothing to check"
    #: from "all good" instead of silently reporting total=0.
    note: str | None = None

    @property
    def total(self) -> int:
        return len(self.comparisons)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.comparisons if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.comparisons if not c.passed)


# ---------------------------------------------------------------------------
# Single-player verification
# ---------------------------------------------------------------------------


def verify_player(
    session: Session,
    *,
    league_id: str,
    player_name: str,
    season_year: int,
    week: int,
    fetcher: _HtmlFetcher,
    tolerance: float = 0.1,
) -> VerifyComparison:
    """Verify one ``(player, season, week)`` cell.

    Looks up the player by name (case-insensitive prefix on
    ``name_full``). Loads our scoring rules for the season, applies them
    to the player's raw stats, then scrapes the matchup the player's
    team played that week and pulls the NFL.com per-player point value.
    """

    player = _find_player_by_name(session, player_name)
    if player is None:
        return VerifyComparison(
            player_id=None,
            player_name=player_name,
            season_year=season_year,
            week=week,
            our_points=None,
            nfl_com_points=None,
            delta=None,
            passed=False,
            note=f"player_not_found: {player_name!r}",
        )

    season = (
        session.execute(
            select(Season).where(Season.league_id == league_id, Season.year == season_year)
        )
        .scalars()
        .first()
    )
    if season is None:
        return VerifyComparison(
            player_id=player.player_id,
            player_name=player.name_full,
            season_year=season_year,
            week=week,
            our_points=None,
            nfl_com_points=None,
            delta=None,
            passed=False,
            note=f"season_missing: league_id={league_id} year={season_year}",
        )

    rules = _load_rules(session, season.season_id)
    if not rules.rules:
        return VerifyComparison(
            player_id=player.player_id,
            player_name=player.name_full,
            season_year=season_year,
            week=week,
            our_points=None,
            nfl_com_points=None,
            delta=None,
            passed=False,
            note=f"scoring_rules_missing: season_id={season.season_id}",
        )

    our_points = _our_player_points(
        session, player_id=player.player_id, season_year=season_year, week=week, rules=rules
    )

    nfl_points = _find_nfl_com_points_for_player(
        session,
        league_id=league_id,
        season_id=season.season_id,
        player_id=player.player_id,
        season_year=season_year,
        week=week,
        fetcher=fetcher,
    )

    return _build_comparison(
        player=player,
        season_year=season_year,
        week=week,
        ours=our_points,
        theirs=nfl_points,
        tolerance=tolerance,
    )


# ---------------------------------------------------------------------------
# Season sweep
# ---------------------------------------------------------------------------


def verify_season_sweep(
    session: Session,
    *,
    league_id: str,
    season_year: int,
    fetcher: _HtmlFetcher,
    weeks: Sequence[int] = DEFAULT_SWEEP_WEEKS,
    tolerance: float = 0.1,
) -> VerifyReport:
    """Verify every starter in ``weeks`` for ``season_year``.

    Caches the parsed gamecenter for each ``(team, week)`` so each team
    is fetched at most once per week.
    """

    season = (
        session.execute(
            select(Season).where(Season.league_id == league_id, Season.year == season_year)
        )
        .scalars()
        .first()
    )
    if season is None:
        return VerifyReport(
            comparisons=(),
            tolerance=tolerance,
            note=f"season_not_found: league_id={league_id} year={season_year}",
        )

    rules = _load_rules(session, season.season_id)
    if not rules.rules:
        return VerifyReport(
            comparisons=(),
            tolerance=tolerance,
            note=(
                f"scoring_rules_missing: season_id={season.season_id} year={season_year} "
                "(load via `ff-pipeline scoring load`)"
            ),
        )

    teams = list(
        session.execute(select(Team).where(Team.season_id == season.season_id)).scalars().all()
    )
    nfl_team_id_by_internal = _nfl_team_id_lookup(teams)

    comparisons: list[VerifyComparison] = []
    for week in weeks:
        gamecenter_by_team: dict[int, ParsedGamecenter] = {}
        for team in teams:
            nfl_team_id = nfl_team_id_by_internal.get(team.team_id)
            if nfl_team_id is None:
                continue
            if nfl_team_id in gamecenter_by_team:
                continue
            try:
                html = fetcher.get_html(team_gamecenter(league_id, season_year, nfl_team_id, week))
            except Exception as exc:
                log.warning(
                    "verify: gamecenter fetch failed",
                    team_id=team.team_id,
                    week=week,
                    error=str(exc),
                )
                continue
            try:
                gc = parse_gamecenter(html)
            except Exception as exc:
                log.warning(
                    "verify: gamecenter parse failed",
                    team_id=team.team_id,
                    week=week,
                    error=str(exc),
                )
                continue
            gamecenter_by_team[nfl_team_id] = gc

        # One pass per side per matchup, comparing per-starter points.
        seen_nfl_team_ids: set[int] = set()
        for gc in gamecenter_by_team.values():
            for side in (gc.home, gc.away):
                if side.team_id in seen_nfl_team_ids:
                    continue
                seen_nfl_team_ids.add(side.team_id)
                comparisons.extend(
                    _compare_side(
                        session,
                        side_entries=side.entries,
                        season=season,
                        week=week,
                        rules=rules,
                        tolerance=tolerance,
                    )
                )

    return VerifyReport(comparisons=tuple(comparisons), tolerance=tolerance)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_player_by_name(session: Session, name: str) -> Player | None:
    stmt = select(Player).where(Player.name_full.ilike(name)).order_by(Player.player_id).limit(1)
    found = session.execute(stmt).scalars().first()
    if found is not None:
        return found
    # Fall back to substring match — handles "Marvin Mims" vs
    # "Marvin Mims Jr." that the resolver's overrides may or may not have
    # collapsed.
    like = f"%{name}%"
    stmt = select(Player).where(Player.name_full.ilike(like)).order_by(Player.player_id).limit(1)
    return session.execute(stmt).scalars().first()


def _our_player_points(
    session: Session,
    *,
    player_id: int,
    season_year: int,
    week: int,
    rules: ScoringRules,
) -> float | None:
    """Apply the season's rules to whatever raw row we have for this player.

    Prefers the ``nflverse`` row (canonical performance data); falls back
    to whatever ``is_primary=True`` row exists. Returns ``None`` if no
    raw stats are on file.
    """
    rows = list(
        session.execute(
            select(PlayerStatsRaw).where(
                PlayerStatsRaw.player_id == player_id,
                PlayerStatsRaw.season_year == season_year,
                PlayerStatsRaw.week == week,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    chosen = next((r for r in rows if r.source == "nflverse"), None) or next(
        (r for r in rows if r.is_primary), rows[0]
    )
    stats = chosen.stats or {}
    if not isinstance(stats, dict):
        return None
    numeric_stats = {k: float(v) for k, v in stats.items() if isinstance(v, int | float)}
    return apply_rules(numeric_stats, rules).total_points


def _find_nfl_com_points_for_player(
    session: Session,
    *,
    league_id: str,
    season_id: int,
    player_id: int,
    season_year: int,
    week: int,
    fetcher: _HtmlFetcher,
) -> float | None:
    """Walk the matchups for the season+week and pull the player's points.

    Fetches the team-gamecenter page for each team that played that
    week (until the player's row is found, then stops).
    """
    player = session.get(Player, player_id)
    if player is None or not player.nfl_com_player_id:
        return None
    target_nfl_player_id = player.nfl_com_player_id
    matchups = list(
        session.execute(select(Matchup).where(Matchup.season_id == season_id, Matchup.week == week))
        .scalars()
        .all()
    )
    teams = {
        team.team_id: team
        for team in session.execute(select(Team).where(Team.season_id == season_id)).scalars().all()
    }
    nfl_team_id_by_internal = _nfl_team_id_lookup(list(teams.values()))

    visited: set[int] = set()
    for m in matchups:
        for side_team_id in (m.team_id, m.opponent_team_id):
            if side_team_id is None:
                continue
            nfl_team_id = nfl_team_id_by_internal.get(side_team_id)
            if nfl_team_id is None or nfl_team_id in visited:
                continue
            visited.add(nfl_team_id)
            try:
                html = fetcher.get_html(team_gamecenter(league_id, season_year, nfl_team_id, week))
                gc = parse_gamecenter(html)
            except Exception as exc:
                log.warning(
                    "verify: gamecenter fetch/parse failed",
                    team_id=side_team_id,
                    week=week,
                    error=str(exc),
                )
                continue
            for side in (gc.home, gc.away):
                for entry in side.entries:
                    if entry.player_id == target_nfl_player_id and entry.points is not None:
                        return entry.points
    return None


def _compare_side(
    session: Session,
    *,
    side_entries: tuple[ParsedRosterEntry, ...],
    season: Season,
    week: int,
    rules: ScoringRules,
    tolerance: float,
) -> Iterable[VerifyComparison]:
    """Compare every starter on one side of a gamecenter view."""

    # Look up every starter's internal player_id in a single query.
    nfl_com_player_ids = [e.player_id for e in side_entries if e.is_starter and e.player_id]
    if not nfl_com_player_ids:
        return []
    player_rows = list(
        session.execute(select(Player).where(Player.nfl_com_player_id.in_(nfl_com_player_ids)))
        .scalars()
        .all()
    )
    by_nfl_id = {p.nfl_com_player_id: p for p in player_rows if p.nfl_com_player_id}

    comparisons: list[VerifyComparison] = []
    for entry in side_entries:
        if not entry.is_starter or entry.points is None:
            continue
        player = by_nfl_id.get(entry.player_id) if entry.player_id else None
        if player is None:
            comparisons.append(
                VerifyComparison(
                    player_id=None,
                    player_name=entry.player_name,
                    season_year=season.year,
                    week=week,
                    our_points=None,
                    nfl_com_points=entry.points,
                    delta=None,
                    passed=False,
                    note="player_not_in_db",
                )
            )
            continue
        our_points = _our_player_points(
            session,
            player_id=player.player_id,
            season_year=season.year,
            week=week,
            rules=rules,
        )
        comparisons.append(
            _build_comparison(
                player=player,
                season_year=season.year,
                week=week,
                ours=our_points,
                theirs=entry.points,
                tolerance=tolerance,
            )
        )
    return comparisons


def _build_comparison(
    *,
    player: Player,
    season_year: int,
    week: int,
    ours: float | None,
    theirs: float | None,
    tolerance: float,
) -> VerifyComparison:
    if ours is None and theirs is None:
        return VerifyComparison(
            player_id=player.player_id,
            player_name=player.name_full,
            season_year=season_year,
            week=week,
            our_points=None,
            nfl_com_points=None,
            delta=None,
            passed=False,
            note="no_data_on_either_side",
        )
    if ours is None:
        return VerifyComparison(
            player_id=player.player_id,
            player_name=player.name_full,
            season_year=season_year,
            week=week,
            our_points=None,
            nfl_com_points=theirs,
            delta=None,
            passed=False,
            note="our_raw_stats_missing",
        )
    if theirs is None:
        return VerifyComparison(
            player_id=player.player_id,
            player_name=player.name_full,
            season_year=season_year,
            week=week,
            our_points=ours,
            nfl_com_points=None,
            delta=None,
            passed=False,
            note="nfl_com_points_missing",
        )
    delta = ours - theirs
    passed = abs(delta) <= tolerance
    return VerifyComparison(
        player_id=player.player_id,
        player_name=player.name_full,
        season_year=season_year,
        week=week,
        our_points=ours,
        nfl_com_points=theirs,
        delta=delta,
        passed=passed,
        note=None,
    )


# ---------------------------------------------------------------------------
# Team-total reconciliation (DST drift detection)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeamTotalComparison:
    """One ``(team, week)`` engine-total-vs-NFL.com-total comparison.

    ``our_total`` is the sum of *our* scored points across the team's
    starters that week; ``nfl_com_total`` is the authoritative
    ``matchups.team_score`` NFL.com recorded. ``starters_missing_score``
    counts starters we have no scored row for — the usual cause of a
    pre-DST-fix shortfall, surfaced so a flagged delta is explainable.
    """

    team_id: int
    team_name: str | None
    season_year: int
    week: int
    our_total: float | None
    nfl_com_total: float | None
    delta: float | None
    passed: bool
    starters_counted: int
    starters_missing_score: int
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Aggregate team-total reconciliation output (mirrors VerifyReport)."""

    comparisons: tuple[TeamTotalComparison, ...] = field(default_factory=tuple)
    tolerance: float = 0.1
    note: str | None = None

    @property
    def total(self) -> int:
        return len(self.comparisons)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.comparisons if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.comparisons if not c.passed)


def reconcile_team_totals(
    session: Session,
    *,
    league_id: str,
    season_year: int,
    weeks: Sequence[int] | None = None,
    tolerance: float = 0.1,
) -> ReconcileReport:
    """Reconcile each team's summed scored starters against its NFL.com total.

    The engine stays the source of truth: this compares the sum of our
    ``player_stats_scored`` rows over each team's **starters** with the
    authoritative ``matchups.team_score`` and flags any delta beyond
    ``tolerance`` as a data-quality alert. It never patches a score — it
    reports. Runs fully offline (no NFL.com fetch).

    ``weeks=None`` reconciles every week present in ``matchups`` for the
    season. A starter with no scored row is counted in
    ``starters_missing_score`` and contributes 0, so a DST gap (or any
    unscored starter) shows up as a shortfall rather than silently passing.
    """

    season = (
        session.execute(
            select(Season).where(Season.league_id == league_id, Season.year == season_year)
        )
        .scalars()
        .first()
    )
    if season is None:
        return ReconcileReport(
            comparisons=(),
            tolerance=tolerance,
            note=f"season_not_found: league_id={league_id} year={season_year}",
        )

    team_names = {
        t.team_id: t.team_name
        for t in session.execute(select(Team).where(Team.season_id == season.season_id))
        .scalars()
        .all()
    }

    matchup_stmt = select(Matchup).where(Matchup.season_id == season.season_id)
    if weeks is not None:
        matchup_stmt = matchup_stmt.where(Matchup.week.in_(list(weeks)))
    matchups = list(session.execute(matchup_stmt).scalars().all())
    if not matchups:
        return ReconcileReport(
            comparisons=(),
            tolerance=tolerance,
            note=f"no_matchups: season_id={season.season_id} year={season_year}",
        )

    comparisons: list[TeamTotalComparison] = []
    for m in matchups:
        our_total, counted, missing = _scored_starters_total(
            session,
            season_id=season.season_id,
            team_id=m.team_id,
            season_year=season_year,
            week=m.week,
        )
        comparisons.append(
            _build_team_total_comparison(
                team_id=m.team_id,
                team_name=team_names.get(m.team_id),
                season_year=season_year,
                week=m.week,
                our_total=our_total,
                nfl_com_total=m.team_score,
                counted=counted,
                missing=missing,
                tolerance=tolerance,
            )
        )

    return ReconcileReport(comparisons=tuple(comparisons), tolerance=tolerance)


def _scored_starters_total(
    session: Session,
    *,
    season_id: int,
    team_id: int,
    season_year: int,
    week: int,
) -> tuple[float | None, int, int]:
    """Return ``(summed_points, starters_counted, starters_missing_score)``.

    ``summed_points`` is ``None`` when the team has no starters recorded for
    the week (can't reconcile); otherwise it's the sum over starters that
    *do* have a scored row. Starters without one are counted as missing.
    """
    starter_ids = list(
        session.execute(
            select(TeamRoster.player_id).where(
                TeamRoster.team_id == team_id,
                TeamRoster.season_year == season_year,
                TeamRoster.week == week,
                TeamRoster.is_starter.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if not starter_ids:
        return None, 0, 0

    scored: dict[int, float | None] = {}
    for pid, pts in session.execute(
        select(PlayerStatsScored.player_id, PlayerStatsScored.total_points).where(
            PlayerStatsScored.season_id == season_id,
            PlayerStatsScored.week == week,
            PlayerStatsScored.player_id.in_(starter_ids),
        )
    ).all():
        scored[pid] = pts
    total = 0.0
    missing = 0
    for pid in starter_ids:
        pts = scored.get(pid)
        if pts is None:
            missing += 1
        else:
            total += float(pts)
    return round(total, 2), len(starter_ids), missing


def _build_team_total_comparison(
    *,
    team_id: int,
    team_name: str | None,
    season_year: int,
    week: int,
    our_total: float | None,
    nfl_com_total: float | None,
    counted: int,
    missing: int,
    tolerance: float,
) -> TeamTotalComparison:
    if our_total is None:
        note = "no_starters_recorded"
    elif nfl_com_total is None:
        note = "nfl_com_total_missing"
    else:
        note = None
    if our_total is None or nfl_com_total is None:
        return TeamTotalComparison(
            team_id=team_id,
            team_name=team_name,
            season_year=season_year,
            week=week,
            our_total=our_total,
            nfl_com_total=nfl_com_total,
            delta=None,
            passed=False,
            starters_counted=counted,
            starters_missing_score=missing,
            note=note,
        )
    delta = round(our_total - nfl_com_total, 2)
    return TeamTotalComparison(
        team_id=team_id,
        team_name=team_name,
        season_year=season_year,
        week=week,
        our_total=our_total,
        nfl_com_total=nfl_com_total,
        delta=delta,
        passed=abs(delta) <= tolerance,
        starters_counted=counted,
        starters_missing_score=missing,
        note=None,
    )


def _nfl_team_id_lookup(teams: Sequence[Team]) -> dict[int, int]:
    """Map ``teams.team_id`` (internal) → ``team_abbrev`` parsed as int.

    The NFL.com runner stashes the NFL.com team_id in ``team_abbrev``;
    we walk that back here. Teams without a parseable abbrev are dropped.
    """
    out: dict[int, int] = {}
    for t in teams:
        if not t.team_abbrev:
            continue
        try:
            out[t.team_id] = int(t.team_abbrev)
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "DEFAULT_SWEEP_WEEKS",
    "ReconcileReport",
    "TeamTotalComparison",
    "VerifyComparison",
    "VerifyReport",
    "reconcile_team_totals",
    "verify_player",
    "verify_season_sweep",
]
