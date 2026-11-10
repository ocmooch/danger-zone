"""Reconstructed vs official points for a platform season (S6, ``01`` §9).

Per rostered player-week, three numbers:

* **official** - what the platform scored (``team_rosters.extra_data.platform_points``);
* **reconstructed** - our engine over nflverse stats (``player_stats_scored``);
* **platform-stats score** - our engine over the platform's *own* stats
  (``player_stats_raw`` with ``source='sleeper'``, S6-D3), when stored.

A divergence is classified by which of these agree:

* ``match`` - reconstructed within ``tolerance`` of official;
* ``stat_source`` - the platform-stats score matches official, so the rules are
  right and the nflverse stat line differs (the differing keys are listed);
* ``rule`` - even the platform's own stats don't reproduce official: a rule or
  mapping bug;
* ``unexplained`` - no platform stats to tell the two apart.

Weeks the platform hasn't finalized are reported apart (``provisional``). This
module only reads and reports; it never changes a score.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ff_pipeline.repository.models import (
    Matchup,
    Player,
    PlayerStatsRaw,
    PlayerStatsScored,
    Season,
    TeamRoster,
)
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rescore import _load_rules

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.scoring.rules import ScoringRules

MATCH = "match"
STAT_SOURCE = "stat_source"
RULE = "rule"
UNEXPLAINED = "unexplained"


@dataclass(frozen=True, slots=True)
class PlayerWeekComparison:
    week: int
    player_id: int
    player_name: str
    position: str | None
    is_starter: bool
    official: float | None
    reconstructed: float | None
    platform_stats_score: float | None
    cls: str
    provisional: bool
    differing_stats: tuple[tuple[str, float, float], ...] = ()  # (key, nflverse, platform)

    @property
    def delta(self) -> float | None:
        if self.official is None:
            return None
        return round((self.reconstructed or 0.0) - self.official, 2)


@dataclass(slots=True)
class PlatformReconcile:
    season_year: int
    tolerance: float
    finalized_weeks: tuple[int, ...]
    comparisons: list[PlayerWeekComparison] = field(default_factory=list)
    note: str | None = None

    def rows(
        self, *, starters_only: bool = True, provisional: bool = False
    ) -> list[PlayerWeekComparison]:
        return [
            c
            for c in self.comparisons
            if c.provisional == provisional and (c.is_starter or not starters_only)
        ]

    def counts(self, *, starters_only: bool = True, provisional: bool = False) -> Counter[str]:
        return Counter(
            c.cls for c in self.rows(starters_only=starters_only, provisional=provisional)
        )

    def stat_key_counts(self, *, starters_only: bool = True) -> Counter[str]:
        out: Counter[str] = Counter()
        for c in self.rows(starters_only=starters_only):
            if c.cls == STAT_SOURCE:
                out.update(k for k, _a, _b in c.differing_stats)
        return out


def reconcile_platform_points(
    session: Session,
    *,
    league_id: str,
    season_year: int,
    tolerance: float = 0.01,
    platform_source: str = "sleeper",
) -> PlatformReconcile:
    season = session.scalars(
        select(Season).where(Season.league_id == league_id, Season.year == season_year)
    ).first()
    if season is None:
        return PlatformReconcile(season_year, tolerance, (), note="season_not_found")
    rules = _load_rules(session, season.season_id)
    if not rules.rules:
        return PlatformReconcile(season_year, tolerance, (), note="scoring_rules_missing")

    finalized = _finalized_weeks(session, season.season_id)
    scored = {
        (pid, wk): float(tp)
        for pid, wk, tp in session.execute(
            select(
                PlayerStatsScored.player_id, PlayerStatsScored.week, PlayerStatsScored.total_points
            ).where(PlayerStatsScored.season_id == season.season_id)
        ).tuples()
        if tp is not None
    }
    raw: dict[str, dict[tuple[int, int], dict[str, Any]]] = {"nflverse": {}, platform_source: {}}
    for pid, wk, src, stats in session.execute(
        select(
            PlayerStatsRaw.player_id,
            PlayerStatsRaw.week,
            PlayerStatsRaw.source,
            PlayerStatsRaw.stats,
        ).where(
            PlayerStatsRaw.season_year == season_year,
            PlayerStatsRaw.source.in_(["nflverse", platform_source]),
        )
    ).tuples():
        raw[src][(pid, wk)] = stats or {}
    players = {
        p.player_id: p
        for p in session.scalars(
            select(Player).where(
                Player.player_id.in_(
                    select(TeamRoster.player_id).where(TeamRoster.season_year == season_year)
                )
            )
        )
    }

    out = PlatformReconcile(season_year, tolerance, tuple(sorted(finalized)))
    rows = session.execute(
        select(
            TeamRoster.week, TeamRoster.player_id, TeamRoster.is_starter, TeamRoster.extra_data
        ).where(TeamRoster.season_year == season_year, TeamRoster.week >= 1)
    ).tuples()
    for week, pid, is_starter, extra in rows:
        official = _float((extra or {}).get("platform_points"))
        if official is None:
            continue
        key = (pid, week)
        reconstructed = scored.get(key)
        plat_stats = raw[platform_source].get(key)
        plat_score = (
            apply_rules(_numeric(plat_stats), rules).total_points
            if plat_stats is not None
            else None
        )
        cls = _classify(official, reconstructed, plat_score, tolerance)
        differing: tuple[tuple[str, float, float], ...] = ()
        if cls != MATCH and plat_stats is not None:
            differing = _differing(raw["nflverse"].get(key) or {}, plat_stats, rules)
        p = players.get(pid)
        out.comparisons.append(
            PlayerWeekComparison(
                week=week,
                player_id=pid,
                player_name=(p.name_full if p else str(pid)),
                position=(p.position if p else None),
                is_starter=bool(is_starter),
                official=official,
                reconstructed=reconstructed,
                platform_stats_score=plat_score,
                cls=cls,
                provisional=week not in finalized,
                differing_stats=differing,
            )
        )
    out.comparisons.sort(key=lambda c: (c.week, c.player_name))
    return out


_CLASS_LABEL = {
    MATCH: "match (within tolerance)",
    STAT_SOURCE: "stat source (Sleeper's own stats reproduce official; nflverse differs)",
    RULE: "rule (Sleeper's own stats do not reproduce official)",
    UNEXPLAINED: "unexplained (no Sleeper stats to compare)",
}


def render_markdown(
    rec: PlatformReconcile,
    *,
    team_totals: tuple[int, int] | None = None,
    generated: str = "",
    notes: tuple[str, ...] = (),
) -> str:
    """The reconcile as a markdown report. Names NFL players only (public);
    no league team or owner names. ``team_totals`` = (passed, total)."""
    lines = [
        f"# {rec.season_year} reconcile: reconstructed vs Sleeper official points",
        "",
        f"Generated {generated}. Tolerance {rec.tolerance:g} per player-week. "
        f"Finalized weeks: {_weeks(rec.finalized_weeks)}.",
        "",
        "- **Official**: Sleeper's per-player points (`team_rosters.extra_data.platform_points`).",
        "- **Reconstructed**: our engine over nflverse stats with the D-10 rules.",
        "- **Sleeper-stats score**: our engine over Sleeper's own stat line (S6-D3).",
        "",
        "## Summary (finalized weeks)",
        "",
        "| Class | Started | All rostered |",
        "|---|---:|---:|",
    ]
    started = rec.counts(starters_only=True)
    allr = rec.counts(starters_only=False)
    for cls in (MATCH, STAT_SOURCE, RULE, UNEXPLAINED):
        lines.append(f"| {_CLASS_LABEL[cls]} | {started.get(cls, 0)} | {allr.get(cls, 0)} |")
    lines.append(f"| **total** | {sum(started.values())} | {sum(allr.values())} |")
    if team_totals is not None:
        lines += [
            "",
            f"Team totals (sum of reconstructed starters vs Sleeper's matchup score, "
            f"tolerance {rec.tolerance:g}): **{team_totals[0]} / {team_totals[1]}** match.",
        ]
    keys = rec.stat_key_counts(starters_only=False)
    if keys:
        lines += [
            "",
            "## Stat-source divergences by stat (all rostered)",
            "",
            "| Stat key | Player-weeks |",
            "|---|---:|",
        ]
        lines += [f"| `{k}` | {n} |" for k, n in keys.most_common()]
    diverging = [c for c in rec.rows(starters_only=False) if c.cls != MATCH]
    if diverging:
        lines += [
            "",
            "## Divergences (finalized weeks, all rostered)",
            "",
            "| Wk | Player | Pos | Started | Official | Reconstructed | Δ | Class | nflverse → Sleeper |",
            "|---:|---|---|:-:|---:|---:|---:|---|---|",
        ]
        for c in diverging:
            diff = "; ".join(f"{k} {a:g}→{b:g}" for k, a, b in c.differing_stats) or "-"
            lines.append(
                f"| {c.week} | {c.player_name} | {c.position or ''} | "
                f"{'✓' if c.is_starter else ''} | {c.official:g} | "
                f"{'-' if c.reconstructed is None else f'{c.reconstructed:g}'} | "
                f"{c.delta:+g} | {c.cls} | {diff} |"
            )
    prov = rec.counts(starters_only=False, provisional=True)
    if prov:
        lines += [
            "",
            "## Provisional (weeks not finalized by Sleeper)",
            "",
            ", ".join(f"{cls}: {n}" for cls, n in sorted(prov.items()))
            + " (all rostered). Stats are still moving; not counted above.",
        ]
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


def _weeks(weeks: tuple[int, ...]) -> str:
    if not weeks:
        return "none"
    if list(weeks) == list(range(weeks[0], weeks[-1] + 1)):
        return f"{weeks[0]}-{weeks[-1]}"
    return ", ".join(map(str, weeks))


def _classify(
    official: float, reconstructed: float | None, plat_score: float | None, tol: float
) -> str:
    if abs((reconstructed or 0.0) - official) <= tol:
        return MATCH
    if plat_score is None:
        return UNEXPLAINED
    if abs(plat_score - official) <= tol:
        return STAT_SOURCE
    return RULE


def _differing(
    nflverse: dict[str, Any], platform: dict[str, Any], rules: ScoringRules
) -> tuple[tuple[str, float, float], ...]:
    keys = {r.stat_key for r in rules.rules}
    a, b = _numeric(nflverse), _numeric(platform)
    return tuple(
        (k, a.get(k, 0.0), b.get(k, 0.0))
        for k in sorted(keys)
        if abs(a.get(k, 0.0) - b.get(k, 0.0)) > 1e-9
    )


def _finalized_weeks(session: Session, season_id: int) -> set[int]:
    weeks: dict[int, bool] = {}
    for week, extra in session.execute(
        select(Matchup.week, Matchup.extra_data).where(Matchup.season_id == season_id)
    ).tuples():
        final = bool((extra or {}).get("finalized"))
        weeks[week] = weeks.get(week, True) and final
    return {w for w, final in weeks.items() if final}


def _numeric(stats: dict[str, Any] | None) -> dict[str, float]:
    return {
        k: float(v)
        for k, v in (stats or {}).items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    }


def _float(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "MATCH",
    "RULE",
    "STAT_SOURCE",
    "UNEXPLAINED",
    "PlatformReconcile",
    "PlayerWeekComparison",
    "reconcile_platform_points",
    "render_markdown",
]
