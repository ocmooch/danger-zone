"""S2 probe: check the ⚑ items of docs/sleeper/04_SLEEPER_FIELD_MAPPING.md against
the archived Sleeper league payloads.

Read-only and offline: reads ``source_payloads`` + the raw store (no Sleeper
calls). ``--schedules`` additionally loads nflverse schedules (network) for
the points-allowed check. Prints a markdown evidence report on stdout with
keys, values and counts only — never names, user ids or chat text, so the
output is safe to paste into the public docs.

    uv run python scripts/sleeper/probe_field_mapping.py --season 2026 --schedules
"""

from __future__ import annotations

import argparse
import collections
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.archive.raw_store import RawStore
from ff_pipeline.crawlers.sleeper_league.client import (
    ArchiveMissError,
    ArchiveSleeperLeagueSource,
)
from ff_pipeline.crawlers.sleeper_league.endpoints import (
    Endpoint,
    PayloadRequest,
    drafts_request,
    league_request,
    league_requests,
    state_request,
    weeks_to_capture,
)
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.models import SourcePayload
from ff_pipeline.settings import get_settings

OFFENSE = {"QB", "RB", "WR", "TE", "K"}
# Sleeper DEF ids that differ from nflverse team abbreviations.
NFLVERSE_TEAM = {"LAR": "LA"}


def _out(line: str = "") -> None:
    print(line)  # this script's product is its stdout report


class Archive:
    def __init__(self, ss: Session, store: RawStore) -> None:
        self.src = ArchiveSleeperLeagueSource(ss, store)

    def json(self, req: PayloadRequest) -> Any:
        try:
            return self.src.get(req).json()
        except ArchiveMissError:
            return None


def _league_id(ss: Session, season: int) -> str:
    lid = ss.scalar(
        select(SourcePayload.platform_league_id)
        .where(SourcePayload.endpoint == "league", SourcePayload.season_year == season)
        .order_by(SourcePayload.last_fetched_at.desc())
        .limit(1)
    )
    if lid is None:
        raise SystemExit(f"no archived league payload for season {season}; run capture first")
    return lid


def probe(arc: Archive, league_id: str, season: int, schedules: bool) -> None:
    state = arc.json(state_request()) or {}
    league = arc.json(league_request(league_id)) or {}
    weeks = weeks_to_capture(state, league)
    by_ep: dict[tuple[Endpoint, int | None], Any] = {
        (r.endpoint, r.week): arc.json(r) for r in league_requests(league_id, season, weeks)
    }
    s = league.get("settings") or {}
    sc: dict[str, float] = league.get("scoring_settings") or {}

    _out(f"## Probe evidence — season {season}, weeks 1..{weeks} (archive replay)\n")
    _out("### League settings")
    for k in (
        "start_week",
        "playoff_week_start",
        "playoff_teams",
        "playoff_type",
        "playoff_round_type",
        "playoff_seed_type",
        "waiver_type",
        "waiver_budget",
        "waiver_bid_min",
        "trade_deadline",
        "reserve_slots",
        "taxi_slots",
        "type",
        "best_ball",
        "max_keepers",
        "draft_rounds",
        "last_scored_leg",
        "divisions",
    ):
        _out(f"- `settings.{k}` = `{s.get(k)!r}`")
    _out(f"- `roster_positions` = `{league.get('roster_positions')}`")
    meta = league.get("metadata") or {}
    _out(f"- `metadata` keys = `{sorted(meta)}`")
    _out(f"- division keys in metadata: `{sorted(k for k in meta if k.startswith('division'))}`")

    users = by_ep.get((Endpoint.USERS, None)) or []
    rosters = by_ep.get((Endpoint.ROSTERS, None)) or []
    _out("\n### Users / rosters")
    _out(
        f"- users: {len(users)}; `is_owner` values: {dict(_count(u.get('is_owner') for u in users))}"
    )
    _out(
        f"- rosters: {len(rosters)}; with co_owners: {sum(1 for r in rosters if r.get('co_owners'))};"
        f" with reserve (IR): {sum(1 for r in rosters if r.get('reserve'))};"
        f" with taxi: {sum(1 for r in rosters if r.get('taxi'))}"
    )
    rmeta = [r.get("metadata") or {} for r in rosters]
    _out(
        f"- `metadata.streak` examples: {sorted({m.get('streak') for m in rmeta if m.get('streak')})[:4]}"
    )
    rec = [m.get("record") for m in rmeta if m.get("record")]
    _out(
        f"- `metadata.record`: W/L string, one char per scored week (len {sorted({len(x) for x in rec})})"
    )
    _out(f"- `settings` keys = `{sorted((rosters[0].get('settings') or {}) if rosters else [])}`")
    blank = sum(1 for u in users if not ((u.get("metadata") or {}).get("team_name") or "").strip())
    _out(f"- users with blank `metadata.team_name`: {blank}")

    _out("\n### Matchups / slots")
    slot_counts: collections.Counter[int] = collections.Counter()
    custom = 0
    for w in range(1, weeks + 1):
        for m in by_ep.get((Endpoint.MATCHUPS, w)) or []:
            slot_counts[len(m.get("starters") or [])] += 1
            custom += m.get("custom_points") is not None
    _out(f"- starters per team-week: {dict(slot_counts)}; non-null `custom_points`: {custom}")

    _out("\n### Transactions")
    txs = [t for w in range(1, weeks + 1) for t in (by_ep.get((Endpoint.TRANSACTIONS, w)) or [])]
    _out(
        f"- {len(txs)} rows; (type, status): {dict(_count((t['type'], t['status']) for t in txs))}"
    )
    bids = [(t.get("settings") or {}).get("waiver_bid") for t in txs if t["type"] == "waiver"]
    _out(f"- `settings.waiver_bid` python types: {dict(_count(type(b).__name__ for b in bids))}")
    _out(
        f"- trades with draft_picks: {sum(1 for t in txs if t.get('draft_picks'))};"
        f" with waiver_budget (FAAB) legs: {sum(1 for t in txs if t.get('waiver_budget'))}"
    )

    _out("\n### Scoring: `players_points` vs Σ stats x scoring_settings")
    stats_by_week = {
        w: {r["player_id"]: r for r in (by_ep.get((Endpoint.STATS_WEEK, w)) or [])}
        for w in range(1, weeks + 1)
    }
    total = mismatched = 0
    for w in range(1, weeks + 1):
        for m in by_ep.get((Endpoint.MATCHUPS, w)) or []:
            for pid, pts in (m.get("players_points") or {}).items():
                st = (stats_by_week[w].get(pid) or {}).get("stats") or {}
                calc = sum(v * sc.get(k, 0) for k, v in st.items() if isinstance(v, int | float))
                total += 1
                mismatched += abs(round(calc, 2) - pts) > 0.011
    _out(f"- rostered player-weeks: {total}; mismatches: {mismatched}")

    _out("\n### Bonus stacking (player rows only; D-10 evidence, decided in S6)")
    players = [
        r["stats"]
        for w in stats_by_week.values()
        for pid, r in w.items()
        if pid.isdigit() and (r.get("player") or {}).get("position") in OFFENSE
    ]
    checks: list[tuple[str, str, str, list[str]]] = [
        ("pass 400+ yd", "pass_yd", "400", ["bonus_pass_yd_300", "bonus_pass_yd_400"]),
        ("pass 300-399 yd", "pass_yd", "300-399", ["bonus_pass_yd_300", "bonus_pass_yd_400"]),
        ("rush 200+ yd", "rush_yd", "200", ["bonus_rush_yd_100", "bonus_rush_yd_200"]),
        ("rec 200+ yd", "rec_yd", "200", ["bonus_rec_yd_100", "bonus_rec_yd_200"]),
        ("pass TD 50+", "pass_td_50p", ">0", ["pass_td_40p", "pass_td_50p"]),
        ("rush TD 50+", "rush_td_50p", ">0", ["rush_td_40p", "rush_td_50p"]),
        ("rec TD 50+", "rec_td_50p", ">0", ["rec_td_40p", "rec_td_50p"]),
        ("FG 50+", "fgm_50p", ">0", ["fgm", "fgm_50_59", "fgm_60p", "fgm_50p"]),
    ]
    _out("| case | n | stat keys present (value) x count |")
    _out("|---|---|---|")
    for label, key, cond, keys in checks:
        hits = [p for p in players if _match(p.get(key) or 0, cond)]
        combos = _count(tuple((k, p.get(k)) for k in keys if p.get(k)) for p in hits)
        shown = "; ".join(f"{dict(c)} x{n}" for c, n in combos.most_common(4)) or "—"
        _out(f"| {label} | {len(hits)} | {shown} |")
    _out(f"- scoring keys used for FG: `{ {k: v for k, v in sc.items() if k.startswith('fg')} }`")
    _out(
        "- special-teams keys in scoring: "
        f"`{ {k: v for k, v in sc.items() if k in {'st_td', 'kr_td', 'pr_td', 'def_st_td', 'def_td'}} }`"
    )

    if schedules:
        _points_allowed(stats_by_week, season)

    _out("\n### Brackets")
    for ep in (Endpoint.WINNERS_BRACKET, Endpoint.LOSERS_BRACKET):
        br = by_ep.get((ep, None)) or []
        placements = sorted((g.get("p"), g.get("r"), g.get("m")) for g in br if g.get("p"))
        decided = sum(1 for g in br if g.get("w"))
        _out(
            f"- `{ep.value}`: {len(br)} games; (p, r, m) placement games: {placements}; decided: {decided}"
        )

    drafts = arc.json(drafts_request(league_id, season)) or []
    _out("\n### Draft")
    _out(f"- drafts: {[(d.get('type'), d.get('status')) for d in drafts]}")


def _points_allowed(stats_by_week: dict[int, dict[str, Any]], season: int) -> None:
    from ff_pipeline.crawlers.nflverse.client import LiveNflverseSource

    final: dict[tuple[int, str], tuple[int, str]] = {}
    for g in LiveNflverseSource().load_schedules([season]).to_dicts():
        if g.get("game_type") != "REG" or g.get("home_score") is None:
            continue
        final[(g["week"], g["home_team"])] = (g["away_score"], g["away_team"])
        final[(g["week"], g["away_team"])] = (g["home_score"], g["home_team"])
    back = {v: k for k, v in NFLVERSE_TEAM.items()}
    n = exact = explained = 0
    other: list[str] = []
    for w, rows in stats_by_week.items():
        for pid, r in rows.items():
            st = r.get("stats") or {}
            if pid.startswith("TEAM_") or pid.isdigit() or "pts_allow" not in st:
                continue
            key = (w, NFLVERSE_TEAM.get(pid, pid))
            if key not in final:
                continue
            score, opp = final[key]
            n += 1
            gap = score - st["pts_allow"]
            if gap == 0:
                exact += 1
                continue
            opp_def = (rows.get(back.get(opp, opp)) or {}).get("stats") or {}
            if gap == 6 * (opp_def.get("def_td") or 0) + 2 * (opp_def.get("safe") or 0):
                explained += 1
            else:
                other.append(f"w{w} gap {gap}")
    _out("\n### Points allowed (`pts_allow`) vs nflverse final score")
    _out(f"- DEF-weeks: {n}; equal to opponent final score: {exact}")
    _out(
        f"- lower by exactly 6 x opponent `def_td` + 2 x opponent `safe`: {explained}"
        f"; unexplained: {len(other)} {other[:5]}"
    )


def _match(v: float, cond: str) -> bool:
    if cond == ">0":
        return v > 0
    if "-" in cond:
        lo, hi = (float(x) for x in cond.split("-"))
        return lo <= v <= hi
    return v >= float(cond)


def _count(items: Any) -> collections.Counter[Any]:
    return collections.Counter(items)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--schedules", action="store_true", help="load nflverse schedules (network)")
    args = ap.parse_args()
    settings = get_settings()
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            arc = Archive(ss, RawStore(settings.sleeper_raw_dir))
            probe(arc, _league_id(ss, args.season), args.season, args.schedules)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
