"""Roll up nflverse team-level data into team-defense (DST) stat dicts.

Our league rosters one team **DEF** per lineup slot and scores it from
team-level events, not from individual defenders. nflverse exposes the
pieces we need across two frames:

* ``load_team_stats`` — one row per ``(team, season, week)`` with the
  defense's own counting events (``def_interceptions``, ...) plus that
  team's *offensive* yardage and ``sacks_suffered``, which we reuse to
  derive the opponent's "yards allowed" and sack count. (Sacks come from
  the opponent's offense-side count, not the noisier ``def_sacks``.)
* ``load_schedules`` — one row per game, carrying both teams and their
  final scores, which gives us each defense's ``points_allowed`` (the
  opponent's points) and the opponent identity used to look up
  ``total_yards_allowed``.

The output is keyed to the scoring engine's *defense* stat-key
vocabulary (see ``docs/05_SCORING_ENGINE.md`` and the ``defense`` rows in
``scoring_rules``):

    sacks, interceptions, fumbles_recovered, safeties, defensive_tds,
    special_teams_tds, points_allowed, total_yards_allowed

``points_allowed`` and ``total_yards_allowed`` are *bracket-gated* in the
rules: a single numeric value selects one flat-points bracket. They are
only emitted when we can actually derive them (the opponent's score /
offensive yardage is known) — never defaulted to ``0``, because a missing
key correctly scores nothing whereas a spurious ``0`` would award the
"shutout" / "under 100 yards" bonus. The counting stats default to ``0``
(a quiet week is a real zero, not missing data).

The nflverse column names for the ``def_*`` family have shifted across
``nflfastR``/``nflreadpy`` versions, so every engine key maps to a
*candidate list* of source columns (summed if more than one is present),
mirroring the tolerant projection in ``stat_keys.py``. Columns that are
absent are reported once by :func:`expected_team_columns` so a rename is
visible in the logs without crashing a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

log = get_logger(__name__)

# Engine defense counting keys ← candidate nflverse team-stat columns read
# from the team's *own* row. Values are summed when more than one candidate
# is present; a key with no present candidate resolves to 0.0.
#
# ``sacks`` is deliberately **not** here: the defense-side ``def_sacks``
# undercounts via half-sack / unattributed plays, so the rollup sources it
# from the opponent's offense-side ``sacks_suffered`` instead (see
# ``_OPPONENT_SACKS`` and :func:`build_team_defense_stats`).
_DEFENSE_COUNTING_MAP: dict[str, tuple[str, ...]] = {
    "interceptions": ("def_interceptions",),
    # DST "fumbles recovered" = takeaways: opponent fumbles this team
    # recovered (defense or special teams), nflverse ``fumble_recovery_opp``.
    "fumbles_recovered": ("fumble_recovery_opp",),
    "safeties": ("def_safeties",),
    "defensive_tds": ("def_tds",),
    "special_teams_tds": ("special_teams_tds",),
}

# DST "sacks" = the number of times the *opponent's* offense was sacked.
# nflverse's offense-side ``sacks_suffered`` (on the opponent's row) is the
# clean team-level count; ``def_sacks`` is read from this team's own row only
# as a fallback when the opponent's stat row can't be located.
_OPPONENT_SACKS: tuple[str, ...] = ("sacks_suffered",)
_DEFENSE_SACKS_FALLBACK: tuple[str, ...] = ("def_sacks",)

# A team's offensive total **net** yards — reused as the opponent's
# ``total_yards_allowed``. The official NFL total subtracts sack yardage from
# passing: net = passing_yards + rushing_yards - |sack_yards_lost|, where
# nflverse ``passing_yards`` is gross of sacks. NOTE: nflreadpy stores
# ``sack_yards_lost`` as a **negative** number (yards lost), so we subtract
# its *magnitude* — subtracting the signed value would double-negate and add
# the sack yardage back, inflating the total (e.g. 233 - (-46) = 279 vs the
# correct 233 - 46 = 187).
_OFFENSE_YARDS_ADD: tuple[str, ...] = ("passing_yards", "rushing_yards")
_OFFENSE_YARDS_SUBTRACT: tuple[str, ...] = ("sack_yards_lost",)


@dataclass(frozen=True, slots=True)
class TeamDefenseStat:
    """One ``(nfl_team, season, week)`` team-defense stat row.

    ``stats`` is keyed to the engine's defense stat vocabulary and is fed
    directly to ``apply_rules`` (the same shape as
    ``NflversePlayerStat.stats``). ``nfl_opponent`` mirrors the offensive
    rows' provenance column.
    """

    nfl_team: str
    season_year: int
    week: int
    season_type: str
    nfl_opponent: str | None
    stats: dict[str, float]


def expected_team_columns() -> frozenset[str]:
    """Every team-stats column the rollup may consume.

    Used by the client to emit a single warning if nflverse renames or
    drops a column the projection depends on.
    """

    cols: set[str] = set(_OFFENSE_YARDS_ADD) | set(_OFFENSE_YARDS_SUBTRACT)
    cols.update(_OPPONENT_SACKS)
    cols.update(_DEFENSE_SACKS_FALLBACK)
    for candidates in _DEFENSE_COUNTING_MAP.values():
        cols.update(candidates)
    return frozenset(cols)


def project_team_counting_stats(row: Mapping[str, object]) -> dict[str, float]:
    """Project one team-stats row onto the engine's defense counting keys.

    Bracket-gated keys (``points_allowed`` / ``total_yards_allowed``) are
    *not* set here — they depend on the opponent and are filled by
    :func:`build_team_defense_stats`.
    """

    out: dict[str, float] = {}
    for engine_key, candidates in _DEFENSE_COUNTING_MAP.items():
        out[engine_key] = sum(_as_float(row.get(c)) for c in candidates)
    return out


def team_offense_yards(row: Mapping[str, object]) -> float:
    """Total **net** offensive yards for a team-stats row (the yards the
    *defense* on the other side allowed): passing + rushing - sack yards.

    ``sack_yards_lost`` is taken as a magnitude (``abs``) so the result is
    correct regardless of nflverse's sign convention — nflreadpy stores it
    negative, and subtracting the signed value would add the sack yardage
    back rather than remove it.
    """

    gained = sum(_as_float(row.get(c)) for c in _OFFENSE_YARDS_ADD)
    lost = sum(abs(_as_float(row.get(c))) for c in _OFFENSE_YARDS_SUBTRACT)
    return gained - lost


def build_team_defense_stats(
    *,
    team_rows: Iterable[Mapping[str, object]],
    schedule_rows: Iterable[Mapping[str, object]],
    play_by_play_rows: Iterable[Mapping[str, object]] = (),
) -> list[TeamDefenseStat]:
    """Combine team-stat rows and schedule rows into DST stat dicts.

    For each ``(team, season, week)`` team-stats row we attach:

    * the team's own counting stats (sacks, INTs, ...);
    * ``points_allowed`` — the opponent's fantasy D/ST points allowed from
      play-by-play when available, otherwise the opponent's final score;
    * ``total_yards_allowed`` — the opponent's offensive total net yards;
    * ``nfl_opponent`` — the opponent's abbreviation.

    The opponent score comes from the schedule; the opponent yardage comes
    from the *opponent's own* team-stats row for the same week. When a
    schedule game or an opponent stat row can't be located, the
    corresponding bracket key is omitted (scores nothing) rather than
    defaulted.
    """

    # Index team offensive yards by (season, week, team) so an opponent's
    # "yards allowed" is a dict lookup, not a scan.
    offense_yards: dict[tuple[int, int, str], float] = {}
    team_row_index: dict[tuple[int, int, str], Mapping[str, object]] = {}
    for row in team_rows:
        key = _team_key(row)
        if key is None:
            continue
        offense_yards[key] = team_offense_yards(row)
        team_row_index[key] = row

    # Index each game's (opponent, points_allowed) per team-week.
    fantasy_points_allowed = _index_fantasy_points_allowed(play_by_play_rows)
    game_context = _index_schedule(schedule_rows, fantasy_points_allowed=fantasy_points_allowed)

    out: list[TeamDefenseStat] = []
    for key, row in team_row_index.items():
        season_year, week, team = key
        stats = project_team_counting_stats(row)
        season_type = str(row.get("season_type") or "REG")

        # Sacks default to this team's own (possibly undercounted) def_sacks;
        # overridden below by the opponent's authoritative ``sacks_suffered``
        # when the opponent's stat row is available.
        stats["sacks"] = sum(_as_float(row.get(c)) for c in _DEFENSE_SACKS_FALLBACK)

        opponent: str | None = None
        ctx = game_context.get(key)
        if ctx is not None:
            opponent, points_allowed = ctx
            if points_allowed is not None:
                stats["points_allowed"] = points_allowed
            if opponent is not None:
                opp_key = (season_year, week, opponent)
                opp_row = team_row_index.get(opp_key)
                if opp_row is not None:
                    stats["sacks"] = sum(_as_float(opp_row.get(c)) for c in _OPPONENT_SACKS)
                opp_yards = offense_yards.get(opp_key)
                if opp_yards is not None:
                    stats["total_yards_allowed"] = opp_yards

        out.append(
            TeamDefenseStat(
                nfl_team=team,
                season_year=season_year,
                week=week,
                season_type=season_type,
                nfl_opponent=opponent,
                stats=stats,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _index_schedule(
    schedule_rows: Iterable[Mapping[str, object]],
    *,
    fantasy_points_allowed: Mapping[tuple[int, int, str], float] | None = None,
) -> dict[tuple[int, int, str], tuple[str | None, float | None]]:
    """``(season, week, team) -> (opponent, points_allowed)``.

    ``points_allowed`` is the opponent's score. A game with a missing
    score (not yet played) yields ``None`` for that side's points so the
    bracket key is skipped rather than awarded a shutout bonus.
    """

    index: dict[tuple[int, int, str], tuple[str | None, float | None]] = {}
    for row in schedule_rows:
        season = _as_opt_int(row.get("season"))
        week = _as_opt_int(row.get("week"))
        home = _as_opt_str(row.get("home_team"))
        away = _as_opt_str(row.get("away_team"))
        if season is None or week is None or home is None or away is None:
            continue
        home_score = _as_opt_float(row.get("home_score"))
        away_score = _as_opt_float(row.get("away_score"))
        if fantasy_points_allowed is not None:
            home_score = fantasy_points_allowed.get((season, week, away), home_score)
            away_score = fantasy_points_allowed.get((season, week, home), away_score)
        # points_allowed for a team is the *other* side's score.
        index[(season, week, home)] = (away, away_score)
        index[(season, week, away)] = (home, home_score)
    return index


def _index_fantasy_points_allowed(
    play_by_play_rows: Iterable[Mapping[str, object]],
) -> dict[tuple[int, int, str], float]:
    """Return fantasy D/ST points allowed by ``(season, week, defense_team)``.

    Final score is not the same as fantasy D/ST points allowed. Defensive
    return TDs and safeties scored against the offense are not charged to that
    team's D/ST; kickoff/punt return TDs are charged because they are scored
    against the special-teams half of the D/ST unit. Extra points and two-point
    conversions inherit the preceding touchdown's classification.
    """

    out: dict[tuple[int, int, str], float] = {}
    prev_score_by_game: dict[str, tuple[float, float]] = {}
    last_td_counts_by_game_team: dict[tuple[str, str], bool] = {}
    for row in play_by_play_rows:
        game_id = _as_opt_str(row.get("game_id"))
        season = _as_opt_int(row.get("season"))
        week = _as_opt_int(row.get("week"))
        home = _as_opt_str(row.get("home_team"))
        away = _as_opt_str(row.get("away_team"))
        home_score = _as_opt_float(row.get("total_home_score"))
        away_score = _as_opt_float(row.get("total_away_score"))
        if (
            game_id is None
            or season is None
            or week is None
            or home is None
            or away is None
            or home_score is None
            or away_score is None
        ):
            continue

        previous = prev_score_by_game.get(game_id)
        current = (home_score, away_score)
        prev_score_by_game[game_id] = current
        if previous is None:
            continue

        home_delta = home_score - previous[0]
        away_delta = away_score - previous[1]
        scoring_team: str | None = None
        points = 0.0
        if home_delta > 0 and away_delta == 0:
            scoring_team = home
            points = home_delta
        elif away_delta > 0 and home_delta == 0:
            scoring_team = away
            points = away_delta
        if scoring_team is None or points <= 0:
            continue

        counts = _score_counts_against_dst(row, scoring_team, last_td_counts_by_game_team, game_id)
        if counts:
            charged_team = away if scoring_team == home else home
            out[(season, week, charged_team)] = out.get((season, week, charged_team), 0.0) + points
    return out


def _score_counts_against_dst(
    row: Mapping[str, object],
    scoring_team: str,
    last_td_counts_by_game_team: dict[tuple[str, str], bool],
    game_id: str,
) -> bool:
    posteam = _as_opt_str(row.get("posteam"))
    td_team = _as_opt_str(row.get("td_team"))
    if _as_float(row.get("touchdown")):
        counts = td_team == posteam or _is_special_teams_return_touchdown(row)
        last_td_counts_by_game_team[(game_id, scoring_team)] = counts
        return counts
    if _as_float(row.get("safety")):
        return False
    if _as_opt_str(row.get("field_goal_result")) == "made":
        return scoring_team == posteam
    if row.get("extra_point_result") is not None or row.get("two_point_conv_result") is not None:
        return last_td_counts_by_game_team.get((game_id, scoring_team), scoring_team == posteam)
    return scoring_team == posteam


def _is_special_teams_return_touchdown(row: Mapping[str, object]) -> bool:
    desc = (_as_opt_str(row.get("desc")) or "").lower()
    return any(
        marker in desc for marker in (" punt", " punts ", " kicks ", " kickoff", "field goal")
    )


def _team_key(row: Mapping[str, object]) -> tuple[int, int, str] | None:
    season = _as_opt_int(row.get("season"))
    week = _as_opt_int(row.get("week"))
    team = _as_opt_str(row.get("team"))
    if season is None or week is None or team is None:
        return None
    return (season, week, team)


def _as_float(value: object) -> float:
    if value is None or isinstance(value, bool):
        return float(bool(value)) if isinstance(value, bool) else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_opt_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_opt_int(value: object) -> int | None:
    f = _as_opt_float(value)
    return int(f) if f is not None else None


def _as_opt_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return str(value)


__all__ = [
    "TeamDefenseStat",
    "build_team_defense_stats",
    "expected_team_columns",
    "project_team_counting_stats",
    "team_offense_yards",
]
