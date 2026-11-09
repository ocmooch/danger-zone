"""Repair moved_owner's back-projected slot (team_abbrev) and name on his 2010-2015 teams.

The lost-team reconstruction that restored moved_owner's (owner 3) 2010-2015 team-seasons
stamped them with his *modern* franchise identity instead of the period-correct one:

- ``team_abbrev = "12"`` (moved_owner's current NFL.com slot) on rows whose real slot
  those years was **"3"**.
- ``team_name = "Team 014"`` (moved_owner's current 2025 name) on those same
  rows, instead of the period-correct slot-3 name.

This is the same back-projection class as the owner-identity / phantom-team repair
(see ``scripts/archive/repair_owner_identity_and_phantom_teams.py``), but it landed
in ``team_abbrev`` and the canonical name. The effect, in each of 2010-2015: a
**duplicate ``team_abbrev = "12"``** (moved_owner + the real slot-12 owner) and a
**missing slot 3**.

Source of truth for the corrected slot-3 names is per-season NFL.com history (the
same path used for the reconstruction); they are cross-checked against the
downstream dashboard's authoritative ``(year, slot) -> name`` table, where each
season's surviving slot-12 name already matches the non-moved_owner DB row exactly.

Only ``teams.team_abbrev`` and ``teams.team_name`` change. Matchups, rosters,
transactions, draft position, and ``final_rank`` key on ``team_id`` and stay
attached. moved_owner's 2018-2025 rows are left alone -- slot 12 is correct there.

Dry-run by default. ``--apply`` backs up the SQLite file, then commits.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.models import Matchup, Season, Team
from ff_pipeline.settings import get_settings

MIKE_OWNER_ID = 3
CORRECT_SLOT = "3"
BACKPROJECTED_SLOT = "12"
BACKPROJECTED_NAME = "Team 014"

# year -> period-correct NFL.com slot-3 team name (moved_owner's real team those years).
SLOT3_NAMES: dict[int, str] = {
    2010: "Team 179",
    2011: "Team 079",
    2012: "Team 143",
    2013: "Team 125",
    2014: "Team 081",
    2015: "Team 137",
}


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as session:
            print("== Repair: moved_owner's back-projected slot + name (2010-2015) ==")
            _repair(session, league_id=args.league_id)
            print("\n== Validation (post-repair, in transaction) ==")
            ok = _validate(session, league_id=args.league_id)

            if not args.apply:
                session.rollback()
                print("\nDry run only; rerun with --apply to commit.")
                return
            if not ok:
                session.rollback()
                raise SystemExit("validation failed; rolled back. Fix inputs and retry.")

            _backup_sqlite(settings.database_url)
            session.commit()
            print("\nApplied: moved_owner's 2010-2015 teams moved slot 12 -> 3 with slot-3 names.")
    finally:
        engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league-id", default=get_settings().nfl_league_id)
    parser.add_argument("--apply", action="store_true", help="commit changes (default: dry run)")
    return parser.parse_args()


def _repair(session: Session, *, league_id: str) -> None:
    for year, correct_name in SLOT3_NAMES.items():
        team = session.execute(
            select(Team)
            .join(Season, Season.season_id == Team.season_id)
            .where(
                Season.league_id == league_id,
                Season.year == year,
                Team.owner_id == MIKE_OWNER_ID,
            )
        ).scalar_one_or_none()
        if team is None:
            print(f"  {year}: moved_owner has no team row (skipped)")
            continue
        if team.team_abbrev == CORRECT_SLOT and team.team_name == correct_name:
            print(f"  {year}: already slot 3 / {correct_name!r} (no-op)")
            continue
        if team.team_abbrev != BACKPROJECTED_SLOT or team.team_name != BACKPROJECTED_NAME:
            print(
                f"  {year}: unexpected state abbrev={team.team_abbrev!r} "
                f"name={team.team_name!r} (expected {BACKPROJECTED_SLOT!r}/"
                f"{BACKPROJECTED_NAME!r}); skipped"
            )
            continue
        team.team_abbrev = CORRECT_SLOT
        team.team_name = correct_name
        print(f"  {year}: team {team.team_id} slot 12 -> 3, name -> {correct_name!r}")
    session.flush()


# --- Validation (handoff acceptance criteria) ----------------------------------
def _validate(session: Session, *, league_id: str) -> bool:
    ok = True
    played_ids = select(Matchup.season_id).distinct().scalar_subquery()

    # 1. No played season has a duplicate team_abbrev.
    dup = session.execute(
        select(Season.year, Team.team_abbrev, func.count())
        .join(Season, Season.season_id == Team.season_id)
        .where(Team.season_id.in_(played_ids))
        .group_by(Season.year, Team.team_abbrev)
        .having(func.count() > 1)
    ).all()
    ok &= _check("no played season has a duplicate team_abbrev", not dup, dup)

    # 2. Every played season 2010-2025 has exactly slots 1-12 present once each.
    rows = session.execute(
        select(Season.year, Team.team_abbrev)
        .join(Season, Season.season_id == Team.season_id)
        .where(Team.season_id.in_(played_ids), Season.year.between(2010, 2025))
    ).all()
    by_year: dict[int, list[str | None]] = {}
    for year, slot in rows:
        by_year.setdefault(year, []).append(slot)
    expected = {str(i) for i in range(1, 13)}
    bad_slots = {
        y: sorted(s or "" for s in slots)
        for y, slots in by_year.items()
        if set(slots) != expected or len(slots) != 12
    }
    ok &= _check("every played season 2010-2025 has slots 1-12 once each", not bad_slots, bad_slots)

    # 3. moved_owner's 2015 team is slot 3 / "Team 137".
    moved_owner_2015 = session.execute(
        select(Team)
        .join(Season, Season.season_id == Team.season_id)
        .where(Season.league_id == league_id, Season.year == 2015, Team.owner_id == MIKE_OWNER_ID)
    ).scalar_one_or_none()
    moved_owner_ok = (
        moved_owner_2015 is not None
        and moved_owner_2015.team_abbrev == CORRECT_SLOT
        and moved_owner_2015.team_name == "Team 137"
    )
    ok &= _check(
        "moved_owner's 2015 team is slot 3 / 'Team 137'",
        moved_owner_ok,
        f"abbrev={getattr(moved_owner_2015, 'team_abbrev', None)} name={getattr(moved_owner_2015, 'team_name', None)}",
    )
    return ok


def _check(label: str, passed: bool, detail: object) -> bool:
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {label}" + ("" if passed else f"  -> {detail}"))
    return passed


def _backup_sqlite(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    db_path = Path(database_url[len(prefix) :]).resolve()
    if not db_path.exists():
        return
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_path.stem}-pre-moved_owner-slot-repair-{stamp}{db_path.suffix}"
    shutil.copy2(db_path, dest)
    print(f"  backup: {dest}")


if __name__ == "__main__":
    main()
