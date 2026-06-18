"""Report strong NFL.com player identity ownership conflicts.

Use ``--strict`` in verification/operations to exit nonzero when any conflict
remains. This is read-only.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.player_identity_integrity import source_identity_mismatches
from ff_pipeline.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    engine = create_app_engine(args.database_url or get_settings().database_url)
    try:
        with Session(engine) as session:
            mismatches = source_identity_mismatches(session)
    finally:
        engine.dispose()

    print(f"Source identity mismatches: {len(mismatches)}")
    for item in mismatches:
        print(
            f"  [{item['reason']}] pid={item['player_id']} "
            f"nflcom={item['nfl_com_player_id']} {item['name_full']} "
            f"{item['position']} observed={item['first_observed_season']}-"
            f"{item['last_observed_season']} career={item['rookie_year']}-"
            f"{item['last_season']}"
        )
    return 1 if args.strict and mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
