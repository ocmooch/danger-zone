"""2022 Week-17 Bills@Bengals no-contest championship resolution.

The NFL Week-17 Bills@Bengals game (Jan 2 2023) was suspended after Damar
Hamlin's cardiac arrest and ruled a **no-contest** — never resumed, never
rescheduled. nflverse *voided* the game from its weekly rollup, so the affected
BUF/CIN starters have **no 2022-wk17 player-stats row**; NFL.com stamped them
``0.0``, which left the fantasy title game reading CMC (160) 89.0 def. Smokin
Doubs (165) 74.9 and recorded CMC as the 2022 champion.

The league resolved the no-contest by, for each affected player, taking the
stats they accrued in the suspended Week-17 game **before play stopped** PLUS
their NFL **Week-19** (Wild Card) game — ``final = wk17_partial + wk19``. Week
18 was deliberately skipped. The cancelled game's partial play is *included*,
not discarded.

This module encodes that resolution as a deterministic override that re-applies
on every ingest/score (so a re-scrape never reverts it), mirroring the
relocation/DST resolver precedent (``crawlers/nflverse/franchises.py``).

**Sourcing the Week-17 partial.** nflverse play-by-play has fully voided the
no-contest game (``2022_17_BUF_CIN`` returns zero rows; only the divisional
rematch ``2022_20_CIN_BUF`` survives), so the partial is a **box-score
reconstruction** from the publicly attributable plays before the 5:58-Q1
suspension (Bengals 7, Bills 3): Burrow 4/4 52 yds + 14-yd TD to Boyd; Boyd
1/14/TD; Higgins 1/13 (the Hamlin-tackle play); Allen 3/6 36 yds; Allen→Diggs
17 yds; Bass 25-yd FG. Players with no individually attributable Week-17 play
(Chase, Gabe Davis, Mixon, et al.) carry an empty partial — honest, and the
title outcome is insensitive to it (Doubs wins by ~38 regardless). Each partial
is scored through the pure scoring engine, never copied from nflverse weekly
scored values.

The **affected set is derived from public data** (rostered in 2022 wk17, no
wk17 stat row, but a wk19 row on BUF/CIN), never hardcoded as a list. The
per-player Week-17 partial reconstruction below is the only hardcoded input.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import (
    Matchup,
    PlayerStatsRaw,
    ScoringRule,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rules import ScoringRule as ScoringRuleDataclass
from ff_pipeline.scoring.rules import ScoringRules

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session

log = get_logger(__name__)

SEASON_YEAR = 2022
NO_CONTEST_WEEK = 17
WILD_CARD_WEEK = 19
SUBSTITUTE_BASIS = "no_contest_wk17partial_plus_wk19"

# Box-score reconstruction of the ~9 minutes played before the no-contest was
# suspended (nflverse voided the game, so this is the public box score, not
# play-by-play). Only individually attributable plays are credited; receivers
# with no reported Week-17 reception carry an empty partial. Keyed on our
# ``players.player_id``. Scored through the live scoring engine, not hardcoded
# point totals.
WK17_PARTIAL_RAW_STATS: dict[int, dict[str, float]] = {
    4236: {"passing_yards": 52.0, "passing_tds": 1.0},  # Joe Burrow 4/4, 14-yd TD to Boyd
    3291: {"receptions": 1.0, "receiving_yards": 14.0, "receiving_tds": 1.0},  # Tyler Boyd TD
    10930: {"receptions": 1.0, "receiving_yards": 13.0},  # Tee Higgins (Hamlin-tackle play)
    1413: {"passing_yards": 36.0},  # Josh Allen 3/6, 36 yds
    6770: {"receptions": 1.0, "receiving_yards": 17.0},  # Stefon Diggs (from Allen)
    2331: {"field_goal_made_20_29": 1.0},  # Tyler Bass 25-yd FG
}


@dataclass(slots=True)
class HamlinOverrideResult:
    """Summary of what the override changed, surfaced to the CLI."""

    applied: bool = False
    reason: str = ""
    slots_written: int = 0
    matchups_recomputed: int = 0
    champion_team_id: int | None = None
    runner_up_team_id: int | None = None
    standings_swapped: tuple[int, int] | None = None
    unexpected_flips: list[str] = field(default_factory=list)


def apply_hamlin_2022_wk17_override(
    session: Session, *, league_id: str | None = None
) -> HamlinOverrideResult:
    """Apply the 2022 no-contest resolution to ``session`` (idempotent).

    Writes the ``hamlin_substitute`` provenance contract + corrected
    ``nfl_com_points`` onto every affected wk17 roster slot, recomputes the
    affected matchup scores from corrected starter sums, and re-derives the
    championship / runner-up / final ranks. Safe to call when the 2022 season
    is absent (no-ops) and safe to re-run (recomputes the same result).

    Does **not** fabricate ``player_stats_raw`` / ``player_stats_scored`` rows
    for the voided wk17 game — the substitution lives at the roster/matchup/
    season layer, flagged with provenance.
    """

    season = _season_for_year(session, year=SEASON_YEAR, league_id=league_id)
    if season is None:
        return HamlinOverrideResult(applied=False, reason="2022 season not present")

    rules = _load_rules(session, season.season_id)
    if not rules.rules:
        log.warning(
            "Hamlin override: no scoring rules for 2022; skipping", season_id=season.season_id
        )
        return HamlinOverrideResult(applied=False, reason="no scoring rules for 2022")

    affected = _affected_player_ids(session)
    if not affected:
        return HamlinOverrideResult(applied=False, reason="no affected players found")

    # 1. Per-player substitute scores (wk17 partial + wk19), scored via the engine.
    substitutes = {
        player_id: _build_substitute(session, player_id=player_id, rules=rules)
        for player_id in affected
    }

    # 2. Write provenance + corrected nfl_com_points onto every affected wk17 slot.
    slots_written = _write_roster_provenance(session, substitutes=substitutes)

    # 3. Recompute affected matchup scores from corrected starter sums.
    matchups, flips = _recompute_matchups(
        session, season_id=season.season_id, affected=set(affected)
    )

    # 4. Re-derive championship / runner-up / final ranks from the title game.
    champion_id, runner_up_id, swapped = _rederive_standings(session, season=season)

    result = HamlinOverrideResult(
        applied=True,
        reason="ok",
        slots_written=slots_written,
        matchups_recomputed=matchups,
        champion_team_id=champion_id,
        runner_up_team_id=runner_up_id,
        standings_swapped=swapped,
        unexpected_flips=flips,
    )
    log.info(
        "Hamlin 2022 wk17 override applied",
        slots_written=slots_written,
        matchups_recomputed=matchups,
        champion_team_id=champion_id,
        runner_up_team_id=runner_up_id,
        unexpected_flips=flips,
    )
    return result


# ---------------------------------------------------------------------------
# Affected-set derivation (public data, not a hardcoded list)
# ---------------------------------------------------------------------------


def _affected_player_ids(session: Session) -> list[int]:
    """Players rostered in 2022 wk17 whose wk17 game was the no-contest.

    Authoritative rule: rostered in 2022 wk17, **no** ``player_stats_raw`` row
    for 2022 wk17, but **has** a 2022 wk19 row on BUF or CIN. This excludes Zack
    Moss (real wk17 Colts row) and includes the players the recovered ledger
    omitted.
    """

    has_wk17 = (
        select(PlayerStatsRaw.player_id)
        .where(
            PlayerStatsRaw.player_id == TeamRoster.player_id,
            PlayerStatsRaw.season_year == SEASON_YEAR,
            PlayerStatsRaw.week == NO_CONTEST_WEEK,
        )
        .exists()
    )
    has_wk19_buf_cin = (
        select(PlayerStatsRaw.player_id)
        .where(
            PlayerStatsRaw.player_id == TeamRoster.player_id,
            PlayerStatsRaw.season_year == SEASON_YEAR,
            PlayerStatsRaw.week == WILD_CARD_WEEK,
            PlayerStatsRaw.nfl_team.in_(("BUF", "CIN")),
        )
        .exists()
    )
    stmt = (
        select(TeamRoster.player_id)
        .where(
            TeamRoster.season_year == SEASON_YEAR,
            TeamRoster.week == NO_CONTEST_WEEK,
            ~has_wk17,
            has_wk19_buf_cin,
        )
        .distinct()
    )
    return sorted({pid for (pid,) in session.execute(stmt).all()})


# ---------------------------------------------------------------------------
# Per-player substitute scoring
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Substitute:
    league_points: float
    wk17_partial_raw: dict[str, float]
    wk17_partial_points: float
    wk19_raw: dict[str, float]
    wk19_points: float
    points_breakdown: dict[str, float]


def _build_substitute(session: Session, *, player_id: int, rules: ScoringRules) -> _Substitute:
    """Combine the wk17 partial + wk19 raw lines and score each via the engine.

    ``league_points`` is the sum of the two separately-scored components —
    matching the league ruling ("partial PLUS the Wild Card week"), which is the
    correct semantics: two distinct games never combine to earn a single-game
    yardage bonus.
    """

    wk17_raw = WK17_PARTIAL_RAW_STATS.get(player_id, {})
    wk19_raw = _wk19_raw_stats(session, player_id=player_id)

    wk17_scored = apply_rules(wk17_raw, rules)
    wk19_scored = apply_rules(wk19_raw, rules)

    breakdown: defaultdict[str, float] = defaultdict(float)
    for category, value in wk17_scored.breakdown.items():
        breakdown[category] += value
    for category, value in wk19_scored.breakdown.items():
        breakdown[category] += value

    league_points = round(wk17_scored.total_points + wk19_scored.total_points, 2)
    return _Substitute(
        league_points=league_points,
        # Store only the stat keys that actually scored — zero-valued nflverse
        # keys add no information and would bloat every affected roster row.
        wk17_partial_raw={k: v for k, v in wk17_raw.items() if v},
        wk17_partial_points=wk17_scored.total_points,
        wk19_raw={k: v for k, v in wk19_raw.items() if v},
        wk19_points=wk19_scored.total_points,
        points_breakdown={k: round(v, 2) for k, v in breakdown.items()},
    )


def _wk19_raw_stats(session: Session, *, player_id: int) -> dict[str, float]:
    row = session.execute(
        select(PlayerStatsRaw.stats).where(
            PlayerStatsRaw.player_id == player_id,
            PlayerStatsRaw.season_year == SEASON_YEAR,
            PlayerStatsRaw.week == WILD_CARD_WEEK,
            PlayerStatsRaw.source == "nflverse",
        )
    ).first()
    if row is None or not isinstance(row.stats, dict):
        return {}
    return {
        k: float(v)
        for k, v in row.stats.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


# ---------------------------------------------------------------------------
# Roster provenance
# ---------------------------------------------------------------------------


def _write_roster_provenance(session: Session, *, substitutes: Mapping[int, _Substitute]) -> int:
    """Write the ``hamlin_substitute`` contract + corrected ``nfl_com_points``
    onto every affected wk17 roster slot (starter and bench alike)."""

    rosters = list(
        session.execute(
            select(TeamRoster).where(
                TeamRoster.season_year == SEASON_YEAR,
                TeamRoster.week == NO_CONTEST_WEEK,
                TeamRoster.player_id.in_(list(substitutes.keys())),
            )
        )
        .scalars()
        .all()
    )
    for roster in rosters:
        sub = substitutes[roster.player_id]
        extra = dict(roster.extra_data or {})
        extra["nfl_com_points"] = sub.league_points
        extra["hamlin_substitute"] = {
            "basis": SUBSTITUTE_BASIS,
            "league_points": sub.league_points,
            "wk17_partial": {
                "raw_stats": sub.wk17_partial_raw,
                "points": sub.wk17_partial_points,
            },
            "wk19": {
                "raw_stats": sub.wk19_raw,
                "points": sub.wk19_points,
            },
            "points_breakdown": sub.points_breakdown,
        }
        # Reassign so the SQLAlchemy JSON column registers the mutation.
        roster.extra_data = extra
    return len(rosters)


# ---------------------------------------------------------------------------
# Matchup recompute
# ---------------------------------------------------------------------------


def _recompute_matchups(
    session: Session, *, season_id: int, affected: set[int]
) -> tuple[int, list[str]]:
    """Recompute team_score/opponent_score/is_win for every wk17 matchup that
    holds an affected player. Returns (count, unexpected_flips)."""

    matchups = list(
        session.execute(
            select(Matchup).where(
                Matchup.season_id == season_id,
                Matchup.week == NO_CONTEST_WEEK,
            )
        )
        .scalars()
        .all()
    )
    # Starter authoritative points per team (the corrected nfl_com_points sum).
    team_totals = _starter_totals(session)
    affected_teams = _teams_with_affected_player(session, affected=affected)

    recomputed = 0
    flips: list[str] = []
    for matchup in matchups:
        opp_id = matchup.opponent_team_id
        if opp_id is None:
            continue
        if matchup.team_id not in affected_teams and opp_id not in affected_teams:
            continue
        new_team = team_totals.get(matchup.team_id, matchup.team_score or 0.0)
        new_opp = team_totals.get(opp_id, matchup.opponent_score or 0.0)
        old_winner = _winner(matchup.team_id, opp_id, matchup.team_score, matchup.opponent_score)
        new_winner = _winner(matchup.team_id, opp_id, new_team, new_opp)
        matchup.team_score = new_team
        matchup.opponent_score = new_opp
        matchup.is_win = new_team > new_opp
        recomputed += 1
        # The championship is the only expected flip; surface any other so
        # nothing changes silently.
        if old_winner != new_winner and not (matchup.is_playoff and not matchup.is_consolation):
            flips.append(f"matchup {matchup.matchup_id}: winner {old_winner} -> {new_winner}")
    return recomputed, flips


def _starter_totals(session: Session) -> dict[int, float]:
    """Sum of starter ``nfl_com_points`` per team for 2022 wk17 (post-override)."""

    rows = session.execute(
        select(TeamRoster.team_id, TeamRoster.extra_data, TeamRoster.is_starter).where(
            TeamRoster.season_year == SEASON_YEAR,
            TeamRoster.week == NO_CONTEST_WEEK,
        )
    ).all()
    totals: defaultdict[int, float] = defaultdict(float)
    for team_id, extra, is_starter in rows:
        if not is_starter:
            continue
        points = 0.0
        if isinstance(extra, dict):
            value = extra.get("nfl_com_points")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                points = float(value)
        totals[team_id] += points
    return {team_id: round(total, 2) for team_id, total in totals.items()}


def _teams_with_affected_player(session: Session, *, affected: set[int]) -> set[int]:
    rows = session.execute(
        select(TeamRoster.team_id).where(
            TeamRoster.season_year == SEASON_YEAR,
            TeamRoster.week == NO_CONTEST_WEEK,
            TeamRoster.player_id.in_(list(affected)),
        )
    ).all()
    return {team_id for (team_id,) in rows}


def _winner(
    team_id: int, opp_id: int | None, team_score: float | None, opp_score: float | None
) -> int | None:
    ts = team_score or 0.0
    os_ = opp_score or 0.0
    if opp_id is None or ts == os_:
        return None
    return team_id if ts > os_ else opp_id


# ---------------------------------------------------------------------------
# Standings re-derivation
# ---------------------------------------------------------------------------


def _rederive_standings(
    session: Session, *, season: Season
) -> tuple[int | None, int | None, tuple[int, int] | None]:
    """Re-derive champion/runner-up + final_rank/playoff_finish from the title
    game's corrected score. Deterministic and idempotent: the higher scorer in
    the championship matchup is the champion."""

    champ_old = season.champion_team_id
    runner_old = season.runner_up_team_id
    if champ_old is None or runner_old is None:
        return champ_old, runner_old, None

    title = (
        session.execute(
            select(Matchup).where(
                Matchup.season_id == season.season_id,
                Matchup.week == NO_CONTEST_WEEK,
                Matchup.is_playoff.is_(True),
                Matchup.is_consolation.is_(False),
                Matchup.team_id.in_((champ_old, runner_old)),
                Matchup.opponent_team_id.in_((champ_old, runner_old)),
            )
        )
        .scalars()
        .first()
    )
    if title is None:
        return champ_old, runner_old, None

    opp_id = title.opponent_team_id
    winner = _winner(title.team_id, opp_id, title.team_score, title.opponent_score)
    if winner is None or opp_id is None:
        return champ_old, runner_old, None
    loser = opp_id if winner == title.team_id else title.team_id

    swapped: tuple[int, int] | None = None
    if winner != champ_old:
        swapped = (champ_old, winner)
        season.champion_team_id = winner
        season.runner_up_team_id = loser
        _set_rank(session, team_id=winner, final_rank=1, playoff_finish=1)
        _set_rank(session, team_id=loser, final_rank=2, playoff_finish=2)
    return season.champion_team_id, season.runner_up_team_id, swapped


def _set_rank(session: Session, *, team_id: int, final_rank: int, playoff_finish: int) -> None:
    team = session.get(Team, team_id)
    if team is not None:
        team.final_rank = final_rank
        team.playoff_finish = playoff_finish


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _season_for_year(session: Session, *, year: int, league_id: str | None) -> Season | None:
    stmt = select(Season).where(Season.year == year)
    if league_id is not None:
        stmt = stmt.where(Season.league_id == league_id)
    return session.execute(stmt).scalars().first()


def _load_rules(session: Session, season_id: int) -> ScoringRules:
    rows = session.execute(
        select(
            ScoringRule.category,
            ScoringRule.stat_key,
            ScoringRule.points_per_unit,
            ScoringRule.unit_size,
            ScoringRule.threshold_min,
            ScoringRule.threshold_max,
            ScoringRule.flat_points,
        ).where(ScoringRule.season_id == season_id)
    ).all()
    rules = tuple(
        ScoringRuleDataclass(
            category=str(r.category),
            stat_key=str(r.stat_key),
            points_per_unit=float(r.points_per_unit or 0.0),
            unit_size=float(r.unit_size or 1.0),
            threshold_min=(float(r.threshold_min) if r.threshold_min is not None else None),
            threshold_max=(float(r.threshold_max) if r.threshold_max is not None else None),
            flat_points=(float(r.flat_points) if r.flat_points is not None else None),
        )
        for r in rows
    )
    return ScoringRules(season_id=season_id, rules=rules)


__all__ = ["HamlinOverrideResult", "apply_hamlin_2022_wk17_override"]
