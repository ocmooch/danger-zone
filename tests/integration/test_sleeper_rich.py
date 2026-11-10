"""S7 facets end to end on the sealed fixture DB: decided brackets → places and
season results, traded picks, status snapshots, avatars."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.canonical.loader import load_season
from ff_pipeline.crawlers.sleeper_league.avatars import download_avatars
from ff_pipeline.crawlers.sleeper_league.runner import build_identity_maps
from ff_pipeline.legacy.verify import connect, verify
from ff_pipeline.normalizer.sleeper_league import avatar_urls, normalize
from ff_pipeline.normalizer.sleeper_status import store_status_snapshots
from ff_pipeline.repository.models import Asset, PlayerStatusSnapshot, Team, TradedPick
from tests.integration.test_sleeper_load import FRANCHISE, _load, db, engine  # noqa: F401
from tests.unit.test_sleeper_brackets import _bracket
from tests.unit.test_sleeper_normalizer import fixture_league

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from ff_pipeline.crawlers.sleeper_league.archive_view import ArchivedLeague

pytestmark = pytest.mark.integration


def _decided(data: ArchivedLeague) -> ArchivedLeague:
    """Weeks 6-8 become the playoffs; every game goes to the higher real score."""
    score = {
        (int(m["roster_id"]), w): float(m.get("points") or 0)
        for w, ms in data.matchups_by_week.items()
        for m in ms
    }
    by_wins = sorted(range(1, 13), key=lambda r: -sum(score[(r, w)] for w in range(1, 6)))

    def bracket(seeds: list[int]) -> list[dict[str, Any]]:
        b = _bracket(seeds, {})
        by = {m["m"]: m for m in b}
        for m in sorted(b, key=lambda x: x["r"]):
            for side in ("t1", "t2"):
                frm = m.get(f"{side}_from")
                if frm:
                    ((kind, src),) = frm.items()
                    m[side] = by[src].get(kind)
            wk = 5 + m["r"]
            a, c = m["t1"], m["t2"]
            m["w"], m["l"] = (a, c) if score[(a, wk)] >= score[(c, wk)] else (c, a)
        return b

    settings = {**data.league["settings"], "playoff_week_start": 6}
    return replace(
        data,
        league={**data.league, "settings": settings},
        winners_bracket=bracket(by_wins[:6]),
        losers_bracket=bracket(by_wins[6:]),
    )


def _load_bundle(engine: Engine, data: ArchivedLeague) -> Any:  # noqa: F811
    with Session(engine) as ss:
        ids, _ = build_identity_maps(ss, data, league_id=FRANCHISE)
        res = load_season(ss, normalize(data, ids), league_id=FRANCHISE)
        ss.commit()
    return res


def test_decided_brackets_write_places_results_and_flags(db: Path, engine: Engine) -> None:  # noqa: F811
    _load(engine)
    data = _decided(fixture_league())
    _load_bundle(engine, data)
    c = sqlite3.connect(db)
    try:
        ranks = c.execute(
            "SELECT final_rank, playoff_finish, made_playoffs FROM teams WHERE season_id = 3"
            " ORDER BY final_rank"
        ).fetchall()
        assert [r[0] for r in ranks] == list(range(1, 13))
        assert all(r[0] == r[1] for r in ranks)
        assert [r[2] for r in ranks] == [1] * 6 + [0] * 6
        champ, runner, last = c.execute(
            "SELECT champion_team_id, runner_up_team_id, last_place_team_id FROM seasons"
            " WHERE season_id = 3"
        ).fetchone()
        rank_of = dict(c.execute("SELECT team_id, final_rank FROM teams WHERE season_id = 3"))
        assert (rank_of[champ], rank_of[runner], rank_of[last]) == (1, 2, 12)
        flags = c.execute(
            "SELECT week, sum(is_playoff), sum(is_consolation) FROM matchups"
            " WHERE season_id = 3 AND week BETWEEN 6 AND 8 GROUP BY week"
        ).fetchall()
        assert flags == [(6, 8, 4), (7, 12, 6), (8, 8, 4)]
        roles = c.execute(
            "SELECT json_extract(extra_data, '$.postseason_role'), count(*) FROM matchups"
            " WHERE season_id = 3 AND week BETWEEN 6 AND 8 AND is_playoff = 0 GROUP BY 1"
        ).fetchall()
        assert sorted(roles) == [("bye", 4), ("placed", 4)]
        assert c.execute(
            "SELECT count(*) FROM playoff_brackets WHERE season_id = 3 AND winner_team_id"
            " IS NOT NULL"
        ).fetchone() == (14,)
    finally:
        c.close()
    again = _load_bundle(engine, data)
    for name, cnt in again.counts.items():
        assert (cnt.inserted, cnt.updated, cnt.deleted) == (0, 0, 0), name
    conn = connect(str(db))
    try:
        assert verify(conn, full=True).ok
    finally:
        conn.close()


def test_traded_pick_current_owner(engine: Engine) -> None:  # noqa: F811
    data = fixture_league(synthetic=True)
    _load(engine)
    res = _load_bundle(engine, data)
    assert res.counts["traded_picks"].inserted == 1
    with Session(engine) as ss:
        owner = {t.platform_team_key: t.owner_id for t in ss.scalars(select(Team))}
        pick = ss.scalars(select(TradedPick)).one()
        assert (pick.pick_season, pick.round) == (2027, 2)
        assert pick.original_owner_id == owner["sleeper:roster:1"]
        assert pick.current_owner_id == owner["sleeper:roster:2"]
        assert pick.source_transaction_id == "900000009999999901"
    # the pick goes back to its original owner: the row is removed
    back = replace(data, traded_picks=[{**data.traded_picks[0], "owner_id": 1}])
    assert _load_bundle(engine, back).counts["traded_picks"].deleted == 1


def test_status_snapshots_are_idempotent_per_day(engine: Engine) -> None:  # noqa: F811
    data = fixture_league()
    _load(engine)
    with Session(engine) as ss:
        ids, _ = build_identity_maps(ss, data, league_id=FRANCHISE)
        day = date(2026, 11, 8)
        store_status_snapshots(ss, [(day, data.players)], player_ids=ids.player_ids)
        ss.commit()
        n = ss.scalar(select(func.count()).select_from(PlayerStatusSnapshot))
        assert n and n > 100
        store_status_snapshots(ss, [(day, data.players)], player_ids=ids.player_ids)
        store_status_snapshots(ss, [(date(2026, 11, 15), data.players)], player_ids=ids.player_ids)
        ss.commit()
        assert ss.scalar(select(func.count()).select_from(PlayerStatusSnapshot)) == 2 * n


class _Fetcher:
    def __init__(self) -> None:
        self.calls = 0

    def get_html(self, _url: str) -> str:
        return ""

    def get_bytes(self, _url: str) -> tuple[bytes, str | None]:
        self.calls += 1
        return b"\x89PNG same image", "image/png"


def test_avatars_dedupe_and_link_both_teams(tmp_path: Path, engine: Engine) -> None:  # noqa: F811
    data = fixture_league()
    users = [dict(u) for u in data.users]
    for u in users[:2]:  # two owners, the same (default) avatar
        u["avatar"] = "f00d"
    data = replace(data, users=users)
    _load(engine)
    fetch = _Fetcher()
    with Session(engine) as ss:
        out = download_avatars(
            ss,
            fetch,
            avatar_urls(data),
            league_id=FRANCHISE,
            assets_root=tmp_path,  # type: ignore[arg-type]
        )
        ss.commit()
        assert (out.stored, fetch.calls) == (1, 1)
        again = download_avatars(
            ss,
            fetch,
            avatar_urls(data),
            league_id=FRANCHISE,
            assets_root=tmp_path,  # type: ignore[arg-type]
        )
        assert (again.already, fetch.calls) == (1, 1)
    _load_bundle(engine, data)
    with Session(engine) as ss:
        asset = ss.scalars(select(Asset).where(Asset.kind == "owner_avatar")).one()
        linked = ss.scalars(select(Team).where(Team.owner_avatar_asset_id == asset.asset_id)).all()
        assert len(linked) == 2
        assert (tmp_path / asset.storage_path).exists()
