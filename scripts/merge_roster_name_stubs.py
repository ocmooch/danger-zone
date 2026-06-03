"""One-off repair: fold abbreviated NFL.com roster name-stubs into their
canonical nflverse players.

Background
----------
NFL.com league rosters render a player as ``"V. Cruz"`` and carry an
``nfl_com_player_id``. At roster-ingest time the canonical nflverse row
(``Victor Cruz``, keyed by ``gsis_id``) has no ``nfl_com_player_id`` yet,
so the resolver's direct-ID stage misses and ``thefuzz`` scores the
abbreviation below threshold — minting a fresh statless stub that then
accumulates the roster foreign keys and the materialized rostered span.
The result is two disjoint rows for one person:

* **canonical** — full ``name_first``/``name_last`` + bio, stats, but NULL
  ``first/last_rostered_season`` (never linked to a roster), and
* **stub** — abbreviated ``name_full`` ("V. Cruz"), NULL bio fields, no
  stats, but holding the rostered span the league actually references.

Why a *curated* map instead of the heuristic merge
--------------------------------------------------
``merge_split_player_identities.py`` can't safely resolve these, because
nflverse stores **legal** first names: Torrey Smith is ``James Smith``,
Duke Johnson is ``Randy Johnson``, Beanie Wells is ``Chris Wells``, Chad
Ochocinco is ``Chad Johnson``. First-initial matching therefore points at
the wrong person (``T. Smith`` → the journeyman *Taj* Smith, not Torrey).
Disambiguating ``A. Smith`` / ``D. Johnson`` / ``M. Floyd`` cohorts needs
position + NFL team + rostered-era judgement, so every pairing below was
verified by hand against known NFL history.

What this does (per pair, mirroring the proven heuristic path)
--------------------------------------------------------------
1. Seed ``player_id_overrides`` (``nfl_com_player_id`` → canonical) so a
   future roster sync resolves the abbreviation directly and never
   re-stubs — the authoritative, durable guard.
2. Repoint every ``player_id`` foreign key (``team_rosters`` &c.) from the
   stub onto the canonical row.
3. Delete the stub and stamp its freed external IDs onto the canonical row.
4. ``recompute_rostered_spans`` so the canonical row carries the span it
   just absorbed.

Two rostered stubs have **no canonical row** in the DB (the player retired
before the 2010 nflverse window: Torry Holt, Vonta Leach). Both were first
rostered in 2010+ so they are league-relevant; there is simply nothing to
merge into. They are left untouched and reported — an honest source gap,
the same treatment as NFL.com-only rows.

Usage
-----
    uv run python scripts/merge_roster_name_stubs.py            # dry run
    uv run python scripts/merge_roster_name_stubs.py --apply    # commit

``--apply`` snapshots the SQLite file to ``data/backups/`` first.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Reuse the proven, side-effect-tested internals so this script and the
# heuristic merger stay byte-for-byte consistent in how they repoint FKs,
# delete stubs and stamp IDs.
from merge_split_player_identities import (
    _apply_merge,
    _fk_tables,
    _load_players,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.settings import get_settings

# ---------------------------------------------------------------------------
# Curated stub player_id → canonical player_id.
#
# Each pairing verified by hand on position + NFL team + rostered era. The
# trailing comment records the stub's abbreviation/team/span and the
# canonical's full name so the map is reviewable without a DB to hand. Note
# where nflverse stores a legal first name (≠ the common name) — those are
# exactly the pairings first-initial heuristics get wrong.
# ---------------------------------------------------------------------------
STUB_TO_CANONICAL: dict[int, int] = {
    # --- single same-position match (the _normalize bug had hidden these) ---
    26224: 15759,  # V. McDonald  TE PIT 2015-2019  -> Vance McDonald
    26327: 21089,  # V. Shiancoe  TE MIN 2010-2011  -> Visanthe Shiancoe
    26330: 26006,  # V. Young     QB TEN 2010-2011  -> Vince Young
    26344: 6449,  # V. Davis     TE SF  2010-2019  -> Vernon Davis
    26371: 12070,  # V. Jackson   WR LAC 2010-2016  -> Vincent Jackson
    26473: 6003,  # V. Cruz      WR NYG 2011-2016  -> Victor Cruz
    26497: 3937,  # V. Brown     WR LAC 2011-2013  -> Vincent Brown
    26511: 2080,  # V. Ballard   RB IND 2012-2013  -> Vick Ballard
    26623: 9508,  # V. Green     TE DEN 2015-2015  -> Virgil Green
    # --- disambiguated by NFL team + rostered era ---
    26190: 23022,  # M. Thomas    WR NO  2016-2023  -> Michael Thomas
    26324: 8174,  # M. Floyd     WR LAC 2010-2015  -> Malcom Floyd
    26328: 25235,  # M. Williams  WR SEA 2010-2010  -> Mike Williams (SEA)
    26346: 13098,  # T. Jones     RB KC  2010-2011  -> Thomas Jones
    26422: 12961,  # J. Jones     WR GB  2010-2015  -> James Jones
    26432: 25182,  # K. Williams  RB WAS 2010-2010  -> Keiland Williams
    26647: 16416,  # Z. Miller    TE CHI 2015-2017  -> Zach Miller (CHI)
    26661: 10257,  # D. Harris    RB SF  2016-2016  -> DuJuan Harris
    26666: 25187,  # K. Williams  RB ARI 2017-2017  -> Kerwynn Williams
    26668: 3801,  # J. Brown     WR ARI 2017-2017  -> Jaron Brown
    26192: 20165,  # J. Ross      WR CIN 2018-2019  -> John Ross
    26387: 23019,  # M. Thomas    WR JAC 2010-2012  -> Mike Thomas
    26390: 16860,  # J. Morgan    WR SF  2010-2010  -> Josh Morgan
    26465: 16720,  # D. Moore     WR LV  2011-2013  -> Denarius Moore
    26469: 21596,  # T. Smith     WR BAL 2011-2017  -> Torrey Smith (legal: James Smith)
    26539: 13091,  # T. Jones     RB LV  2012-2012  -> Taiwan Jones
    26592: 10984,  # J. Hill      RB CIN 2014-2017  -> Jeremy Hill
    26672: 7924,  # D. Fells     TE DET 2017-2020  -> Darren Fells
    26691: 7369,  # B. Edwards   WR LV  2021-2021  -> Bryan Edwards
    26124: 22937,  # D. Thomas    WR HOU 2010-2018  -> Demaryius Thomas
    26149: 12494,  # D. Johnson   RB ARI 2015-2021  -> David Johnson
    26172: 12696,  # D. Johnson   RB CLE 2015-2021  -> Duke Johnson (legal: Randy Johnson)
    26178: 3809,  # J. Brown     WR BAL 2014-2020  -> John Brown
    26227: 6390,  # M. Davis     RB SEA 2016-2022  -> Mike Davis
    26297: 12953,  # J. Jones     WR HOU 2010-2013  -> Jacoby Jones
    26375: 21748,  # S. Smith     WR NYG 2010-2012  -> Steve Smith (USC; legal: Steven Smith)
    26376: 16417,  # Z. Miller    TE LV  2010-2013  -> Zach Miller (SEA/OAK; legal: Zachary Miller)
    26382: 5189,  # M. Clayton   WR STL 2010-2010  -> Mark Clayton
    26489: 21457,  # A. Smith     RB ARI 2011-2011  -> Alfonso Smith
    26549: 8177,  # M. Floyd     WR ARI 2013-2016  -> Michael Floyd
    26604: 21468,  # A. Smith     RB ATL 2014-2016  -> Antone Smith
    26616: 12456,  # C. Johnson   WR MIN 2014-2015  -> Charles D. Johnson
    26630: 25179,  # K. Williams  RB BUF 2015-2015  -> Karlos Williams
    26653: 24336,  # D. Washington RB LV 2016-2020  -> DeAndre Washington
    26656: 24341,  # D. Washington RB DET 2016-2016 -> Dwayne Washington
    26689: 12761,  # T. Johnson   WR LAC 2020-2020  -> Tyron Johnson
    26692: 13102,  # T. Jones     RB NO  2021-2022  -> Tony Jones
    # --- name changed / surname differs from abbreviation ---
    26312: 12449,  # C. Ochocinco WR CIN 2010-2011  -> Chad Johnson (legal name)
    26367: 24595,  # B. Wells     RB ARI 2010-2012  -> Chris Wells ("Beanie" Wells)
}

# Rostered stubs with no canonical nflverse row to merge into (player's last
# NFL season predates the 2010 nflverse window). Reported, not modified.
NO_CANONICAL_STUBS: dict[int, str] = {
    26315: "T. Holt  WR NE  2010-2010  (Torry Holt; no nflverse row)",
    26564: "V. Leach RB BAL 2013-2013  (Vonta Leach; no nflverse row)",
}

# Stubs deliberately held back: the obvious canonical is itself already
# corrupted, so folding the stub in would compound the error. These need a
# separate untangle before they can be merged. Reported, not modified.
#
#   26662  J. Nelson WR ARI 2016-2017 — is J.J. Nelson, but the canonical
#   row 17322 ("J.J. Nelson", legal Jamarcus) already carries a 2010-2018
#   rostered span: the 2010-2014 weeks belong to *Jordy* Nelson (J.J.
#   debuted 2015). 17322 is a pre-existing J.J./Jordy conflation; fix that
#   first, then this stub can fold into the cleaned J.J. Nelson row.
NEEDS_REVIEW_STUBS: dict[int, str] = {
    26662: "J. Nelson WR ARI 2016-2017 — canonical 17322 is a J.J./Jordy Nelson conflation",
}


def _validate(by_id: dict[int, Any], stub_id: int, canon_id: int) -> tuple[str, str] | None:
    """Classify a pairing as ``("error", msg)`` (blocks), ``("warn", msg)``
    (proceeds), or ``None`` (clean)."""
    stub = by_id.get(stub_id)
    canon = by_id.get(canon_id)
    if stub is None:
        return "error", "stub player_id absent (already merged?)"
    if canon is None:
        return "error", "canonical player_id absent"
    if stub.raw_rows:
        return "error", f"stub unexpectedly stats-bearing ({stub.raw_rows} raw rows)"
    if not stub.nfl_com_player_id:
        return "error", "stub has no nfl_com_player_id to override/stamp"
    canon_nflc = canon.ids.get("nfl_com_player_id")
    if canon_nflc and canon_nflc != stub.nfl_com_player_id:
        # Not fatal: the canonical keeps its existing id (the stamp is NULL→
        # value only) and the stub's id is pinned via the override table, so
        # both ids resolve to the canonical going forward. Verified by hand
        # that the canonical is the right player; flag for the record.
        return "warn", f"canonical keeps its existing nfl_com_player_id ({canon_nflc})"
    return None


def _seed_override(conn, stub, canon_id: int) -> None:
    """Pin the stub's nfl_com_player_id to the canonical player (idempotent)."""
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
            "val": stub.nfl_com_player_id,
            "pid": canon_id,
            "notes": "roster name-stub merge (merge_roster_name_stubs.py)",
        },
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit the merges (default: dry run).")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL from settings.")
    args = ap.parse_args(argv)

    db_url = args.database_url or get_settings().database_url

    if args.apply and db_url.startswith("sqlite:///"):
        src = Path(db_url.removeprefix("sqlite:///"))
        if src.exists():
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            dst = src.parent / "backups" / f"{src.stem}-prerostermerge-{stamp}.db"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            print(f"Backed up DB → {dst}")

    engine = create_app_engine(db_url)
    try:
        with engine.connect() as conn:
            _players, by_id = _load_players(conn)
            fk_tables = _fk_tables(conn)

            planned: list[tuple[int, int]] = []
            errors: list[tuple[int, int, str]] = []
            warnings: list[tuple[int, int, str]] = []
            for stub_id, canon_id in STUB_TO_CANONICAL.items():
                verdict = _validate(by_id, stub_id, canon_id)
                if verdict is None:
                    planned.append((stub_id, canon_id))
                elif verdict[0] == "warn":
                    planned.append((stub_id, canon_id))
                    warnings.append((stub_id, canon_id, verdict[1]))
                else:
                    errors.append((stub_id, canon_id, verdict[1]))

            print(f"FK tables repointed per merge: {fk_tables}")
            print(f"\nCurated pairings: {len(STUB_TO_CANONICAL)}")
            print(f"  ready to merge: {len(planned)}")
            print(f"  warnings (proceed): {len(warnings)}")
            for stub_id, canon_id, warn in warnings:
                print(f"    stub {stub_id} → {canon_id}: {warn}")
            print(f"  validation errors: {len(errors)}")
            for stub_id, canon_id, err in errors:
                print(f"    stub {stub_id} → {canon_id}: {err}")
            print(f"\nLeft untouched (no canonical row, reported): {len(NO_CANONICAL_STUBS)}")
            for pid, label in NO_CANONICAL_STUBS.items():
                print(f"    {pid}: {label}")
            print(f"\nLeft untouched (canonical needs untangling first): {len(NEEDS_REVIEW_STUBS)}")
            for pid, label in NEEDS_REVIEW_STUBS.items():
                print(f"    {pid}: {label}")

            if errors:
                print("\nABORT — resolve validation errors before applying.")
                return 1

            if not args.apply:
                print("\nDRY RUN — no changes written. Re-run with --apply to commit.")
                return 0

            # The SELECTs above auto-began a read transaction; close it so we
            # can open an explicit write transaction.
            conn.rollback()
            with conn.begin():
                for stub_id, canon_id in planned:
                    stub, canon = by_id[stub_id], by_id[canon_id]
                    _seed_override(conn, stub, canon_id)
                    _apply_merge(conn, stub, canon, fk_tables)
            print(f"\nApplied {len(planned)} merges; seeded {len(planned)} id overrides.")

            with Session(engine) as ss:
                touched = recompute_rostered_spans(ss)
                ss.commit()
            print(f"Recomputed rostered-season spans ({touched} player rows).")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
