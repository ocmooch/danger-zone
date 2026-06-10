"""Distill (solve, don't guess) a season's fantasy scoring rules from data.

Why
---
Seasons 2010-2015 carry no ``scoring_rules`` rows, so ``rescore`` skips them
and they have no ``player_stats_scored`` (open question §P1-V1). But every
starter-week in ``team_rosters.extra_data`` carries ``nfl_com_points`` — the
*actual* league score NFL.com recorded — and ``player_stats_raw.stats`` already
maps nflverse stats to the engine's canonical keys for those same years. So for
any season we hold a labeled dataset of ``(stat_vector -> league_points)`` and
can **solve** the per-unit scoring coefficients rather than guess them.

Method
------
For each season build the clean labeled matrix ``X . beta = y`` over clean skill
starter-weeks (QB/RB/WR/TE; DST and K carry rollup/bracket complications and are
fit separately by ``--kdst``):

* one **row** per starter-week,
* **columns** ``X`` = canonical stat keys (passing/rushing/receiving yards, TDs,
  receptions, INTs, fumbles, 2pt, special-teams TDs) plus engineered indicator
  columns for the yardage *bonuses* (100/200 rush+recv, 300/400 pass),
* **target** ``y`` = ``nfl_com_points`` from ``team_rosters.extra_data``,
* **unknowns** ``beta`` = points-per-unit per stat key.

~1,170 equations over ~15 unknowns: massively overdetermined. NFL.com
coefficients are exact rationals (0.04 = 1/25, 0.1 = 1/10, 4, 6, 1.0, ...), so
we solve by least squares (pure-Python normal equations — no numpy dependency),
**snap each coefficient to the nearest exact rational**, then verify the snapped
ruleset reproduces the data to the cent *by scoring it back through the real
engine* (``scoring.engine.apply_rules``) — no parallel scoring path.

What it resolves vs. doesn't
----------------------------
Resolves every per-unit coefficient whose stat varies in the data — the skill
core, INTs, fumbles, 2pt, the yardage bonuses. It **cannot** resolve the
long-TD-length bonuses (40+/50+ yard TDs need per-TD distance from pbp; the
weekly aggregates lack it). That is the same §P1-V2 gap that caps modern verify
at ~92%, so a recovered ruleset is "correct" at the same ceiling. Rows whose
``nfl_com_points`` the snapped ruleset can't reproduce are reported as the
residual tail and are dominated by that long-TD gap.

Usage
-----
    uv run python scripts/distill_scoring_rules.py --season 2010
    uv run python scripts/distill_scoring_rules.py --season 2010 --emit-csv .project-src/dz-rules-2010.csv
    uv run python scripts/distill_scoring_rules.py --all          # 2010-2025 summary
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from sqlalchemy import text

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rules import ScoringRule, ScoringRules
from ff_pipeline.settings import get_settings

# Skill positions only in the first pass: K and DEF carry the FG-bracket and
# team-defense-rollup complications and are fit separately (--kdst).
_SKILL_SLOTS = ("QB", "RB", "WR", "TE")

# Per-unit features: (feature_name, stat_key). One linear coefficient each.
# 2pt conversions are folded into a single feature (NFL.com awards +2 to the
# scorer regardless of play type) and re-expanded to three rules on emit.
_PER_UNIT_FEATURES: tuple[tuple[str, str], ...] = (
    ("passing_yards", "passing_yards"),
    ("passing_tds", "passing_tds"),
    ("passing_interceptions", "passing_interceptions"),
    ("rushing_yards", "rushing_yards"),
    ("rushing_tds", "rushing_tds"),
    ("receptions", "receptions"),
    ("receiving_yards", "receiving_yards"),
    ("receiving_tds", "receiving_tds"),
    ("fumbles_lost", "fumbles_lost"),
    ("special_teams_tds", "special_teams_tds"),
    ("two_point_conversions", "__2pt__"),
)

# Engineered indicator (flat-bonus) features: (name, stat_key, lo, hi). Value is
# 1 when lo <= stat <= hi, else 0. These recover the flat yardage bonuses.
_BONUS_FEATURES: tuple[tuple[str, str, float, float | None], ...] = (
    ("bonus_pass_300_399", "passing_yards", 300.0, 399.0),
    ("bonus_pass_400_plus", "passing_yards", 400.0, None),
    ("bonus_rush_100_199", "rushing_yards", 100.0, 199.0),
    ("bonus_rush_200_plus", "rushing_yards", 200.0, None),
    ("bonus_recv_100_199", "receiving_yards", 100.0, 199.0),
    ("bonus_recv_200_plus", "receiving_yards", 200.0, None),
)

_FEATURE_NAMES: tuple[str, ...] = tuple(f[0] for f in _PER_UNIT_FEATURES) + tuple(
    f[0] for f in _BONUS_FEATURES
)

# Coarse, NFL.com-vocabulary candidate values per core feature. We snap the
# least-squares estimate to the *nearest value in these sets*, not to an
# arbitrary small-denominator rational — because the unobservable long-TD
# bonuses (40+ = +1 and 50+ = +3 STACK, so a 50-yard TD adds +4 beyond the base
# TD) get absorbed into the TD coefficients and bias the raw estimate high
# (passing_tds lands near 4.15, rushing_tds near 6.16). A coarse grid keyed to
# the values NFL.com actually uses pulls those back to the truth (4, 6) while
# still discriminating the real era differences (PPR 0 vs 0.5 vs 1.0; pass TD 4
# vs 6; pass yard 1/25 vs 1/20).
_CANDIDATES: dict[str, tuple[float, ...]] = {
    "passing_yards": (0.0, 0.04, 0.05),
    "passing_tds": (3.0, 4.0, 5.0, 6.0),
    "passing_interceptions": (0.0, -1.0, -2.0, -3.0),
    "rushing_yards": (0.0, 0.1),
    "rushing_tds": (4.0, 5.0, 6.0),
    "receptions": (0.0, 0.5, 1.0),
    "receiving_yards": (0.0, 0.1),
    "receiving_tds": (4.0, 5.0, 6.0),
    "fumbles_lost": (0.0, -1.0, -2.0, -3.0),
    "special_teams_tds": (0.0, 6.0),
    "two_point_conversions": (0.0, 2.0),
}
# Bonus features are too sparse and too contaminated by the long-TD gap to solve
# reliably (a handful of 200-yard games per season, each correlated with a long
# TD). They are inherited from the canonical current ruleset and reported, not
# independently fit. These are the NFL.com values (flat points per bracket).
_CANONICAL_BONUS: dict[str, float] = {
    "bonus_pass_300_399": 1.0,
    "bonus_pass_400_plus": 3.0,
    "bonus_rush_100_199": 1.0,
    "bonus_rush_200_plus": 3.0,
    "bonus_recv_100_199": 1.0,
    "bonus_recv_200_plus": 3.0,
}
# The current (2016+) ruleset's core values, for a recovered-vs-current diff.
_CURRENT_CORE: dict[str, float] = {
    "passing_yards": 0.04,
    "passing_tds": 4.0,
    "passing_interceptions": -2.0,
    "rushing_yards": 0.1,
    "rushing_tds": 6.0,
    "receptions": 1.0,
    "receiving_yards": 0.1,
    "receiving_tds": 6.0,
    "fumbles_lost": -2.0,
    "special_teams_tds": 6.0,
    "two_point_conversions": 2.0,
}

# A feature whose column is entirely (near-)zero never occurred — unobservable.
_UNOBSERVED_EPS = 1e-9


@dataclass
class LabeledRow:
    stats: dict[str, float]
    y: float


def _load_labeled_rows(conn, season: int) -> list[LabeledRow]:
    rows = conn.execute(
        text(
            """
            SELECT psr.stats AS stats,
                   json_extract(tr.extra_data, '$.nfl_com_points') AS y
            FROM team_rosters tr
            JOIN player_stats_raw psr
              ON psr.player_id = tr.player_id
             AND psr.season_year = tr.season_year
             AND psr.week = tr.week
             AND psr.source = 'nflverse'
             AND psr.is_primary = 1
            WHERE tr.is_starter = 1
              AND tr.season_year = :season
              AND tr.roster_slot IN ('QB', 'RB', 'WR', 'TE')
              AND json_extract(tr.extra_data, '$.nfl_com_points') IS NOT NULL
            """
        ),
        {"season": season},
    ).all()

    out: list[LabeledRow] = []
    for raw_stats, y in rows:
        stats = json.loads(raw_stats) if isinstance(raw_stats, str) else (raw_stats or {})
        numeric = {k: float(v) for k, v in stats.items() if isinstance(v, (int, float))}
        # Fold the three 2pt keys into one synthetic feature stat.
        numeric["__2pt__"] = (
            numeric.get("passing_2pt_conversions", 0.0)
            + numeric.get("rushing_2pt_conversions", 0.0)
            + numeric.get("receiving_2pt_conversions", 0.0)
        )
        out.append(LabeledRow(stats=numeric, y=float(y)))
    return out


def _feature_vector(stats: dict[str, float]) -> list[float]:
    vec: list[float] = []
    for _name, key in _PER_UNIT_FEATURES:
        vec.append(stats.get(key, 0.0))
    for _name, key, lo, hi in _BONUS_FEATURES:
        v = stats.get(key, 0.0)
        on = v >= lo and (hi is None or v <= hi)
        vec.append(1.0 if on else 0.0)
    return vec


# ---------------------------------------------------------------------------
# Pure-Python ordinary least squares via the normal equations.
#
# With ~15 unknowns, X^T X is at most 17x17; solving it with Gaussian
# elimination (partial pivoting) is exact enough and keeps this script
# dependency-free. Columns that never vary are dropped (and reported as
# unobserved) so the system stays full-rank.
# ---------------------------------------------------------------------------


def _solve_least_squares(
    X: list[list[float]], y: list[float]
) -> tuple[dict[str, float], list[str]]:
    n_features = len(_FEATURE_NAMES)
    # Identify and drop all-zero (unobserved) columns to preserve rank.
    col_max = [max((abs(row[j]) for row in X), default=0.0) for j in range(n_features)]
    active = [j for j in range(n_features) if col_max[j] > _UNOBSERVED_EPS]
    unobserved = [_FEATURE_NAMES[j] for j in range(n_features) if col_max[j] <= _UNOBSERVED_EPS]

    k = len(active)
    # Normal equations: A = Xa^T Xa, b = Xa^T y.
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for r, row in enumerate(X):
        yi = y[r]
        for a in range(k):
            xa = row[active[a]]
            if xa == 0.0:
                continue
            b[a] += xa * yi
            for c in range(a, k):
                A[a][c] += xa * row[active[c]]
    # Mirror the symmetric lower triangle.
    for a in range(k):
        for c in range(a + 1, k):
            A[c][a] = A[a][c]

    beta = _gaussian_solve(A, b)
    coeffs = dict.fromkeys(_FEATURE_NAMES, 0.0)
    for a, j in enumerate(active):
        coeffs[_FEATURE_NAMES[j]] = beta[a]
    return coeffs, unobserved


def _gaussian_solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(A)
    # Augment.
    M = [[*A[i][:], b[i]] for i in range(n)]
    for col in range(n):
        # Partial pivot.
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            continue  # singular column; leave coefficient at 0
        M[col], M[pivot] = M[pivot], M[col]
        pv = M[col][col]
        for j in range(col, n + 1):
            M[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                M[r][j] -= factor * M[col][j]
    return [M[i][n] for i in range(n)]


def _snap_core(name: str, x: float) -> tuple[float, bool]:
    """Snap a core least-squares estimate to its nearest NFL.com candidate.

    Returns ``(snapped, ambiguous)``. ``ambiguous`` is True when the estimate
    sits suspiciously far from every candidate (>0.25 off the nearest) — a sign
    the data can't separate two rules or a stat barely occurred — so the caller
    can flag it for the Phase C questionnaire rather than trust it silently.
    """
    candidates = _CANDIDATES[name]
    snapped = min(candidates, key=lambda c: abs(c - x))
    ambiguous = abs(snapped - x) > 0.25
    return snapped, ambiguous


# ---------------------------------------------------------------------------
# Translate snapped coefficients into real engine ScoringRule objects, so the
# recovered ruleset is verified through scoring.engine.apply_rules — not a
# parallel re-implementation.
# ---------------------------------------------------------------------------


def _build_rules(snapped: dict[str, float]) -> ScoringRules:
    rules: list[ScoringRule] = []

    def per_unit(stat_key: str, value: float, category: str) -> None:
        if value == 0.0:
            return
        frac = Fraction(value).limit_denominator(100)
        rules.append(
            ScoringRule(
                category=category,
                stat_key=stat_key,
                points_per_unit=float(frac.numerator),
                unit_size=float(frac.denominator),
                threshold_min=0.0,
            )
        )

    cat = {
        "passing_yards": "passing",
        "passing_tds": "passing",
        "passing_interceptions": "passing",
        "rushing_yards": "rushing",
        "rushing_tds": "rushing",
        "receptions": "receiving",
        "receiving_yards": "receiving",
        "receiving_tds": "receiving",
        "fumbles_lost": "misc",
        "special_teams_tds": "misc",
    }
    for name, key in _PER_UNIT_FEATURES:
        if key == "__2pt__":
            for c, sk in (
                ("passing", "passing_2pt_conversions"),
                ("rushing", "rushing_2pt_conversions"),
                ("receiving", "receiving_2pt_conversions"),
            ):
                per_unit(sk, snapped[name], c)
            continue
        per_unit(key, snapped[name], cat[key])

    # Bonuses are inherited from the canonical ruleset (see _CANONICAL_BONUS):
    # too sparse/long-TD-contaminated to fit, no evidence they ever changed.
    bonus_cat = {
        "passing_yards": "passing",
        "rushing_yards": "rushing",
        "receiving_yards": "receiving",
    }
    for name, key, lo, hi in _BONUS_FEATURES:
        v = _CANONICAL_BONUS[name]
        if v == 0.0:
            continue
        rules.append(
            ScoringRule(
                category=bonus_cat[key],
                stat_key=key,
                flat_points=v,
                threshold_min=lo,
                threshold_max=hi,
            )
        )
    return ScoringRules(season_id=0, rules=tuple(rules))


def _exact_pct(rows: list[LabeledRow], rules: ScoringRules) -> float:
    """Fraction of rows the ruleset reproduces to the cent, scored through the engine."""
    rule_keys = {r.stat_key for r in rules.rules}
    exact = 0
    for row in rows:
        scoped = {k: v for k, v in row.stats.items() if k in rule_keys}
        if abs(apply_rules(scoped, rules).total_points - row.y) < 0.005:
            exact += 1
    return 100.0 * exact / len(rows) if rows else 0.0


def _refine_coordinate_ascent(rows: list[LabeledRow], seed: dict[str, float]) -> dict[str, float]:
    """Pick the candidate per core coefficient that maximizes exact-to-cent.

    Nearest-value snapping fails when the long-TD contamination lands an
    estimate near a midpoint (2018 passing_tds = 4.51 -> wrongly 5, which then
    *overscores* 13% of rows). The data itself is the arbiter: hold every other
    coefficient fixed and choose the value that reproduces the most NFL.com
    totals exactly, iterating to a fixed point. Ties keep the incumbent value,
    so well-determined coefficients never drift and sparse ones (a season with
    no skill-player return TD) stay at their seeded canonical default.
    """
    current = dict(seed)
    for _ in range(6):
        moved = False
        for name in _CANDIDATES:
            best_val = current[name]
            best_score = _exact_pct(rows, _build_rules(current))
            for cand in _CANDIDATES[name]:
                if cand == current[name]:
                    continue
                trial = dict(current)
                trial[name] = cand
                score = _exact_pct(rows, _build_rules(trial))
                if score > best_score + 1e-9:
                    best_score, best_val = score, cand
            if best_val != current[name]:
                current[name] = best_val
                moved = True
        if not moved:
            break
    return current


def _verify_through_engine(
    rows: list[LabeledRow], rules: ScoringRules
) -> tuple[float, float, list[float]]:
    """Score every row through the real engine; return (exact%, median_delta, deltas)."""
    # Restrict each row's stats to the keys some rule consumes. Numerically a
    # no-op (the engine defaults missing keys to zero), but it keeps the engine
    # from logging an "unmapped stat" warning for every kicker/DST key that
    # rides along in the skill players' raw JSON.
    rule_keys = {r.stat_key for r in rules.rules}
    deltas: list[float] = []
    exact = 0
    for row in rows:
        scoped = {k: v for k, v in row.stats.items() if k in rule_keys}
        result = apply_rules(scoped, rules)
        delta = round(result.total_points - row.y, 2)
        deltas.append(delta)
        if abs(delta) < 0.005:
            exact += 1
    deltas_sorted = sorted(deltas)
    n = len(deltas_sorted)
    median = deltas_sorted[n // 2] if n else 0.0
    return (100.0 * exact / n if n else 0.0), median, deltas


def distill_season(conn, season: int) -> dict:
    rows = _load_labeled_rows(conn, season)
    if not rows:
        return {"season": season, "rows": 0, "error": "no labeled rows"}

    X = [_feature_vector(r.stats) for r in rows]
    y = [r.y for r in rows]
    raw_coeffs, unobserved = _solve_least_squares(X, y)

    snapped: dict[str, float] = {}
    ambiguous: list[str] = []
    for name in _CANDIDATES:
        if name in unobserved:
            # Never occurred this season — no row constrains it. Keep the
            # canonical default so the emitted ruleset stays correct.
            snapped[name] = _CURRENT_CORE[name]
            continue
        val, is_ambiguous = _snap_core(name, raw_coeffs[name])
        snapped[name] = val
        if is_ambiguous:
            ambiguous.append(name)
    # Bonuses are inherited (reported raw for transparency, not solved).
    for name in _CANONICAL_BONUS:
        snapped[name] = _CANONICAL_BONUS[name]

    # Let the data arbitrate: refine to the candidate set that maximizes
    # exact-to-cent. This corrects midpoint mis-snaps (2018) and pins sparse
    # coefficients to whatever reproduces their few rows.
    pre_refine_exact = _exact_pct(rows, _build_rules(snapped))
    snapped = _refine_coordinate_ascent(rows, snapped)

    core_diff = {
        name: (snapped[name], _CURRENT_CORE[name])
        for name in _CURRENT_CORE
        if snapped[name] != _CURRENT_CORE[name] and name not in unobserved
    }

    rules = _build_rules(snapped)
    exact_pct, median_delta, deltas = _verify_through_engine(rows, rules)
    neg_tail = 100.0 * sum(1 for d in deltas if d < -0.005) / len(deltas)
    pos_tail = 100.0 * sum(1 for d in deltas if d > 0.005) / len(deltas)

    return {
        "season": season,
        "rows": len(rows),
        "raw_coeffs": {k: round(v, 4) for k, v in raw_coeffs.items()},
        "snapped": snapped,
        "unobserved": unobserved,
        "ambiguous": ambiguous,
        "core_diff": core_diff,
        "pre_refine_exact_pct": round(pre_refine_exact, 2),
        "exact_pct": round(exact_pct, 2),
        "median_delta": median_delta,
        "neg_tail_pct": round(neg_tail, 2),
        "pos_tail_pct": round(pos_tail, 2),
        "rules": rules,
    }


def _print_report(rep: dict) -> None:
    s = rep["season"]
    if rep.get("error"):
        print(f"  {s}: {rep['error']}")
        return
    print(f"\n=== Season {s} — {rep['rows']} clean skill starter-weeks ===")
    print(
        f"  exact-to-cent: {rep['exact_pct']}%   median Δ: {rep['median_delta']:+.2f}"
        f"   (pre-refine {rep['pre_refine_exact_pct']}%)"
    )
    print(f"  residual tail: {rep['neg_tail_pct']}% under / {rep['pos_tail_pct']}% over")
    if rep["core_diff"]:
        print("  *** CORE DIFFERS FROM CURRENT (2016+) RULES ***")
        for name, (got, cur) in rep["core_diff"].items():
            print(f"      {name:24} recovered {got:<7} vs current {cur}")
    else:
        print("  core matches current (2016+) rules exactly")
    print("  recovered core coefficients (raw least-squares -> snapped):")
    for name in _CANDIDATES:
        raw = rep["raw_coeffs"][name]
        snap = rep["snapped"][name]
        flags = ""
        if name in rep["unobserved"]:
            flags = "  [UNOBSERVED]"
        elif name in rep["ambiguous"]:
            flags = "  [AMBIGUOUS — data can't cleanly separate]"
        marker = " *" if name in rep["core_diff"] else "  "
        print(f"   {marker}{name:24} {raw:9.4f} -> {snap:<8}{flags}")


# Offense-section CSV label for each core feature, plus the unit noun used when
# the value is per-unit (so half-PPR renders "1 point per 2 receptions").
_FEATURE_TO_OFFENSE_LABEL: dict[str, tuple[str, str]] = {
    "passing_yards": ("Passing Yards", "yards"),
    "passing_tds": ("Passing Touchdowns", "td"),
    "passing_interceptions": ("Interceptions Thrown", "int"),
    "rushing_yards": ("Rushing Yards", "yards"),
    "rushing_tds": ("Rushing Touchdowns", "td"),
    "receptions": ("Receptions", "receptions"),
    "receiving_yards": ("Receiving Yards", "yards"),
    "receiving_tds": ("Receiving Touchdowns", "td"),
    "fumbles_lost": ("Fumbles Lost", "fumble"),
    "two_point_conversions": ("2-Point Conversions", "conv"),
}


def _value_text(value: float, unit_noun: str) -> str:
    if value == 0.0:
        return "0 points"
    frac = Fraction(value).limit_denominator(100)
    if frac.denominator == 1:
        n = frac.numerator
        return f"{n} point" + ("s" if n != 1 else "")
    return f"{frac.numerator} point per {frac.denominator} {unit_noun}"


def _emit_settings_csv(rep: dict, path: str, source_csv: str) -> None:
    """Write the recovered ruleset by *patching* the canonical settings CSV.

    Only the Offense-section lines whose value the solve proves changed
    (``core_diff``) are rewritten; bonuses, kicking, and defense are inherited
    verbatim from ``source_csv``. This keeps the recovered ruleset a minimal,
    auditable delta from the known-good current export and loads through exactly
    the same ``scoring load`` machinery as a real NFL.com export — no bespoke
    DB-insert path. The Trade Deadline line is restamped so the loader infers
    the right season year.
    """
    diff = rep["core_diff"]
    patches = {
        _FEATURE_TO_OFFENSE_LABEL[name][0].lower(): _value_text(
            new, _FEATURE_TO_OFFENSE_LABEL[name][1]
        )
        for name, (new, _cur) in diff.items()
        if name in _FEATURE_TO_OFFENSE_LABEL
    }

    with Path(source_csv).open(encoding="utf-8", newline="") as fh:
        raw = fh.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n").rstrip("\n").split("\n")

    out: list[str] = []
    section: str | None = None
    pending_label: str | None = None
    applied: set[str] = set()
    for line in src:
        stripped = line.strip()
        norm = stripped.rstrip(":").strip().lower()
        if norm in ("offense", "kicking", "defense / special teams", "other"):
            section = norm
            pending_label = None
            out.append(line)
            continue
        # Restamp the season so the loader infers the recovered season's year.
        if pending_label is None and norm == "trade deadline":
            pending_label = "__trade_deadline__"
            out.append(line)
            continue
        if pending_label == "__trade_deadline__":
            out.append(f'"November 21, {rep["season"]}"')
            pending_label = None
            continue
        if section == "offense" and pending_label is None and norm in patches:
            pending_label = norm
            out.append(line)
            continue
        if pending_label in patches:
            out.append(patches[pending_label])
            applied.add(pending_label)
            pending_label = None
            continue
        pending_label = None
        out.append(line)

    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        fh.write(newline.join(out) + newline)

    print(f"\n  patched {len(applied)} offense line(s) {sorted(applied)} -> {path}")
    if len(applied) != len(patches):
        missed = set(patches) - applied
        print(f"  WARNING: did not find labels to patch: {sorted(missed)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, help="Season year to distill.")
    ap.add_argument("--all", action="store_true", help="Summarize all seasons 2010-2025.")
    ap.add_argument("--emit-csv", default=None, help="Write recovered ruleset as a settings CSV.")
    ap.add_argument(
        "--source-csv",
        default=".project-src/dz-rules.csv",
        help="Canonical settings CSV to patch when --emit-csv is set.",
    )
    ap.add_argument("--json", default=None, help="Dump the full report dict to this JSON path.")
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args(argv)

    if not args.season and not args.all:
        ap.error("pass --season YEAR or --all")

    settings = get_settings()
    db_url = args.database_url or settings.database_url
    engine = create_app_engine(db_url)

    reports: list[dict] = []
    try:
        with engine.connect() as conn:
            seasons = range(2010, 2026) if args.all else [args.season]
            for yr in seasons:
                rep = distill_season(conn, yr)
                reports.append(rep)
                _print_report(rep)
                if args.emit_csv and yr == args.season:
                    _emit_settings_csv(rep, args.emit_csv, args.source_csv)
    finally:
        engine.dispose()

    if args.json:
        serializable = [{k: v for k, v in r.items() if k != "rules"} for r in reports]
        with Path(args.json).open("w", encoding="utf-8") as fh:
            json.dump(serializable, fh, indent=2)
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
