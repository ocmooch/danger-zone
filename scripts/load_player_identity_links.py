#!/usr/bin/env python3
"""Load curated player identity links into the DB.

The YAML is the source of truth for manual same-player links. The loader is
idempotent and replaces the table contents so re-running after edits converges.

Usage:
    uv run python scripts/load_player_identity_links.py
    uv run python scripts/load_player_identity_links.py --dry-run
    uv run python scripts/load_player_identity_links.py --db sqlite:///./data/fantasy.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_SEED = ROOT / "data" / "player_identity_links.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Load player identity links into the DB.")
    parser.add_argument("--db", type=str, default=None, help="DATABASE_URL override")
    parser.add_argument("--seed", type=str, default=str(DEFAULT_SEED), help="Path to the seed YAML")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report, but do not write")
    args = parser.parse_args()

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.migrations import upgrade_to_head
    from ff_pipeline.repository.models import Player, PlayerIdentityLink
    from ff_pipeline.settings import get_settings

    seed_path = Path(args.seed)
    raw = yaml.safe_load(seed_path.read_text())
    links = list((raw or {}).get("links", []))
    triage = list((raw or {}).get("triage_decisions", []))

    settings = get_settings()
    db_url = args.db or settings.database_url
    print(f"Database: {db_url}")
    print(f"Seed:     {seed_path} ({len(links)} links, {len(triage)} triage decisions)")

    engine = create_app_engine(db_url)
    upgrade_to_head(engine=engine)

    with Session(engine) as ss:
        player_ids = {
            int(pid)
            for pid in ss.execute(select(Player.player_id)).scalars().all()
            if pid is not None
        }
        rows: list[PlayerIdentityLink] = []
        for item in links:
            member = int(item["member_player_id"])
            canonical = int(item["canonical_player_id"])
            if member == canonical:
                print(f"ERROR: member_player_id and canonical_player_id are both {member}.")
                engine.dispose()
                sys.exit(1)
            missing = [pid for pid in (member, canonical) if pid not in player_ids]
            if missing:
                print(f"ERROR: player_id(s) not found in DB: {missing}")
                engine.dispose()
                sys.exit(1)
            rows.append(
                PlayerIdentityLink(
                    member_player_id=member,
                    canonical_player_id=canonical,
                    source=str(item.get("source") or "manual"),
                    confidence=str(item.get("confidence") or "manual"),
                    notes=item.get("notes"),
                )
            )
            print(f"  link {member} -> {canonical}: {item.get('notes') or ''}")

        if args.dry_run:
            print("Dry run - no changes written.")
            engine.dispose()
            return

        existing = ss.execute(select(PlayerIdentityLink)).scalars().all()
        for row in existing:
            ss.delete(row)
        ss.flush()
        ss.add_all(rows)
        ss.commit()
        print(f"Done. Wrote {len(rows)} player identity links (replaced {len(existing)}).")

    engine.dispose()


if __name__ == "__main__":
    main()
