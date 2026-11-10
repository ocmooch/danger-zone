"""Play-by-play counts the nflverse weekly files don't carry (S6).

Sleeper's 2026 scoring adds stats that only play-by-play can count. Each
definition below was checked against Sleeper's own weekly stats for weeks 1-8
of 2026 (see ``docs/sleeper/reports/2026-reconcile.md``):

* **40+ yd plays** (per player): a completion of 40+ passing yards (passer),
  40+ receiving yards (receiver; a lateral after the catch is not the
  receiver's), a rush of 40+ rushing yards. Two-point tries never count. A
  40+ yd touchdown counts here *and* as a long TD.
* **Special-teams fumble recoveries**: on a kickoff or punt, the *kicking*
  team recovers a fumble by the return team. nflverse sets ``posteam`` to the
  receiving team on kickoffs and to the punting team on punts. Credited to the
  recovering player and to the kicking team's DEF.
* **Blocked kicks** (DEF): a blocked punt, field goal or extra point, credited
  to the defending team.
* **Defensive 2-pt returns** (DEF): a defensive two-point conversion (a
  returned PAT or 2-pt try), credited to the defending team.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ff_pipeline.crawlers.nflverse.long_td_bonus import LongTdBonusKey
from ff_pipeline.nfl_teams import canonical_franchise

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

PASSING_COMPLETIONS_40_PLUS = "passing_completions_40_plus"
RUSHING_ATTEMPTS_40_PLUS = "rushing_attempts_40_plus"
RECEPTIONS_40_PLUS = "receptions_40_plus"
SPECIAL_TEAMS_FUMBLE_RECOVERIES = "special_teams_fumble_recoveries"

BLOCKED_KICKS = "blocked_kicks"
DEFENSIVE_2PT_RETURNS = "defensive_2pt_returns"
DEFENSE_SPECIAL_TEAMS_FUMBLE_RECOVERIES = "defense_special_teams_fumble_recoveries"

PLAYER_PLAY_STAT_ZEROES: dict[str, float] = {
    PASSING_COMPLETIONS_40_PLUS: 0.0,
    RUSHING_ATTEMPTS_40_PLUS: 0.0,
    RECEPTIONS_40_PLUS: 0.0,
    SPECIAL_TEAMS_FUMBLE_RECOVERIES: 0.0,
}
TEAM_PLAY_STAT_ZEROES: dict[str, float] = {
    BLOCKED_KICKS: 0.0,
    DEFENSIVE_2PT_RETURNS: 0.0,
    DEFENSE_SPECIAL_TEAMS_FUMBLE_RECOVERIES: 0.0,
}

TeamKey = tuple[int, int, str]  # (season, week, canonical franchise)
_LONG_PLAY_YARDS = 40.0


@dataclass(slots=True)
class PlayCounts:
    player: dict[LongTdBonusKey, dict[str, float]] = field(default_factory=dict)
    team: dict[TeamKey, dict[str, float]] = field(default_factory=dict)


def derive_play_counts(play_by_play_rows: Iterable[Mapping[str, object]]) -> PlayCounts:
    """Count the S6 play stats per player-week and per team-week."""
    player: defaultdict[LongTdBonusKey, defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    team: defaultdict[TeamKey, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in play_by_play_rows:
        season = _as_int(row.get("season"))
        week = _as_int(row.get("week"))
        if season is None or week is None:
            continue

        def bump_player(gsis: object, key: str, s: int = season, w: int = week) -> None:
            pid = _str(gsis)
            if pid is not None:
                player[LongTdBonusKey(gsis_id=pid, season_year=s, week=w)][key] += 1.0

        play_type = _str(row.get("play_type"))
        posteam = _str(row.get("posteam"))
        defteam = _str(row.get("defteam"))

        if not _truthy(row.get("two_point_attempt")):
            if _truthy(row.get("complete_pass")):
                if _num(row.get("passing_yards")) >= _LONG_PLAY_YARDS:
                    bump_player(row.get("passer_player_id"), PASSING_COMPLETIONS_40_PLUS)
                if _num(row.get("receiving_yards")) >= _LONG_PLAY_YARDS:
                    bump_player(row.get("receiver_player_id"), RECEPTIONS_40_PLUS)
            if _truthy(row.get("rush_attempt")) and (
                _num(row.get("rushing_yards")) >= _LONG_PLAY_YARDS
            ):
                bump_player(row.get("rusher_player_id"), RUSHING_ATTEMPTS_40_PLUS)

        if play_type in {"kickoff", "punt"}:
            kicking = defteam if play_type == "kickoff" else posteam
            for i in (1, 2):
                rec_team = _str(row.get(f"fumble_recovery_{i}_team"))
                fumbler_team = _str(row.get(f"fumbled_{i}_team"))
                if kicking and rec_team == kicking and fumbler_team not in (None, kicking):
                    bump_player(
                        row.get(f"fumble_recovery_{i}_player_id"),
                        SPECIAL_TEAMS_FUMBLE_RECOVERIES,
                    )
                    _bump_team(team, season, week, kicking, DEFENSE_SPECIAL_TEAMS_FUMBLE_RECOVERIES)

        if defteam and (
            _truthy(row.get("punt_blocked"))
            or _str(row.get("field_goal_result")) == "blocked"
            or _str(row.get("extra_point_result")) == "blocked"
        ):
            _bump_team(team, season, week, defteam, BLOCKED_KICKS)
        if defteam and (
            _truthy(row.get("defensive_two_point_conv"))
            or _truthy(row.get("defensive_extra_point_conv"))
        ):
            _bump_team(team, season, week, defteam, DEFENSIVE_2PT_RETURNS)

    return PlayCounts(
        player={k: dict(v) for k, v in player.items()},
        team={k: dict(v) for k, v in team.items()},
    )


def _bump_team(
    team: defaultdict[TeamKey, defaultdict[str, float]],
    season: int,
    week: int,
    abbrev: str,
    key: str,
) -> None:
    franchise = canonical_franchise(abbrev) or abbrev
    team[(season, week, franchise)][key] += 1.0


def _num(value: object) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _truthy(value: object) -> bool:
    return _num(value) > 0


def _str(value: object) -> str | None:
    if value is None:
        return None
    out = str(value).strip()
    return out or None


__all__ = [
    "BLOCKED_KICKS",
    "DEFENSE_SPECIAL_TEAMS_FUMBLE_RECOVERIES",
    "DEFENSIVE_2PT_RETURNS",
    "PASSING_COMPLETIONS_40_PLUS",
    "PLAYER_PLAY_STAT_ZEROES",
    "RECEPTIONS_40_PLUS",
    "RUSHING_ATTEMPTS_40_PLUS",
    "SPECIAL_TEAMS_FUMBLE_RECOVERIES",
    "TEAM_PLAY_STAT_ZEROES",
    "PlayCounts",
    "derive_play_counts",
]
