"""Refresh nflverse-sourced player metadata on the EXISTING players.

Background
----------
``players.last_season`` was added (migration ``b3e1d9f4c2a7``) after an older
nflverse metadata crawl ran, so existing DBs can have stale or missing
``last_season`` even though the crawler now maps it. The fix is operational:
re-pull ``nflverse.load_players()`` and update the rows we already have.

What it does
------------
For every player already in the DB that carries a ``gsis_id``, this re-runs
the production ``_upsert_players`` path (so the mapping/coercion is identical
to a real crawl) **restricted to those gsis_ids** — it refreshes
``last_season``, ``rookie_year``, ``is_active``, ``nfl_team``, etc. and
inserts *nothing* new. It deliberately does not pull the whole 25k-row NFL
universe back in (that is what the league-scope filter + ``prune-players``
exist to keep out); a metadata refresh must not regrow the ghost set.

Players with no ``gsis_id`` (NFL.com-only rows nflverse can't identify), team
DEF rows, and stale ``gsis_id`` values no longer returned by nflverse are left
as-is — honest source gaps, not population bugs.

Finally it recomputes the league-relevance span
(``first/last_rostered_season``) so a fresh DB and this script agree.

This is the same idempotent crawl as ``ff-pipeline run --source nflverse``,
minus the weekly-stats re-ingest; re-running it keeps ``last_season``
populated.

Usage
-----
    uv run python scripts/refresh_player_metadata.py            # dry run
    uv run python scripts/refresh_player_metadata.py --apply    # commit

``--apply`` snapshots the SQLite file to ``data/backups/`` first.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nflverse.client import LiveNflverseSource, NflverseClient
from ff_pipeline.crawlers.nflverse.runner import _upsert_players
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.repository.models import Player
from ff_pipeline.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit the refresh (default: dry run).")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL from settings.")
    args = ap.parse_args(argv)

    db_url = args.database_url or get_settings().database_url

    if args.apply and db_url.startswith("sqlite:///"):
        src = Path(db_url.removeprefix("sqlite:///"))
        if src.exists():
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            dst = src.parent / "backups" / f"{src.stem}-premetarefresh-{stamp}.db"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            print(f"Backed up DB → {dst}")

    engine = create_app_engine(db_url)
    try:
        with Session(engine) as session:
            existing_gsis = {
                g
                for g in session.execute(
                    select(Player.gsis_id).where(Player.gsis_id.is_not(None))
                ).scalars()
                if g
            }
            before_last_season = session.execute(
                select(func.count()).where(Player.last_season.is_not(None))
            ).scalar_one()
            total_players = session.execute(select(func.count(Player.player_id))).scalar_one()
            print(
                f"Players: {total_players} total, {len(existing_gsis)} with gsis_id, "
                f"{before_last_season} with last_season populated."
            )

            print("Pulling nflverse load_players() …")
            client = NflverseClient(source=LiveNflverseSource())
            meta = client.players()
            # Restrict to players we already have. This is the crux: we update
            # existing rows only, never insert the rest of the NFL universe.
            meta = [m for m in meta if m.gsis_id in existing_gsis]
            print(f"nflverse rows matching existing players: {len(meta)}")

            counts = _upsert_players(session, meta)
            session.flush()
            spans_updated = recompute_rostered_spans(session)

            after_last_season = session.execute(
                select(func.count()).where(Player.last_season.is_not(None))
            ).scalar_one()
            rostered = session.execute(
                select(func.count()).where(Player.last_rostered_season.is_not(None))
            ).scalar_one()

            print(
                f"Refreshed: players ~{counts.rows_updated} updated "
                f"(+{counts.rows_added} added — expected 0).\n"
                f"last_season populated: {before_last_season} → {after_last_season} "
                f"/ {total_players}.\n"
                f"Rostered-span recompute touched {spans_updated} rows; "
                f"{rostered} players are league-relevant (ever rostered)."
            )
            if counts.rows_added:
                print(
                    "WARNING: rows were ADDED — a gsis_id slipped the existing-only "
                    "filter. Investigate before trusting the refresh.",
                    file=sys.stderr,
                )

            if args.apply:
                session.commit()
                print("Committed.")
            else:
                session.rollback()
                print("\nDRY RUN — no changes written. Re-run with --apply to commit.")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
