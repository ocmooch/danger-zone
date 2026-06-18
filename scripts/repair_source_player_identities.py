"""Repair reviewed NFL.com external-ID ownership mistakes.

The reviewed ledger lives in ``data/source_player_identity_repairs.json``.
Default mode is a dry run; ``--apply`` snapshots SQLite, re-homes NFL.com-owned
rows, transfers the external ID, seeds an override, and recomputes roster spans.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.repository.player_identity_integrity import source_identity_mismatches
from ff_pipeline.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LEDGER = _ROOT / "data" / "source_player_identity_repairs.json"
_MOVE_TABLES = ("team_rosters", "transactions", "player_availability")


@dataclass(frozen=True, slots=True)
class Repair:
    nfl_com_player_id: str
    wrong_player_id: int
    canonical_player_id: int
    source_name: str
    source_position: str
    evidence: str


def _load_repairs(path: Path) -> list[Repair]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list):
        raise ValueError("repair ledger must contain a JSON list")
    return [Repair(**row) for row in payload]


def _count(conn: Connection, table: str, player_id: int) -> int:
    return int(
        conn.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE player_id = :player_id"),
            {"player_id": player_id},
        ).scalar_one()
    )


def _validate(conn: Connection, repairs: list[Repair]) -> list[str]:
    errors: list[str] = []
    wrong_ids = {repair.wrong_player_id for repair in repairs}
    for repair in repairs:
        rows = conn.execute(
            text(
                "SELECT player_id, name_full, position, nfl_com_player_id "
                "FROM players WHERE player_id IN (:wrong, :canonical)"
            ),
            {"wrong": repair.wrong_player_id, "canonical": repair.canonical_player_id},
        ).mappings()
        players = {int(row["player_id"]): row for row in rows}
        wrong = players.get(repair.wrong_player_id)
        canonical = players.get(repair.canonical_player_id)
        if wrong is None or canonical is None:
            errors.append(f"{repair.nfl_com_player_id}: missing wrong or canonical player row")
            continue
        if wrong["nfl_com_player_id"] != repair.nfl_com_player_id:
            # An already-applied ledger entry is a valid no-op.
            if canonical["nfl_com_player_id"] == repair.nfl_com_player_id:
                continue
            errors.append(
                f"{repair.nfl_com_player_id}: wrong player {repair.wrong_player_id} owns "
                f"{wrong['nfl_com_player_id']!r}"
            )
        canonical_id = canonical["nfl_com_player_id"]
        if (
            canonical_id is not None
            and canonical_id != repair.nfl_com_player_id
            and repair.canonical_player_id not in wrong_ids
        ):
            errors.append(
                f"{repair.nfl_com_player_id}: canonical player {repair.canonical_player_id} "
                f"already owns {canonical_id!r}"
            )
        collision = conn.execute(
            text(
                "SELECT COUNT(*) FROM team_rosters src "
                "JOIN team_rosters dst ON dst.season_year = src.season_year "
                "AND dst.week = src.week AND dst.player_id = :canonical "
                "WHERE src.player_id = :wrong"
            ),
            {
                "wrong": repair.wrong_player_id,
                "canonical": repair.canonical_player_id,
            },
        ).scalar_one()
        if collision:
            errors.append(
                f"{repair.nfl_com_player_id}: {collision} team_rosters uniqueness collisions"
            )
    return errors


def _report(conn: Connection, repairs: list[Repair]) -> None:
    for repair in repairs:
        counts = ", ".join(
            f"{table}={_count(conn, table, repair.wrong_player_id)}" for table in _MOVE_TABLES
        )
        print(
            f"{repair.nfl_com_player_id}: {repair.wrong_player_id} -> "
            f"{repair.canonical_player_id} {repair.source_name} ({counts})"
        )


def _pending_repairs(conn: Connection, repairs: list[Repair]) -> list[Repair]:
    pending: list[Repair] = []
    for repair in repairs:
        canonical_id = conn.execute(
            text("SELECT nfl_com_player_id FROM players WHERE player_id = :player_id"),
            {"player_id": repair.canonical_player_id},
        ).scalar_one()
        if canonical_id != repair.nfl_com_player_id:
            pending.append(repair)
    return pending


def _backup_sqlite(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    source = Path(database_url.removeprefix("sqlite:///"))
    if not source.exists():
        return
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = source.parent / "backups" / f"{source.stem}-pre-player-identity-{stamp}.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    print(f"Backed up DB -> {destination}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the reviewed repairs.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--ledger", type=Path, default=_DEFAULT_LEDGER)
    args = parser.parse_args(argv)

    database_url = args.database_url or get_settings().database_url
    repairs = _load_repairs(args.ledger)
    engine = create_app_engine(database_url)
    try:
        with engine.connect() as conn:
            errors = _validate(conn, repairs)
            _report(conn, repairs)
            pending = _pending_repairs(conn, repairs)
            if errors:
                print("\nABORT - validation errors:")
                for error in errors:
                    print(f"  - {error}")
                return 1
            if not args.apply:
                print(
                    f"\nDRY RUN - {len(pending)} pending of {len(repairs)} reviewed repairs; "
                    "no changes written."
                )
                return 0
            if not pending:
                print("\nNo pending repairs.")
                return 0

        _backup_sqlite(database_url)
        with engine.begin() as conn:
            # Clear every bad owner first so chained repairs can safely assign IDs.
            for repair in pending:
                conn.execute(
                    text(
                        "UPDATE players SET nfl_com_player_id = NULL "
                        "WHERE player_id = :wrong AND nfl_com_player_id = :external_id"
                    ),
                    {"wrong": repair.wrong_player_id, "external_id": repair.nfl_com_player_id},
                )
            for repair in pending:
                for table in _MOVE_TABLES:
                    conn.execute(
                        text(f"UPDATE {table} SET player_id = :canonical WHERE player_id = :wrong"),
                        {
                            "canonical": repair.canonical_player_id,
                            "wrong": repair.wrong_player_id,
                        },
                    )
                conn.execute(
                    text(
                        "UPDATE players SET nfl_com_player_id = :external_id "
                        "WHERE player_id = :canonical"
                    ),
                    {
                        "external_id": repair.nfl_com_player_id,
                        "canonical": repair.canonical_player_id,
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO player_id_overrides "
                        "(external_id_kind, external_id_value, player_id, notes) "
                        "VALUES ('nfl_com_player_id', :external_id, :canonical, :notes) "
                        "ON CONFLICT(external_id_kind, external_id_value) DO UPDATE SET "
                        "player_id = excluded.player_id, notes = excluded.notes, "
                        "updated_at = CURRENT_TIMESTAMP"
                    ),
                    {
                        "external_id": repair.nfl_com_player_id,
                        "canonical": repair.canonical_player_id,
                        "notes": f"Reviewed source identity repair: {repair.evidence}",
                    },
                )

        with Session(engine) as session:
            touched = recompute_rostered_spans(session)
            session.commit()
            remaining = source_identity_mismatches(session)
        print(
            f"\nApplied {len(pending)} repairs; recomputed {touched} roster spans; "
            f"{len(remaining)} strong mismatch candidates remain."
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
