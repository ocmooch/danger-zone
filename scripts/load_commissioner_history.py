#!/usr/bin/env python3
"""Load commissioner history from data/commissioner_history.yaml into the DB.

Commissioner tenure is manually-curated metadata (it cannot be crawled from
NFL.com), so the version-controlled YAML is the source of truth and this loader
replaces the league's ``commissioners`` rows with its contents. Idempotent —
safe to re-run after editing the YAML.

Usage:
    uv run python scripts/load_commissioner_history.py
    uv run python scripts/load_commissioner_history.py --dry-run
    uv run python scripts/load_commissioner_history.py --db sqlite:///./data/fantasy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# Make the src layout importable when run directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_SEED = ROOT / "data" / "commissioner_history.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Load commissioner history into the DB.")
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    parser.add_argument("--seed", type=str, default=str(DEFAULT_SEED), help="Path to the seed YAML")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report, but do not write")
    args = parser.parse_args()

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.repository.models import Commissioner, League, Owner
    from ff_pipeline.settings import get_settings

    seed_path = Path(args.seed)
    raw = yaml.safe_load(seed_path.read_text())
    terms = (raw or {}).get("commissioners", [])
    if not terms:
        print(f"No commissioners found in {seed_path}; nothing to do.")
        return

    settings = get_settings()
    db_url = args.db or settings.database_url
    print(f"Database: {db_url}")
    print(f"Seed:     {seed_path} ({len(terms)} terms)")

    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)

    with Session(engine) as ss:
        league = ss.execute(select(League).order_by(League.league_id)).scalars().first()
        if league is None:
            print("ERROR: no league in the DB — run the Phase 1 pipeline first.")
            engine.dispose()
            sys.exit(1)

        valid_owner_ids = {o.owner_id: o.display_name for o in ss.execute(select(Owner)).scalars()}

        rows: list[Commissioner] = []
        for t in terms:
            owner_id = int(t["owner_id"])
            if owner_id not in valid_owner_ids:
                print(f"ERROR: owner_id {owner_id} not found in DB.")
                engine.dispose()
                sys.exit(1)
            from_year = int(t["from_year"])
            to_year = t.get("to_year")
            to_year = int(to_year) if to_year is not None else None
            rows.append(
                Commissioner(
                    league_id=league.league_id,
                    owner_id=owner_id,
                    from_year=from_year,
                    to_year=to_year,
                    notes=t.get("notes"),
                )
            )
            name = valid_owner_ids[owner_id] or str(owner_id)
            print(f"  {from_year}-{to_year or 'present':<7} {name}")

        if args.dry_run:
            print("Dry run — no changes written.")
            engine.dispose()
            return

        # The YAML is the source of truth: replace this league's rows wholesale.
        existing = (
            ss.execute(select(Commissioner).where(Commissioner.league_id == league.league_id))
            .scalars()
            .all()
        )
        for row in existing:
            ss.delete(row)
        ss.flush()
        ss.add_all(rows)
        ss.commit()
        print(f"Done. Wrote {len(rows)} commissioner terms (replaced {len(existing)}).")

    engine.dispose()


if __name__ == "__main__":
    main()
