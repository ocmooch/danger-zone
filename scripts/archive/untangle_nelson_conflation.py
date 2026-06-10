"""One-off repair: untangle the J.J. / Jordy Nelson identity conflation, then
fold the held-back abbreviated roster stub into the cleaned J.J. Nelson row.

Background
----------
This resolves the case ``merge_roster_name_stubs.py`` deliberately held back
(``NEEDS_REVIEW_STUBS``) because its obvious canonical target was itself
already corrupt. Two real players were tangled across three rows:

* **17322 "J.J. Nelson"** (legal *Jamarcus*, ``gsis 00-0032112``) correctly
  carries J.J.'s nflverse stats (``player_stats_raw`` 2015-2019,
  ``player_stats_scored``) — but it was *also* stamped with
  ``nfl_com_player_id 1032``, which is **Jordy** Nelson's NFL.com id. Every
  NFL.com-sourced fantasy row that resolves through id ``1032`` therefore
  piled onto J.J.'s row: ``team_rosters`` (117 rows, 2010-2018) and
  ``transactions`` (7), i.e. Jordy's whole league history.
* **17326 "Jordy Nelson"** (``gsis 00-0026176``) correctly carries Jordy's
  nflverse stats — but had **no** ``nfl_com_player_id`` and **zero** fantasy
  rows: its NFL.com side had been hijacked by 17322.
* **26662 stub "J. Nelson"** (``nfl_com 2552656`` — J.J.'s real NFL.com id)
  holds J.J.'s actual 2016-2017 Arizona fantasy weeks (``team_rosters`` 13,
  ``transactions`` 1) as a statless stub.

The nflverse stats were never conflated — they are already split cleanly by
``gsis_id``. The whole defect is on the NFL.com side: id ``1032``'s pile is on
the wrong canonical row. So the fix is a provenance repoint, not per-week
surgery.

What this does (one transaction)
--------------------------------
1. Move the NFL.com-sourced rows that arrived via id ``1032``
   (``team_rosters`` + ``transactions``) from 17322 -> 17326. The nflverse
   gsis-keyed stat tables stay on 17322 (they are J.J.'s and already right).
2. Move ``nfl_com_player_id 1032`` off 17322 onto 17326, and seed
   ``player_id_overrides`` (``1032`` -> 17326) so future NFL.com syncs resolve
   Jordy directly.
3. Fold stub 26662 into the now-clean 17322 through the proven
   ``_apply_merge`` path: repoint the stub's fantasy rows, stamp its
   ``nfl_com 2552656`` onto 17322, seed an override, delete the stub.
4. ``recompute_rostered_spans``. Final spans: Jordy 17326 = 2010-2018,
   J.J. 17322 = 2016-2017.

The collision-free guarantee: 17326 starts with zero ``team_rosters`` /
``transactions`` rows, and step 1 empties 17322's, so neither the
``(season_year, week, player_id)`` uniqueness on ``team_rosters`` nor any
other key can clash.

Usage
-----
    uv run python scripts/untangle_nelson_conflation.py            # dry run
    uv run python scripts/untangle_nelson_conflation.py --apply    # commit

``--apply`` snapshots the SQLite file to ``data/backups/`` first.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

# Reuse the proven, side-effect-tested internals so this repair repoints FKs,
# deletes the stub and stamps IDs identically to the heuristic merger.
from merge_roster_name_stubs import _seed_override
from merge_split_player_identities import _apply_merge, _fk_tables, _load_players
from sqlalchemy import text
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.settings import get_settings

# Canonical row ids (verified by hand against gsis + NFL.com id + career era).
JJ = 17322  # J.J. Nelson  (legal Jamarcus, gsis 00-0032112, ARI 2015-2017)
JORDY = 17326  # Jordy Nelson (gsis 00-0026176, GB 2008-2017 / OAK 2018)
STUB = 26662  # "J. Nelson" stub, nfl_com 2552656 = J.J.'s real NFL.com id

JORDY_NFL_COM = "1032"  # mis-stamped on JJ; actually Jordy's NFL.com id
JJ_NFL_COM = "2552656"  # currently on the stub; J.J.'s real NFL.com id

# NFL.com-sourced tables whose rows arrived via id 1032 and so belong to Jordy.
# (Deliberately excludes the nflverse gsis-keyed player_stats_raw /
# player_stats_scored, and the Sleeper-keyed projections — those stay on JJ.)
MOVE_TABLES = ("team_rosters", "transactions")


def _count(conn, table: str, pid: int) -> int:
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE player_id = :pid"), {"pid": pid}
    ).scalar_one()


def _validate(conn) -> list[str]:
    """Assert the DB is in the exact pre-repair shape this script expects.
    Returns a list of problems (empty == safe to apply)."""
    problems: list[str] = []
    rows = {
        r["player_id"]: r
        for r in conn.execute(
            text(
                "SELECT player_id, name_full, gsis_id, nfl_com_player_id "
                "FROM players WHERE player_id IN (:a, :b, :c)"
            ),
            {"a": JJ, "b": JORDY, "c": STUB},
        ).mappings()
    }

    jj, jordy, stub = rows.get(JJ), rows.get(JORDY), rows.get(STUB)
    if jj is None:
        problems.append(f"J.J. row {JJ} missing")
    elif jj["gsis_id"] != "00-0032112" or jj["nfl_com_player_id"] != JORDY_NFL_COM:
        problems.append(
            f"J.J. row {JJ} not in expected pre-state "
            f"(gsis={jj['gsis_id']}, nfl_com={jj['nfl_com_player_id']})"
        )
    if jordy is None:
        problems.append(f"Jordy row {JORDY} missing")
    elif jordy["gsis_id"] != "00-0026176" or jordy["nfl_com_player_id"] is not None:
        problems.append(
            f"Jordy row {JORDY} not in expected pre-state "
            f"(gsis={jordy['gsis_id']}, nfl_com={jordy['nfl_com_player_id']})"
        )
    if stub is None:
        problems.append(f"stub row {STUB} missing (already merged?)")
    elif stub["nfl_com_player_id"] != JJ_NFL_COM:
        problems.append(
            f"stub row {STUB} nfl_com={stub['nfl_com_player_id']} (expected {JJ_NFL_COM})"
        )
    elif _count(conn, "player_stats_raw", STUB) != 0:
        problems.append(f"stub row {STUB} unexpectedly carries raw stats")

    # Jordy must start empty on the NFL.com tables, or the repoint could clash.
    for t in MOVE_TABLES:
        if (n := _count(conn, t, JORDY)) != 0:
            problems.append(f"Jordy row {JORDY} already has {n} {t} rows (move would collide)")

    return problems


def _report(conn, header: str) -> None:
    print(f"\n{header}")
    for pid, label in ((JJ, "J.J.  17322"), (JORDY, "Jordy 17326"), (STUB, "stub  26662")):
        row = (
            conn.execute(
                text(
                    "SELECT name_full, nfl_com_player_id, gsis_id FROM players WHERE player_id = :p"
                ),
                {"p": pid},
            )
            .mappings()
            .first()
        )
        if row is None:
            print(f"  {label}: (deleted)")
            continue
        rosters = _count(conn, "team_rosters", pid)
        txns = _count(conn, "transactions", pid)
        raw = _count(conn, "player_stats_raw", pid)
        span = conn.execute(
            text(
                "SELECT first_rostered_season, last_rostered_season "
                "FROM players WHERE player_id = :p"
            ),
            {"p": pid},
        ).first()
        print(
            f"  {label}: nfl_com={row['nfl_com_player_id']!s:<8} gsis={row['gsis_id']}  "
            f"rosters={rosters:<4} txns={txns:<3} raw={raw:<4} span={span[0]}-{span[1]}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit the repair (default: dry run).")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL from settings.")
    args = ap.parse_args(argv)

    db_url = args.database_url or get_settings().database_url

    if args.apply and db_url.startswith("sqlite:///"):
        src = Path(db_url.removeprefix("sqlite:///"))
        if src.exists():
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            dst = src.parent / "backups" / f"{src.stem}-prenelsonuntangle-{stamp}.db"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            print(f"Backed up DB → {dst}")

    engine = create_app_engine(db_url)
    try:
        with engine.connect() as conn:
            problems = _validate(conn)
            _report(conn, "BEFORE:")
            if problems:
                print("\nABORT — DB not in the expected pre-repair state:")
                for p in problems:
                    print(f"  - {p}")
                return 1

            move_counts = {t: _count(conn, t, JJ) for t in MOVE_TABLES}
            print(
                f"\nPlan:\n"
                f"  1. move {move_counts} from J.J. {JJ} → Jordy {JORDY}\n"
                f"  2. move nfl_com {JORDY_NFL_COM} off {JJ} onto {JORDY} (+ seed override)\n"
                f"  3. fold stub {STUB} (nfl_com {JJ_NFL_COM}) into J.J. {JJ}\n"
                f"  4. recompute rostered spans"
            )

            if not args.apply:
                print("\nDRY RUN — no changes written. Re-run with --apply to commit.")
                return 0

            fk_tables = _fk_tables(conn)
            # The SELECTs above auto-began a read transaction; close it so we
            # can open an explicit write transaction.
            conn.rollback()
            with conn.begin():
                # 1. Jordy's misplaced NFL.com fantasy rows: 17322 → 17326.
                for t in MOVE_TABLES:
                    conn.execute(
                        text(f"UPDATE {t} SET player_id = :dst WHERE player_id = :src"),
                        {"dst": JORDY, "src": JJ},
                    )
                # 2. Hand the NFL.com id 1032 to Jordy and pin it durably.
                conn.execute(
                    text("UPDATE players SET nfl_com_player_id = NULL WHERE player_id = :p"),
                    {"p": JJ},
                )
                conn.execute(
                    text("UPDATE players SET nfl_com_player_id = :v WHERE player_id = :p"),
                    {"v": JORDY_NFL_COM, "p": JORDY},
                )
                conn.execute(
                    text(
                        "INSERT INTO player_id_overrides "
                        "(external_id_kind, external_id_value, player_id, notes) "
                        "VALUES ('nfl_com_player_id', :val, :pid, :notes) "
                        "ON CONFLICT(external_id_kind, external_id_value) DO UPDATE SET "
                        "player_id = excluded.player_id, notes = excluded.notes, "
                        "updated_at = CURRENT_TIMESTAMP"
                    ),
                    {
                        "val": JORDY_NFL_COM,
                        "pid": JORDY,
                        "notes": "Nelson conflation untangle (untangle_nelson_conflation.py)",
                    },
                )
                # 3. Fold J.J.'s stub into the now-clean 17322. Re-read players
                # so the canonical's nfl_com_player_id reflects the NULL we just
                # wrote — otherwise _apply_merge would skip stamping 2552656.
                _players, by_id = _load_players(conn)
                stub, canon = by_id[STUB], by_id[JJ]
                _seed_override(conn, stub, JJ)
                _apply_merge(conn, stub, canon, fk_tables)

            # 4. Materialize the corrected league-relevance spans.
            with Session(engine) as ss:
                touched = recompute_rostered_spans(ss)
                ss.commit()
            print(f"\nApplied. Recomputed rostered-season spans ({touched} player rows).")
            _report(conn, "AFTER:")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
