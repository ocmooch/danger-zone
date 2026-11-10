"""Sleeper payloads → canonical DTOs (S5), on the pseudonymized 2026 fixtures."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ff_pipeline.crawlers.sleeper_league.archive_view import load_league
from ff_pipeline.crawlers.sleeper_league.client import FixtureSleeperLeagueSource
from ff_pipeline.normalizer.sleeper_league import (
    IdentityMaps,
    NormalizeError,
    normalize,
    team_key,
)

if TYPE_CHECKING:
    from ff_pipeline.canonical.dto import SeasonBundle
    from ff_pipeline.crawlers.sleeper_league.archive_view import ArchivedLeague
    from ff_pipeline.crawlers.sleeper_league.client import FetchedPayload
    from ff_pipeline.crawlers.sleeper_league.endpoints import PayloadRequest

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sleeper_league"
LEAGUE = "900000000000000001"


@dataclass(frozen=True)
class OverlaySource:
    """Synthetic scenario files replace their season_2026 namesakes."""

    synthetic: bool = False

    def get(self, request: PayloadRequest) -> FetchedPayload:
        if self.synthetic and (FX / "synthetic" / request.fixture_name).exists():
            return FixtureSleeperLeagueSource(FX / "synthetic").get(request)
        return FixtureSleeperLeagueSource(FX / "season_2026").get(request)


def fixture_league(*, synthetic: bool = False) -> ArchivedLeague:
    return load_league(OverlaySource(synthetic), LEAGUE)


def fixture_ids(data: ArchivedLeague, **kw: object) -> IdentityMaps:
    users = sorted(str(u["user_id"]) for u in data.users)
    owner_by_user = {u: 101 + i for i, u in enumerate(users)}
    return IdentityMaps(
        owner_by_user=owner_by_user,
        owner_names={o: f"Owner {o}" for o in owner_by_user.values()},
        owner_abbrev={o: str(o - 100) for o in owner_by_user.values()},
        player_ids={
            sid: 10_000 + i
            for i, sid in enumerate(
                sorted(set(data.players) | {str(p["player_id"]) for p in data.draft_picks})
            )
        },
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture(scope="module")
def data() -> ArchivedLeague:
    return fixture_league()


@pytest.fixture(scope="module")
def bundle(data: ArchivedLeague) -> SeasonBundle:
    return normalize(data, fixture_ids(data))


def test_season(bundle: SeasonBundle) -> None:
    s = bundle.season
    assert (s.year, s.status, s.regular_season_weeks, s.playoff_weeks) == (
        2026,
        "in_progress",
        14,
        3,
    )
    assert s.last_finalized_week == 8
    assert s.platform_league_id == LEAGUE


def test_teams_names_draft_slots_and_platform_record(
    data: ArchivedLeague, bundle: SeasonBundle
) -> None:
    assert len(bundle.teams) == 12
    assert len({t.team_name for t in bundle.teams}) == 12
    blank = [t for t in bundle.teams if t.team_name.endswith("'s Team")]
    assert len(blank) == 1  # the fixture's blank Sleeper team name → owner-name fallback
    assert re.fullmatch(r"Owner \d+'s Team", blank[0].team_name)
    assert sorted(t.draft_position for t in bundle.teams) == list(range(1, 13))
    assert all(t.platform_record is not None for t in bundle.teams)
    assert {t.team_abbrev for t in bundle.teams} == {str(i) for i in range(1, 13)}
    _ = data


def test_matchups_pair_up_and_finalization(bundle: SeasonBundle) -> None:
    assert len(bundle.matchups) == 9 * 12
    by = {(m.week, m.team_key): m for m in bundle.matchups}
    for m in bundle.matchups:
        assert m.opponent_key is not None
        opp = by[(m.week, m.opponent_key)]
        assert opp.opponent_key == m.team_key
        assert (opp.team_score, opp.opponent_score) == (m.opponent_score, m.team_score)
    assert all(m.finalized for m in bundle.matchups if m.week <= 8)
    assert not any(m.finalized for m in bundle.matchups if m.week == 9)


def test_custom_points_override_the_score() -> None:
    data = fixture_league(synthetic=True)
    b = normalize(data, fixture_ids(data))
    overridden = [m for m in b.matchups if "custom_points" in m.extra_data]
    assert len(overridden) == 1
    m = overridden[0]
    assert m.team_score == m.extra_data["custom_points"]
    assert m.team_score != m.extra_data["points_official"]


def test_weekly_rosters_one_row_per_player_and_slots(bundle: SeasonBundle) -> None:
    per_week = Counter((r.week, r.player_id) for r in bundle.rosters)
    assert max(per_week.values()) == 1
    weekly = [r for r in bundle.rosters if r.week >= 1]
    starters = Counter((r.week, r.team_key) for r in weekly if r.is_starter)
    assert set(starters.values()) == {9}
    week1 = sorted(
        (r for r in weekly if r.week == 1 and r.team_key == team_key(1) and r.is_starter),
        key=lambda r: ["QB", "RB", "WR", "TE", "R/W/T", "K", "DEF"].index(r.roster_slot),
    )
    assert Counter(r.roster_slot for r in week1) == Counter(
        {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "R/W/T": 1, "K": 1, "DEF": 1}
    )
    bench = [r for r in weekly if not r.is_starter]
    assert all(r.roster_slot == "BN" for r in bench)
    assert all(r.extra_data.get("slot_certainty") == "bench_or_reserve" for r in bench)
    assert all("platform_points" in r.extra_data for r in weekly)
    assert sum(1 for r in bundle.rosters if r.week == 0) == 180


def test_known_reserve_list_makes_res_rows(data: ArchivedLeague) -> None:
    rosters = {int(r["roster_id"]): r for r in data.rosters}
    week9 = {rid: {str(p) for p in (r.get("reserve") or [])} for rid, r in rosters.items()}
    b = normalize(data, fixture_ids(data, reserve_by_week={9: week9}))
    res9 = [r for r in b.rosters if r.week == 9 and r.roster_slot == "RES"]
    assert len(res9) == sum(len(v) for v in week9.values()) > 0
    assert not any("slot_certainty" in r.extra_data for r in b.rosters if r.week == 9)


def test_transaction_legs(bundle: SeasonBundle) -> None:
    failed = [t for t in bundle.transactions if t.status == "failed"]
    assert failed and all(
        t.transaction_type == "waiver_add" and t.direction == "in" for t in failed
    )
    failed_ids = {t.platform_transaction_id for t in failed}
    assert not any(
        t.direction == "out" and t.platform_transaction_id in failed_ids
        for t in bundle.transactions
    )
    waivers = [t for t in bundle.transactions if t.transaction_type == "waiver_add"]
    assert all(isinstance((t.extra_data or {}).get("faab_bid"), int) for t in waivers)
    draft = sorted(
        (t for t in bundle.transactions if t.transaction_type == "draft"),
        key=lambda t: t.executed_at or 0,
    )
    assert len(draft) == 180
    assert draft[0].notes == "Round 1, Pick 1 (overall 1)"
    assert draft[12].notes == "Round 2, Pick 1 (overall 13)"
    assert {t.effective_week for t in draft} == {0}
    assert len({t.key for t in bundle.transactions}) == len(bundle.transactions)


def test_trade_legs_with_counterparts() -> None:
    data = fixture_league(synthetic=True)
    b = normalize(data, fixture_ids(data))
    trade = [t for t in b.transactions if t.transaction_type == "trade"]
    assert len(trade) == 4  # two players, an in and an out leg each
    for leg in trade:
        assert leg.counterpart_key is not None and leg.counterpart_key != leg.team_key
    ins = {(t.player_id, t.team_key) for t in trade if t.direction == "in"}
    outs = {(t.player_id, t.counterpart_key) for t in trade if t.direction == "out"}
    assert ins == outs


def test_unmapped_user_fails_loudly(data: ArchivedLeague) -> None:
    ids = fixture_ids(data)
    first = next(iter(ids.owner_by_user))
    broken = IdentityMaps(
        owner_by_user={k: v for k, v in ids.owner_by_user.items() if k != first},
        owner_names=ids.owner_names,
        owner_abbrev=ids.owner_abbrev,
        player_ids=ids.player_ids,
    )
    with pytest.raises(NormalizeError, match="owners map"):
        normalize(data, broken)


def test_fixture_directory_is_what_the_tests_assume() -> None:
    d = json.loads((FX / "season_2026" / "v1_players_nfl.json").read_text())
    assert sum(1 for k in d if not k.isdigit()) == 28  # team defenses
