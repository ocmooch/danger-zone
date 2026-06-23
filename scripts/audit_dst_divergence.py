#!/usr/bin/env python3
"""Audit DST (DEF) reconstruction divergence from the authoritative NFL.com points.

Fully offline (reads the DB only). For every rostered DEF week it compares our
reconstructed ``player_stats_scored.total_points`` against the ground-truth
``team_rosters.extra_data.nfl_com_points`` and, for each diverging row, finds the
minimal *source-stat* change that would reconcile it. It then disambiguates the
two dominant classes using an independent signal — the opponent's actual final
score, parsed from ``extra_data.game_status`` — so a "+6" gap that could be
either an uncredited TD *or* a two-bracket points-allowed move is split cleanly.

Why this exists: the scoring engine is correct (a rule recompute reproduces the
stored total for every diverging row), so the remaining DST gap is entirely in
the source stat values (``defensive_tds`` / ``special_teams_tds`` undercount, and
the ``points_allowed`` derivation). This audit quantifies each class so a fix can
be scoped and, after a re-ingest, regression-checked. See
``dz-dashboard/docs/plans/dst-deep-classification.md``.

Census classes:
  * TD undercount     — gap is +6/+12 and the opponent's final score independently
                        confirms our points_allowed bracket -> the gap is real
                        uncredited defensive/ST touchdown(s).
  * points_allowed    — a bracket move reconciles the gap and the opponent's final
                        score is the value that produces it (our derived PA is wrong).
  * TD_or_PA          — gap is +6/+12 but the final score disagrees with our PA;
                        needs play-by-play to split.
  * OTHER             — sacks / yards / residual one-offs.

Usage:
    uv run python scripts/audit_dst_divergence.py
    uv run python scripts/audit_dst_divergence.py --db sqlite:////tmp/copy.db --detail TD
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_TOLERANCE = 0.1
_GAME_SCORE = re.compile(r"(\d+)\s*-\s*(\d+)")


def _opponent_score(game_status: str | None) -> int | None:
    """Opponent's score from a ``"Win,15-9"`` / ``"Loss,6-13"`` status (2nd number)."""
    if not game_status:
        return None
    m = _GAME_SCORE.search(game_status)
    return int(m.group(2)) if m else None


def _bracket_flat(
    brackets: list[tuple[float, float | None, float]], value: float | None
) -> float | None:
    if value is None:
        return None
    for lo, hi, flat in brackets:
        if value >= lo and (hi is None or value <= hi):
            return flat
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit DST reconstruction divergence (offline).")
    parser.add_argument("--db", type=str, default=None, help="sqlite path or DATABASE_URL override")
    parser.add_argument(
        "--detail",
        choices=("TD", "PA", "TD_or_PA", "OTHER"),
        default=None,
        help="Print per-row detail for one class.",
    )
    args = parser.parse_args()

    import sqlite3

    from ff_pipeline.settings import get_settings

    db_url = args.db or get_settings().database_url
    sqlite_path = (
        db_url.replace("sqlite:///", "").replace("sqlite://", "") if "sqlite" in db_url else db_url
    )
    db = sqlite3.connect(sqlite_path)
    c = db.cursor()

    # Per-season defense rules: per-unit (ppu, unit) and bracket lists.
    unit_rules: dict[int, dict[str, tuple[float, float]]] = defaultdict(dict)
    bracket_rules: dict[int, dict[str, list[tuple[float, float | None, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for sid, sk, ppu, unit, tmin, tmax, flat in c.execute(
        "SELECT season_id,stat_key,points_per_unit,unit_size,threshold_min,threshold_max,flat_points "
        "FROM scoring_rules WHERE category='defense'"
    ):
        if flat is None:
            unit_rules[sid][sk] = (ppu, unit)
        else:
            bracket_rules[sid][sk].append((tmin, tmax, flat))

    def score_defense(stats: dict[str, float], sid: int) -> float:
        total = 0.0
        for sk, (ppu, unit) in unit_rules[sid].items():
            v = stats.get(sk)
            if v:
                total += (v / unit) * ppu
        for sk, brs in bracket_rules[sid].items():
            if sk in stats:  # a missing bracket key correctly scores nothing
                flat = _bracket_flat(brs, stats[sk])
                if flat is not None:
                    total += flat
        return round(total, 2)

    rows = c.execute(
        """
        SELECT tr.season_year, tr.week, p.nfl_team, se.season_id,
               CAST(json_extract(tr.extra_data,'$.nfl_com_points') AS FLOAT),
               s.total_points, r.stats, json_extract(tr.extra_data,'$.game_status')
        FROM team_rosters tr
        JOIN players p ON tr.player_id=p.player_id
        JOIN seasons se ON se.year=tr.season_year
        JOIN player_stats_scored s
          ON s.player_id=tr.player_id AND s.week=tr.week AND s.season_id=se.season_id
        LEFT JOIN player_stats_raw r
          ON r.player_id=tr.player_id AND r.season_year=tr.season_year
         AND r.week=tr.week AND r.source='nflverse'
        WHERE p.position='DEF' AND json_extract(tr.extra_data,'$.nfl_com_points') IS NOT NULL
        """
    ).fetchall()

    census: Counter[str] = Counter()
    recompute_mismatch = 0
    detail: list[str] = []
    diverging = 0

    for season_year, week, team, sid, nfl, total, raw, game_status in rows:
        if total is None or nfl is None:
            continue
        gap = round(nfl - total, 2)
        if abs(gap) <= _TOLERANCE:
            continue
        diverging += 1
        stats = {
            k: float(v)
            for k, v in (json.loads(raw) if raw else {}).items()
            if isinstance(v, (int, float))
        }
        # Trust check: our recompute should equal the stored total.
        if abs(score_defense(stats, sid) - total) > _TOLERANCE:
            recompute_mismatch += 1

        cur_pa = stats.get("points_allowed")
        cur_flat = _bracket_flat(bracket_rules[sid]["points_allowed"], cur_pa)
        opp = _opponent_score(game_status)
        opp_flat = _bracket_flat(bracket_rules[sid]["points_allowed"], opp)
        is_td_gap = gap > 0 and abs(gap % 6) < 0.01
        pa_confirms = (
            cur_flat is not None and opp_flat is not None and abs(opp_flat - cur_flat) < 0.01
        )
        pa_explains = (
            not is_td_gap
            and cur_flat is not None
            and opp_flat is not None
            and abs((opp_flat - cur_flat) - gap) < 0.01
        )

        if is_td_gap and pa_confirms:
            klass = "TD"
        elif pa_explains:
            klass = "PA"
        elif is_td_gap:
            klass = "TD_or_PA"
        else:
            klass = f"OTHER need={gap:+g}"
        census[klass] += 1

        if args.detail and klass.split()[0] == args.detail:
            detail.append(
                f"  {season_year} wk{week:>2} {team!s:>3} | nfl={nfl:5g} ours={total:5g} "
                f"gap={gap:+5g} | PA={cur_pa} opp_final={opp}"
            )

    print(f"diverging DST rows (|gap|>{_TOLERANCE}): {diverging}")
    print(f"rule-recompute mismatches (should be 0): {recompute_mismatch}")
    print("\n=== disambiguated census ===")
    for k, n in census.most_common():
        print(f"  {n:4d}  {k}")
    if args.detail:
        print(f"\n=== {args.detail} rows ===")
        for line in detail:
            print(line)


if __name__ == "__main__":
    main()
