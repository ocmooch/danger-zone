"""S2: build trimmed, pseudonymized Sleeper league fixtures from the archive (D-11).

Both code repos are public, so nothing personal may reach ``tests/fixtures``:

* user / league / draft / transaction ids → fake ids in the ``9000…`` range
  (league ``900000000000000001``, draft ``…100001``, users ``9000000001…``,
  transactions ``9000000002…``);
  display names → ``Manager NN``; team names → ``Team NN`` (blank stays blank:
  that is a real scenario); league name → ``Fixture League``;
* dropped: avatars, chat fields (``last_message_*``, ``last_author_*``), the
  NFL.com import metadata, player nicknames (``p_nick_*``, ``player_map``),
  notification preferences, draft reactions;
* kept: roster ids, NFL player ids (public), points, stats, settings.

The pseudonym map lives in memory only. Before writing, every output string is
checked against the real user ids, names and team names held in memory.

Output (fixture names = ``PayloadRequest.fixture_name``, readable by
``FixtureSleeperLeagueSource``):

* ``tests/fixtures/sleeper_league/season_2026/`` — the season to date, trimmed.
* ``tests/fixtures/sleeper_league/synthetic/`` — scenarios the league has not
  produced yet, derived from real rows and marked ``"_synthetic": true``.

    uv run python scripts/sleeper/build_fixtures.py --season 2026
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
from typing import Any

from sqlalchemy.orm import Session

from ff_pipeline.archive.raw_store import RawStore
from ff_pipeline.crawlers.sleeper_league.client import ArchiveSleeperLeagueSource
from ff_pipeline.crawlers.sleeper_league.endpoints import (
    Endpoint,
    PayloadRequest,
    draft_ids,
    draft_requests,
    drafts_request,
    league_request,
    league_requests,
    players_request,
    state_request,
    weeks_to_capture,
)
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.settings import PROJECT_ROOT, get_settings

OUT = PROJECT_ROOT / "tests" / "fixtures" / "sleeper_league"
FAKE_LEAGUE = "900000000000000001"
FAKE_DRAFT = "900000000000100001"
TX_PER_KIND_PER_WEEK = 3
PLAYER_FIELDS = (
    "player_id",
    "first_name",
    "last_name",
    "position",
    "fantasy_positions",
    "team",
    "status",
    "gsis_id",
    "injury_status",
    "years_exp",
)
LEAGUE_FIELDS = (
    "league_id",
    "season",
    "season_type",
    "sport",
    "status",
    "name",
    "previous_league_id",
    "draft_id",
    "total_rosters",
    "roster_positions",
    "scoring_settings",
    "settings",
)


class Pseudonyms:
    """Deterministic fake ids; real values are kept in memory for the leak check."""

    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.tx: dict[str, str] = {}
        self.real_strings: set[str] = set()

    def user(self, real: Any) -> Any:
        if real is None:
            return None
        real = str(real)
        if real not in self.users:
            self.users[real] = f"9000000001{len(self.users) + 1:08d}"
        self.real_strings.add(real)
        return self.users[real]

    def transaction(self, real: Any) -> str:
        real = str(real)
        if real not in self.tx:
            self.tx[real] = f"9000000002{len(self.tx) + 1:08d}"
        return self.tx[real]


def _league(league: dict[str, Any], ps: Pseudonyms) -> dict[str, Any]:
    out = {k: copy.deepcopy(league.get(k)) for k in LEAGUE_FIELDS}
    out["league_id"] = FAKE_LEAGUE
    out["draft_id"] = FAKE_DRAFT if league.get("draft_id") else None
    out["name"] = "Fixture League"
    out["avatar"] = None
    out["metadata"] = {
        k: v for k, v in (league.get("metadata") or {}).items() if k in {"keeper_deadline"}
    }
    ps.real_strings.update(
        str(league[k]) for k in ("league_id", "name", "draft_id") if league.get(k)
    )
    return out


def _users(users: list[dict[str, Any]], ps: Pseudonyms) -> list[dict[str, Any]]:
    out = []
    for i, u in enumerate(sorted(users, key=lambda u: str(u.get("user_id"))), start=1):
        team = ((u.get("metadata") or {}).get("team_name") or "").strip()
        ps.real_strings.update(
            x for x in (u.get("display_name"), team, u.get("user_id")) if x and len(str(x)) > 2
        )
        out.append(
            {
                "user_id": ps.user(u.get("user_id")),
                "display_name": f"Manager {i:02d}",
                "avatar": None,
                "is_owner": u.get("is_owner"),
                "is_bot": u.get("is_bot"),
                "league_id": FAKE_LEAGUE,
                "metadata": {"team_name": f"Team {i:02d}"} if team else {},
            }
        )
    return out


def _rosters(rosters: list[dict[str, Any]], ps: Pseudonyms) -> list[dict[str, Any]]:
    out = []
    for r in rosters:
        meta = r.get("metadata") or {}
        out.append(
            {
                "roster_id": r["roster_id"],
                "owner_id": ps.user(r.get("owner_id")),
                "co_owners": [ps.user(x) for x in r["co_owners"]] if r.get("co_owners") else None,
                "league_id": FAKE_LEAGUE,
                "players": r.get("players"),
                "starters": r.get("starters"),
                "reserve": r.get("reserve"),
                "taxi": r.get("taxi"),
                "keepers": r.get("keepers"),
                "settings": r.get("settings"),
                "metadata": {k: meta[k] for k in ("streak", "record") if k in meta},
            }
        )
    return out


def _transactions(txs: list[dict[str, Any]], ps: Pseudonyms) -> list[dict[str, Any]]:
    kept: dict[tuple[str, str], int] = {}
    out = []
    for t in sorted(txs, key=lambda t: (t.get("created") or 0, str(t.get("transaction_id")))):
        kind = (t.get("type"), t.get("status"))
        if kept.get(kind, 0) >= TX_PER_KIND_PER_WEEK:
            continue
        kept[kind] = kept.get(kind, 0) + 1
        ps.real_strings.add(str(t.get("transaction_id")))
        t2 = copy.deepcopy(t)
        t2["transaction_id"] = ps.transaction(t["transaction_id"])
        t2["creator"] = ps.user(t.get("creator"))
        notes = (t.get("metadata") or {}).get("notes")
        t2["metadata"] = {"notes": notes} if notes else None
        out.append(t2)
    return out


def _draft(d: dict[str, Any], ps: Pseudonyms) -> dict[str, Any]:
    d2 = {
        k: copy.deepcopy(d.get(k))
        for k in ("type", "status", "season", "season_type", "sport", "settings", "start_time")
    }
    d2["draft_id"] = FAKE_DRAFT
    d2["league_id"] = FAKE_LEAGUE
    d2["creators"] = [ps.user(x) for x in d.get("creators") or []]
    if d.get("draft_order"):
        d2["draft_order"] = {ps.user(k): v for k, v in d["draft_order"].items()}
    d2["metadata"] = {k: v for k, v in (d.get("metadata") or {}).items() if k in {"scoring_type"}}
    return d2


def _picks(picks: list[dict[str, Any]], ps: Pseudonyms) -> list[dict[str, Any]]:
    out = []
    for p in picks:
        p2 = {k: copy.deepcopy(v) for k, v in p.items() if k != "reactions"}
        p2["draft_id"] = FAKE_DRAFT
        p2["picked_by"] = ps.user(p.get("picked_by"))
        out.append(p2)
    return out


def _stats(rows: list[dict[str, Any]], keep_ids: set[str], keys: set[str]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        pid = str(r.get("player_id"))
        if pid not in keep_ids:
            continue
        out.append(
            {
                "player_id": pid,
                "week": r.get("week"),
                "season": r.get("season"),
                "season_type": r.get("season_type"),
                "team": r.get("team"),
                "opponent": r.get("opponent"),
                "player": {"position": (r.get("player") or {}).get("position")},
                "stats": {k: v for k, v in (r.get("stats") or {}).items() if k in keys},
            }
        )
    return sorted(out, key=lambda r: r["player_id"])


def build(src: ArchiveSleeperLeagueSource, league_id: str, season: int) -> dict[str, Any]:
    """Return {relative path: payload} for every fixture file."""
    ps = Pseudonyms()

    def j(req: PayloadRequest) -> Any:
        return src.get(req).json()

    state = j(state_request())
    league = j(league_request(league_id))
    weeks = weeks_to_capture(state, league)
    real = {(r.endpoint, r.week): j(r) for r in league_requests(league_id, season, weeks)}
    files: dict[str, Any] = {}

    def put(req: PayloadRequest, payload: Any, scenario: str = "season_2026") -> None:
        files[f"{scenario}/{req.fixture_name}"] = payload

    fake_reqs = {(r.endpoint, r.week): r for r in league_requests(FAKE_LEAGUE, season, weeks)}
    put(state_request(), state)
    put(league_request(FAKE_LEAGUE), _league(league, ps))
    put(fake_reqs[(Endpoint.USERS, None)], _users(real[(Endpoint.USERS, None)], ps))
    rosters = _rosters(real[(Endpoint.ROSTERS, None)], ps)
    put(fake_reqs[(Endpoint.ROSTERS, None)], rosters)
    for ep in (Endpoint.TRADED_PICKS, Endpoint.WINNERS_BRACKET, Endpoint.LOSERS_BRACKET):
        put(fake_reqs[(ep, None)], real[(ep, None)])

    drafts = j(drafts_request(league_id, season)) or []
    put(drafts_request(FAKE_LEAGUE, season), [_draft(d, ps) for d in drafts[:1]])
    ids = draft_ids(league, drafts)
    if ids:
        real_draft, real_picks = (j(r) for r in draft_requests(league_id, season, ids[0]))
        fd, fp = draft_requests(FAKE_LEAGUE, season, FAKE_DRAFT)
        put(fd, _draft(real_draft, ps))
        put(fp, _picks(real_picks, ps))
        ps.real_strings.update(ids)

    scoring = league.get("scoring_settings") or {}
    stat_keys = {k for k, v in scoring.items() if v} | {
        "pts_allow",
        "yds_allow",
        "pass_yd",
        "rush_yd",
        "rec_yd",
        "fgm_50p",
        # per-distance FG makes: the engine scores FGs by range (S6)
        "fgm_0_19",
        "fgm_20_29",
        "fgm_30_39",
        "fgm_40_49",
        "fgm_50_59",
        "fgm_60p",
        "td",
        "gp",
    }
    referenced: set[str] = set()
    for w in range(1, weeks + 1):
        matchups = real[(Endpoint.MATCHUPS, w)]
        put(fake_reqs[(Endpoint.MATCHUPS, w)], matchups)
        week_ids = {str(p) for m in matchups for p in (m.get("players") or [])}
        referenced |= week_ids
        put(
            fake_reqs[(Endpoint.TRANSACTIONS, w)],
            _transactions(real[(Endpoint.TRANSACTIONS, w)], ps),
        )
        put(
            fake_reqs[(Endpoint.STATS_WEEK, w)],
            _stats(real[(Endpoint.STATS_WEEK, w)] or [], week_ids, stat_keys),
        )
    for r in rosters:
        referenced |= {str(p) for p in (r.get("players") or [])}
    for f, payload in files.items():
        if "transactions" in f:
            for t in payload:
                referenced |= set((t.get("adds") or {}).keys()) | set((t.get("drops") or {}).keys())
    directory = j(players_request()) or {}
    put(
        players_request(),
        {
            pid: {k: directory[pid].get(k) for k in PLAYER_FIELDS}
            for pid in sorted(referenced)
            if pid in directory
        },
    )

    _synthetic(files, fake_reqs, rosters, ps)
    _leak_check(files, ps)
    return files


def _synthetic(
    files: dict[str, Any],
    fake_reqs: dict[tuple[Endpoint, int | None], PayloadRequest],
    rosters: list[dict[str, Any]],
    ps: Pseudonyms,
) -> None:
    """Scenarios the real league has not produced yet, derived from real rows."""
    base = "season_2026"
    # trade with a draft pick + FAAB, between rosters 1 and 2 (week 1)
    r1, r2 = rosters[0], rosters[1]
    p1, p2 = (r1.get("players") or ["0"])[-1], (r2.get("players") or ["0"])[-1]
    trade = {
        "_synthetic": True,
        "type": "trade",
        "status": "complete",
        "transaction_id": "900000009999999901",
        "leg": 1,
        "roster_ids": [r1["roster_id"], r2["roster_id"]],
        "consenter_ids": [r1["roster_id"], r2["roster_id"]],
        "creator": r1["owner_id"],
        "adds": {p1: r2["roster_id"], p2: r1["roster_id"]},
        "drops": {p1: r1["roster_id"], p2: r2["roster_id"]},
        "draft_picks": [
            {
                "season": "2027",
                "round": 2,
                "roster_id": r1["roster_id"],
                "previous_owner_id": r1["roster_id"],
                "owner_id": r2["roster_id"],
            }
        ],
        "waiver_budget": [{"sender": r2["roster_id"], "receiver": r1["roster_id"], "amount": 7}],
        "settings": None,
        "metadata": None,
    }
    tx_req = fake_reqs[(Endpoint.TRANSACTIONS, 1)]
    files[f"synthetic/{tx_req.fixture_name}"] = [trade, *files[f"{base}/{tx_req.fixture_name}"]]
    # commissioner custom_points on one team-week (week 1)
    m_req = fake_reqs[(Endpoint.MATCHUPS, 1)]
    matchups = copy.deepcopy(files[f"{base}/{m_req.fixture_name}"])
    matchups[0]["custom_points"] = round(float(matchups[0]["points"]) + 5.0, 2)
    matchups[0]["_synthetic"] = True
    files[f"synthetic/{m_req.fixture_name}"] = matchups
    # a co-owner on roster 1
    ro_req = fake_reqs[(Endpoint.ROSTERS, None)]
    co = copy.deepcopy(rosters)
    co[0]["co_owners"] = [co[1]["owner_id"]]
    co[0]["_synthetic"] = True
    files[f"synthetic/{ro_req.fixture_name}"] = co
    _ = ps


def _leak_check(files: dict[str, Any], ps: Pseudonyms) -> None:
    blob = json.dumps(files)
    leaks = sorted(s for s in ps.real_strings if s and s in blob)
    if leaks:
        raise SystemExit(f"refusing to write fixtures: {len(leaks)} real value(s) leaked")
    for long_id in re.findall(r"\d{15,}", blob):
        if not long_id.startswith("9000"):
            raise SystemExit("refusing to write fixtures: a non-fake 15+ digit id remains")


README = """# Sleeper league fixtures (S2, D-11)

Built by `scripts/sleeper/build_fixtures.py` from the archived 2026 capture. Pseudonymized:
fake ids (`9000…`), `Manager NN` / `Team NN`, no avatars, chat, nicknames or import metadata.
File names = `PayloadRequest.fixture_name` (read with `FixtureSleeperLeagueSource`).

- `season_2026/` — the real season to date (league id `900000000000000001`), trimmed:
  {tx} transactions per (type, status) per week; stats only for players in that week's
  matchups and only scoring-relevant keys; players directory only for referenced players.
  Covers: regular weeks, bye weeks, failed + complete waivers, free-agent adds, a blank team
  name, the DEF slot, IR (reserve) players, pre-generated brackets, a snake draft.
- `synthetic/` — files that **replace** their `season_2026/` namesake for scenarios the league
  has not produced yet; every changed object carries `"_synthetic": true`:
  a trade with a draft pick + FAAB (week 1 transactions), commissioner `custom_points`
  (week 1 matchups), a co-owner (rosters).
- Not built yet: a playoff week (captured for real after week 15; bracket semantics are
  unverified until games are decided, so no synthetic version is made).
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Build pseudonymized Sleeper league fixtures.")
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args()
    settings = get_settings()
    league_id = settings.require_sleeper_league_id()
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            files = build(
                ArchiveSleeperLeagueSource(ss, RawStore(settings.sleeper_raw_dir)),
                league_id,
                args.season,
            )
    finally:
        engine.dispose()
    for sub in ("season_2026", "synthetic"):
        shutil.rmtree(OUT / sub, ignore_errors=True)
    for rel, payload in sorted(files.items()):
        path = OUT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    (OUT / "README.md").write_text(README.format(tx=TX_PER_KIND_PER_WEEK))
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"wrote {len(files)} fixture files, {total / 1024:.0f} KiB")


if __name__ == "__main__":
    main()
