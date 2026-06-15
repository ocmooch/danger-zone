"""Repair Christine Michael roster rows stamped onto Michael Cox.

Background
----------
The resolver previously allowed an abbreviated NFL.com name like
``C. Michael`` to fuzzy-match by token similarity alone. That let Christine
Michael's NFL.com player id (``2539322``) land on the real Giants RB Michael
Cox row (``players.player_id=5829``). Cox's nflverse/GSIS stat rows are
correct; the NFL.com-side roster and transaction rows below belong to the
existing Christine Michael row (``players.player_id=16245``).

This script moves only the confirmed NFL.com-side rows:

* 2013 week 8 roster + add/drop transactions (Seattle at St. Louis context);
* all 2015-2016 roster + transaction rows (Christine Michael production);
* ``nfl_com_player_id=2539322`` and a permanent override to Christine Michael.

The real Michael Cox 2013 week 7 row and 2014 draft/drop context are left on
Michael Cox.

Usage
-----
    uv run python scripts/repair_christine_michael_identity.py
    uv run python scripts/repair_christine_michael_identity.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.settings import get_settings

COX_PID = 5829
CHRISTINE_PID = 16245
CHRISTINE_NFL_COM_ID = "2539322"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes; default is dry run.")
    parser.add_argument("--database-url", default=None, help="Override settings database URL.")
    args = parser.parse_args(argv)

    db_url = args.database_url or get_settings().database_url
    if args.apply:
        _backup_sqlite(db_url)

    engine = create_app_engine(db_url)
    try:
        with engine.connect() as conn:
            errors = _validate(conn)
            _report(conn, "BEFORE")
            if errors:
                print("\nABORT - validation errors:")
                for error in errors:
                    print(f"  - {error}")
                return 1
            if not args.apply:
                print("\nDRY RUN - no changes written. Re-run with --apply to commit.")
                return 0

            conn.rollback()
            with conn.begin():
                moved_rosters = conn.execute(
                    text(
                        "UPDATE team_rosters SET player_id = :dst "
                        "WHERE player_id = :src "
                        "AND (season_year >= 2015 OR (season_year = 2013 AND week = 8))"
                    ),
                    {"dst": CHRISTINE_PID, "src": COX_PID},
                ).rowcount
                moved_txns = conn.execute(
                    text(
                        "UPDATE transactions SET player_id = :dst "
                        "WHERE player_id = :src "
                        "AND (season_id IN (SELECT season_id FROM seasons WHERE year >= 2015) "
                        "OR transaction_id IN (10387, 10261))"
                    ),
                    {"dst": CHRISTINE_PID, "src": COX_PID},
                ).rowcount
                conn.execute(
                    text("UPDATE players SET nfl_com_player_id = NULL WHERE player_id = :pid"),
                    {"pid": COX_PID},
                )
                conn.execute(
                    text("UPDATE players SET nfl_com_player_id = :nfl WHERE player_id = :pid"),
                    {"nfl": CHRISTINE_NFL_COM_ID, "pid": CHRISTINE_PID},
                )
                conn.execute(
                    text(
                        "INSERT INTO player_id_overrides "
                        "(external_id_kind, external_id_value, player_id, notes) "
                        "VALUES ('nfl_com_player_id', :nfl, :pid, :notes) "
                        "ON CONFLICT(external_id_kind, external_id_value) DO UPDATE SET "
                        "player_id = excluded.player_id, notes = excluded.notes, "
                        "updated_at = CURRENT_TIMESTAMP"
                    ),
                    {
                        "nfl": CHRISTINE_NFL_COM_ID,
                        "pid": CHRISTINE_PID,
                        "notes": "Christine Michael / Michael Cox abbreviated-name repair",
                    },
                )

            with Session(engine) as session:
                touched = recompute_rostered_spans(session)
                session.commit()

            print(
                f"\nApplied repair: moved {moved_rosters} roster rows and {moved_txns} "
                f"transactions; recomputed {touched} player spans."
            )
            _report(conn, "AFTER")
    finally:
        engine.dispose()
    return 0


def _validate(conn) -> list[str]:  # type: ignore[no-untyped-def]
    rows = {
        row["player_id"]: row
        for row in conn.execute(
            text(
                "SELECT player_id, name_full, nfl_com_player_id "
                "FROM players WHERE player_id IN (:cox, :christine)"
            ),
            {"cox": COX_PID, "christine": CHRISTINE_PID},
        ).mappings()
    }
    errors: list[str] = []
    cox = rows.get(COX_PID)
    christine = rows.get(CHRISTINE_PID)
    if cox is None:
        errors.append(f"missing Michael Cox row {COX_PID}")
    elif cox["nfl_com_player_id"] != CHRISTINE_NFL_COM_ID:
        errors.append(
            f"Michael Cox nfl_com_player_id is {cox['nfl_com_player_id']!r}, "
            f"expected {CHRISTINE_NFL_COM_ID!r}"
        )
    if christine is None:
        errors.append(f"missing Christine Michael row {CHRISTINE_PID}")
    elif christine["nfl_com_player_id"] not in (None, CHRISTINE_NFL_COM_ID):
        errors.append(
            f"Christine Michael already has nfl_com_player_id={christine['nfl_com_player_id']!r}"
        )
    if _count_rows_to_move(conn, "team_rosters") == 0:
        errors.append("no candidate team_rosters rows found to move")
    return errors


def _count_rows_to_move(conn, table: str) -> int:  # type: ignore[no-untyped-def]
    if table == "team_rosters":
        return conn.execute(
            text(
                "SELECT COUNT(*) FROM team_rosters WHERE player_id = :pid "
                "AND (season_year >= 2015 OR (season_year = 2013 AND week = 8))"
            ),
            {"pid": COX_PID},
        ).scalar_one()
    return conn.execute(
        text(
            "SELECT COUNT(*) FROM transactions WHERE player_id = :pid "
            "AND (season_id IN (SELECT season_id FROM seasons WHERE year >= 2015) "
            "OR transaction_id IN (10387, 10261))"
        ),
        {"pid": COX_PID},
    ).scalar_one()


def _report(conn, title: str) -> None:  # type: ignore[no-untyped-def]
    print(f"\n{title}:")
    for pid in (COX_PID, CHRISTINE_PID):
        row = conn.execute(
            text(
                "SELECT name_full, nfl_com_player_id, first_rostered_season, last_rostered_season "
                "FROM players WHERE player_id = :pid"
            ),
            {"pid": pid},
        ).first()
        rosters = conn.execute(
            text("SELECT COUNT(*) FROM team_rosters WHERE player_id = :pid"), {"pid": pid}
        ).scalar_one()
        txns = conn.execute(
            text("SELECT COUNT(*) FROM transactions WHERE player_id = :pid"), {"pid": pid}
        ).scalar_one()
        print(
            f"  {pid}: {row[0]} nfl_com={row[1]} rosters={rosters} "
            f"transactions={txns} span={row[2]}-{row[3]}"
        )
    print(
        "  candidate moves: "
        f"rosters={_count_rows_to_move(conn, 'team_rosters')} "
        f"transactions={_count_rows_to_move(conn, 'transactions')}"
    )


def _backup_sqlite(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    src = Path(database_url.removeprefix("sqlite:///"))
    if not src.exists():
        return
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    dst = src.parent / "backups" / f"{src.stem}-prechristine-michael-{stamp}{src.suffix}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    print(f"Backed up DB -> {dst}")


if __name__ == "__main__":
    sys.exit(main())
