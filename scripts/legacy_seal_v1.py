#!/usr/bin/env python3
"""Produce Legacy Seal v1 — the certified copy of the NFL.com era (S0).

    python3 scripts/legacy_seal_v1.py            # -> ~/ff-legacy-vault/seal-v1-<date>/
    python3 scripts/legacy_seal_v1.py --vault DIR

Read-only on data/fantasy.db. Steps: pre-flight integrity checks on the live DB,
build the seal directory, run its verify.py, then make it read-only
(chmod -R a-w). Encrypting and shipping it is `scripts/vault/vault_sync.sh pack-seal`.
See docs/sleeper/03_LEGACY_PRESERVATION.md §11.
"""

from __future__ import annotations

import argparse
import sqlite3
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "vault"))
import build_vault_dir as builder
import verify as vfy

LABEL = "seal-v1"


def preflight(db: Path, allow_dirty: bool) -> None:
    dirty = subprocess.run(
        ["git", "-C", str(builder.REPO), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty and not allow_dirty:
        raise SystemExit(f"repo has uncommitted changes — commit first:\n{dirty}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fks = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if integrity != "ok" or fks:
        raise SystemExit(f"live DB not clean: integrity={integrity}, fk violations={len(fks)}")
    print(f"pre-flight ok: integrity_check=ok, foreign_key_check=0 ({db})")


def make_read_only(root: Path) -> None:
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        path.chmod(path.stat().st_mode & ~write_bits)
    root.chmod(root.stat().st_mode & ~write_bits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault", type=Path, default=Path.home() / "ff-legacy-vault")
    parser.add_argument("--db", type=Path, default=builder.LIVE_DB)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()

    preflight(args.db, args.allow_dirty)
    out = args.vault / f"{LABEL}-{date.today().isoformat()}"
    print(f"building {out} …")
    manifest = builder.build(out, LABEL, "seal", source_db=args.db)
    print(f"  {len(manifest['files'])} files, {len(manifest['tables'])} fingerprinted tables")
    if vfy.verify(out) != 0:
        return 1
    make_read_only(out)
    sha = (out / vfy.MANIFEST_SHA_NAME).read_text().split()[0]
    print(f"\nsealed (read-only): {out}\nMANIFEST.json sha256: {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
