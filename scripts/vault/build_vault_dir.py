"""Build a plain (unencrypted) vault directory: a seal or a weekly snapshot.

Standard library + sqlite3 only. Opens the live DB **read-only** and copies it
with SQLite's online backup API. See docs/sleeper/03_LEGACY_PRESERVATION.md §4-5.

Seal layout (snapshots carry db/, assets/, seeds/, raw/ and the manifest only):

    <label>/README.md  MANIFEST.json  MANIFEST.json.sha256  verify.py
            db/fantasy-legacy-<label>.db  db/schema.sql
            tables/<t>.csv.gz  tables/<t>.jsonl.gz
            assets/  seeds/  fixtures/  dictionary/
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify as vfy

REPO = Path(__file__).resolve().parents[2]
LIVE_DB = REPO / "data" / "fantasy.db"

# Irreplaceable league facts — every column except bookkeeping is fingerprinted.
GUARDED_TABLES = (
    "leagues",
    "seasons",
    "owners",
    "owner_identity_overrides",
    "commissioners",
    "teams",
    "matchups",
    "team_rosters",
    "transactions",
    "player_availability",
    "scoring_rules",
    "player_id_overrides",
    "player_identity_links",
)
# Identity judgment on players: only the NFL.com mapping is fingerprinted;
# the rest of the row is re-derivable (nflverse) and changes legitimately.
COLUMN_SCOPED = {"players": ["player_id", "nfl_com_player_id"]}

DICTIONARY_DOCS = (
    "docs/04_DATA_MODEL.md",
    "docs/05_SCORING_ENGINE.md",
    "docs/PRE2016_STRUCTURAL_REFERENCE.md",
    "docs/RUNBOOK.md",
)

SEAL_README = """\
# {label} — NFL.com-era legacy vault

This directory is a sealed, read-only copy of the fantasy league's history as
it stood on {date} (danger-zone commit {commit}).

- `db/` — a complete SQLite copy of the league database plus its schema.
- `tables/` — every irreplaceable table as gzipped CSV and JSON Lines, so the
  data stays readable without SQLite.
- `assets/`, `seeds/`, `fixtures/`, `dictionary/` — team/owner images, hand-
  curated seed files, captured NFL.com pages, and the docs that explain the
  data model and scoring.
- `MANIFEST.json` — sha256 of every file plus row counts and fingerprints of
  every irreplaceable table. Its own hash is in `MANIFEST.json.sha256` and in
  the danger-zone repo at `docs/sleeper/seals/{label}.manifest.json`.

## Verify

    python3 verify.py

Needs only Python 3 (no packages). It re-hashes every file, runs SQLite's
integrity check, and recomputes every table fingerprint.

## Restore

See danger-zone `docs/sleeper/03_LEGACY_PRESERVATION.md` §12. In short: copy
`db/fantasy-legacy-{label}.db` to `data/fantasy.db`, run `ff-pipeline migrate up`,
then re-run the re-derivable pipelines.
"""


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    return out.stdout.strip()


def _gzip_bytes(data: bytes) -> bytes:
    """Reproducible gzip (fixed mtime, no embedded filename)."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(data)
    return buf.getvalue()


def _copy_tree(src: Path, dst: Path, pattern: str = "**/*") -> int:
    count = 0
    for path in sorted(src.glob(pattern)):
        if path.is_file():
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            count += 1
    return count


def backup_db(source: Path, dest: Path) -> None:
    """Online backup of a read-only source into a single-file (non-WAL) copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()


def dump_schema(conn: sqlite3.Connection, dest: Path) -> None:
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"
    )
    dest.write_text("".join(f"{r[0]};\n\n" for r in rows))


def export_table(conn: sqlite3.Connection, table: str, out_dir: Path) -> None:
    """All columns, ordered by primary key, as csv.gz and jsonl.gz."""
    cols = vfy.table_columns(conn, table)
    names = [c[0] for c in cols]
    types = [c[1] for c in cols]
    order = ", ".join(f'"{c}"' for c in vfy.primary_key(conn, table))
    select = ", ".join(f'"{c}"' for c in names)
    rows = conn.execute(f'SELECT {select} FROM "{table}" ORDER BY {order}').fetchall()

    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(names)
    writer.writerows(["" if v is None else v for v in row] for row in rows)
    (out_dir / f"{table}.csv.gz").write_bytes(_gzip_bytes(text.getvalue().encode()))

    lines = "".join(vfy.canonical_row(names, types, row) + "\n" for row in rows)
    (out_dir / f"{table}.jsonl.gz").write_bytes(_gzip_bytes(lines.encode()))


def build(
    out_dir: Path,
    label: str,
    kind: str,
    *,
    source_db: Path = LIVE_DB,
    repo: Path = REPO,
    extra_dirs: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Build ``out_dir`` and return its manifest. ``kind`` is "seal" or "snapshot"."""
    if out_dir.exists():
        raise SystemExit(f"refusing to overwrite existing {out_dir}")
    is_seal = kind == "seal"
    out_dir.mkdir(parents=True)
    db_rel = f"db/fantasy-legacy-{label}.db" if is_seal else "db/fantasy.db"
    db_path = out_dir / db_rel

    backup_db(source_db, db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        alembic = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"integrity_check failed on the copy: {integrity}")
        dump_schema(conn, out_dir / "db" / "schema.sql")

        tables: dict[str, Any] = {}
        specs = [
            (t, [c[0] for c in vfy.table_columns(conn, t) if c[0] not in vfy.BOOKKEEPING_COLUMNS])
            for t in GUARDED_TABLES
        ] + list(COLUMN_SCOPED.items())
        for table, legacy_cols in specs:
            tables[table] = vfy.fingerprint_table(conn, table, legacy_cols)
        if is_seal:
            (out_dir / "tables").mkdir()
            for table, _ in specs:
                export_table(conn, table, out_dir / "tables")
    finally:
        conn.close()

    _copy_tree(repo / "data" / "assets", out_dir / "assets")
    seeds = out_dir / "seeds"
    seeds.mkdir()
    for path in sorted((repo / "data").glob("*.yaml")) + sorted((repo / "data").glob("*.json")):
        shutil.copy2(path, seeds / path.name)
    if is_seal:
        _copy_tree(repo / "tests" / "fixtures" / "nfl_com_html", out_dir / "fixtures")
        (out_dir / "dictionary").mkdir()
        for rel in DICTIONARY_DOCS:
            shutil.copy2(repo / rel, out_dir / "dictionary" / Path(rel).name)
    for name, src in (extra_dirs or {}).items():
        if src.is_dir():
            _copy_tree(src, out_dir / name)

    commit = _git("rev-parse", "HEAD")
    now = datetime.now(tz=UTC)
    if is_seal:
        (out_dir / "README.md").write_text(
            SEAL_README.format(label=label, date=now.date().isoformat(), commit=commit[:12])
        )
    shutil.copy2(Path(vfy.__file__), out_dir / "verify.py")

    files = {
        p.relative_to(out_dir).as_posix(): "sha256:" + vfy.sha256_file(p)
        for p in sorted(out_dir.rglob("*"))
        if p.is_file()
    }
    manifest = {
        "label": label,
        "kind": kind,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": commit,
        "alembic_revision": alembic,
        "sqlite_integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "database": db_rel,
        "files": files,
        "tables": tables,
    }
    manifest_path = out_dir / vfy.MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out_dir / vfy.MANIFEST_SHA_NAME).write_text(
        f"{vfy.sha256_file(manifest_path)}  {vfy.MANIFEST_NAME}\n"
    )
    return manifest
