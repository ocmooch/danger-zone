"""One-off repair: re-home NFL.com roster identities that were stamped onto
the wrong same-name player.

Background
----------
The same defect first found for J.J. / Jordy Nelson
(``scripts/untangle_nelson_conflation.py``) recurs for a handful of other
namesakes: the resolver fuzzy-matched an abbreviated NFL.com lineup name
("S. Smith", "J. Brown") onto a same-name + same-position canonical row
*without* checking that the lineup's season fell inside that player's NFL
career. The wrong row then accreted another player's ``nfl_com_player_id``
and its entire NFL.com fantasy history (``team_rosters``, ``transactions``),
while the **real** player — who has nflverse stats but never picked up an
``nfl_com_player_id`` — was left stranded with zero roster rows.

``scripts/audit_roster_stat_era_mismatch.py`` enumerates the suspects; each
pairing below was then confirmed by matching the misplaced roster rows'
``nfl_com_points`` (and nflverse stat presence) to the true owner, week by
week. In every case the nflverse stats were never conflated — only the
NFL.com side moved — so the repair is a whole-pile re-home, not stat surgery.

The fix is the same shape as the Nelson untangle, minus the stub-fold:
1. Move the NFL.com-sourced rows (``team_rosters`` + ``transactions``) from
   the wrong row to the true owner.
2. Move the ``nfl_com_player_id`` over (NULL it on the wrong row, set it on
   the owner) and seed ``player_id_overrides`` so a future sync resolves the
   owner directly and never re-stamps.
3. ``recompute_rostered_spans``.

The wrong row keeps its own (correct, gsis-keyed) nflverse stats and simply
loses the NFL.com history that was never its own.

Resolver hardening: ``normalizer/player_ids.py`` now season-constrains the
fuzzy fallback, so reconstruction can't reproduce these. This script repairs
the rows already in the DB.

Usage
-----
    uv run python scripts/untangle_misstamped_roster_identities.py            # dry run
    uv run python scripts/untangle_misstamped_roster_identities.py --apply    # commit

``--apply`` snapshots the SQLite file to ``data/backups/`` first.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.settings import get_settings


@dataclass(frozen=True)
class Misstamp:
    wrong_pid: int  # row that wrongly holds the NFL.com identity
    owner_pid: int  # stranded real owner (has nflverse stats, no nfl_com id)
    nfl_com_id: str  # the misplaced id; belongs to the owner
    note: str  # owner name + verification summary


# Curated, hand-verified map. Each pairing confirmed by matching the misplaced
# roster rows' weekly nfl_com_points to the owner's actual production (the wrong
# row has zero nflverse stats in the rostered years, except Crabtree — see note).
MISSTAMPS: tuple[Misstamp, ...] = (
    Misstamp(
        21743, 21749, "2504595", "Shi Smith(rookie'21) <- Steve Smith Sr 2010-2016 (109 rosters)"
    ),
    Misstamp(
        3812, 3814, "2505459", "Jon Brown K(rookie'17) <- Josh Brown K 2010-2015 (28 rosters)"
    ),
    Misstamp(
        7365, 7367, "2506342", "Ben Edwards(rookie'15) <- Braylon Edwards 2010-2012 (23 rosters)"
    ),
    Misstamp(
        15400, 15401, "2543500", "John Matthews(last'11) <- Jordan Matthews 2014-2018 (50 rosters)"
    ),
    # Crabtree's roster span overlaps Tom's 2010-13 era, but every roster row's
    # nfl_com_points tracks Michael (WR), not Tom (TE) — incl. the 2014-18 weeks
    # after Tom retired. All 127 rows are Michael's; Tom owns none.
    Misstamp(
        5845,
        5844,
        "71269",
        "Tom Crabtree TE(last'13) <- Michael Crabtree WR 2010-2018 (127 rosters)",
    ),
)

# NFL.com-sourced tables keyed (transitively) by nfl_com_player_id. The
# gsis-keyed nflverse stat tables stay on the wrong row — they are its own.
MOVE_TABLES = ("team_rosters", "transactions")


def _count(conn, table: str, pid: int) -> int:
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE player_id = :p"), {"p": pid}
    ).scalar_one()


def _validate(conn, m: Misstamp) -> list[str]:
    problems: list[str] = []
    rows = {
        r["player_id"]: r
        for r in conn.execute(
            text("SELECT player_id, nfl_com_player_id FROM players WHERE player_id IN (:a, :b)"),
            {"a": m.wrong_pid, "b": m.owner_pid},
        ).mappings()
    }
    wrong, owner = rows.get(m.wrong_pid), rows.get(m.owner_pid)
    if wrong is None:
        problems.append(f"wrong row {m.wrong_pid} missing")
    elif wrong["nfl_com_player_id"] != m.nfl_com_id:
        problems.append(
            f"wrong row {m.wrong_pid} nfl_com={wrong['nfl_com_player_id']} (expected {m.nfl_com_id})"
        )
    if owner is None:
        problems.append(f"owner row {m.owner_pid} missing")
    elif owner["nfl_com_player_id"] is not None:
        problems.append(
            f"owner row {m.owner_pid} already has nfl_com_player_id={owner['nfl_com_player_id']}"
        )
    for t in MOVE_TABLES:
        if (n := _count(conn, t, m.owner_pid)) != 0:
            problems.append(
                f"owner row {m.owner_pid} already has {n} {t} rows (move would collide)"
            )
    return problems


def _report(conn, m: Misstamp, header: str) -> None:
    print(f"  {header} [{m.note}]")
    for pid, tag in ((m.wrong_pid, "wrong"), (m.owner_pid, "owner")):
        row = conn.execute(
            text(
                "SELECT nfl_com_player_id, first_rostered_season, last_rostered_season "
                "FROM players WHERE player_id = :p"
            ),
            {"p": pid},
        ).first()
        rosters = _count(conn, "team_rosters", pid)
        txns = _count(conn, "transactions", pid)
        print(
            f"      {tag:<5} {pid}: nfl_com={row[0]!s:<9} rosters={rosters:<4} txns={txns:<3} "
            f"span={row[1]}-{row[2]}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit the repairs (default: dry run).")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL from settings.")
    args = ap.parse_args(argv)
    db_url = args.database_url or get_settings().database_url

    if args.apply and db_url.startswith("sqlite:///"):
        src = Path(db_url.removeprefix("sqlite:///"))
        if src.exists():
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            dst = src.parent / "backups" / f"{src.stem}-premisstamp-{stamp}.db"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            print(f"Backed up DB → {dst}")

    engine = create_app_engine(db_url)
    try:
        with engine.connect() as conn:
            print("BEFORE:")
            errors: list[str] = []
            for m in MISSTAMPS:
                problems = _validate(conn, m)
                _report(conn, m, "·")
                errors += [f"{m.wrong_pid}->{m.owner_pid}: {p}" for p in problems]

            if errors:
                print("\nABORT — validation errors:")
                for e in errors:
                    print(f"  - {e}")
                return 1

            if not args.apply:
                print("\nDRY RUN — no changes written. Re-run with --apply to commit.")
                return 0

            conn.rollback()  # close the implicit read txn
            with conn.begin():
                for m in MISSTAMPS:
                    for t in MOVE_TABLES:
                        conn.execute(
                            text(f"UPDATE {t} SET player_id = :dst WHERE player_id = :src"),
                            {"dst": m.owner_pid, "src": m.wrong_pid},
                        )
                    conn.execute(
                        text("UPDATE players SET nfl_com_player_id = NULL WHERE player_id = :p"),
                        {"p": m.wrong_pid},
                    )
                    conn.execute(
                        text("UPDATE players SET nfl_com_player_id = :v WHERE player_id = :p"),
                        {"v": m.nfl_com_id, "p": m.owner_pid},
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
                            "val": m.nfl_com_id,
                            "pid": m.owner_pid,
                            "notes": "misstamped roster identity repair "
                            "(untangle_misstamped_roster_identities.py)",
                        },
                    )

            with Session(engine) as ss:
                touched = recompute_rostered_spans(ss)
                ss.commit()
            print(f"\nApplied {len(MISSTAMPS)} re-homes. Recomputed spans ({touched} rows).\n")
            print("AFTER:")
            for m in MISSTAMPS:
                _report(conn, m, "·")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
