"""Thin CLI wrapper that delegates to ``ff-pipeline backfill``.

The roadmap's M9 names this script as the entry point for the
multi-season backfill. The real implementation lives in
``ff_pipeline.backfill`` so it's importable + unit-testable; this
wrapper simply re-routes shell invocations through the Typer CLI so a
``python scripts/backfill.py --start 2014 --end 2025`` invocation works
identically to ``ff-pipeline backfill --start 2014 --end 2025``.

Usage::

    uv run python scripts/backfill.py --start 2014 --end 2025
"""

from __future__ import annotations

import sys

from ff_pipeline.cli import app


def main() -> None:
    sys.argv.insert(1, "backfill")
    app()


if __name__ == "__main__":
    main()
