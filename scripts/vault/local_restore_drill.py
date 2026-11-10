"""Key-free local restore drill (S9-D7; ``03_LEGACY_PRESERVATION.md`` §12).

Restores the newest plaintext seal directory (``~/ff-legacy-vault/seal-v<N>-*``,
read-only: only copied from, never modified) into a scratch directory and proves
it is a working database:

1. copy ``db/fantasy-legacy-<seal>.db`` out; ``PRAGMA integrity_check``;
2. ``ff-pipeline migrate up`` (additive migrations re-apply);
3. re-adopt the committed manifest (``legacy seal --adopt``) and ``legacy verify``:
   the restored rows must match the committed seal byte for byte;
4. dashboard smoke: the dz-dashboard BFF serves the season list and one box score
   per era from the copy.

Prints one result line to paste into ``docs/sleeper/drills.md``. The encrypted
path (decrypting a vault archive with a key) is ``restore_check.sh``, run by the
user. The scratch copy is deleted at the end.

    uv run python scripts/vault/local_restore_drill.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VAULT = Path(os.environ.get("DZ_VAULT_DIR", Path.home() / "ff-legacy-vault"))
DASHBOARD = REPO.parent / "dz-dashboard"

SMOKE = """
import sys
from fastapi.testclient import TestClient
from ff_pipeline.repository.database import create_app_engine
from ff_dashboard.api.main import create_app
engine = create_app_engine(sys.argv[1])
c = TestClient(create_app(engine=engine))
seasons = c.get("/v1/seasons").json()["data"]["seasons"]
assert seasons, "no seasons served"
import sqlite3
db = sqlite3.connect(sys.argv[1].removeprefix("sqlite:///"))
ids = [r[0] for r in db.execute(
    "SELECT min(m.matchup_id) FROM matchups m JOIN seasons s USING(season_id) "
    "GROUP BY s.platform")]
for mid in ids:
    r = c.get(f"/v1/matchups/{mid}/box-score")
    assert r.status_code == 200, (mid, r.status_code)
print(f"smoke ok: {len(seasons)} seasons, {len(ids)} box score(s)")
"""


def newest_seal_dir() -> Path:
    dirs = sorted(
        (p for p in VAULT.glob("seal-v*-*") if (p / "db").is_dir()),
        key=lambda p: (int(p.name.split("-")[1][1:]), p.name),
    )
    if not dirs:
        raise SystemExit(f"no plaintext seal directory under {VAULT}")
    return dirs[-1]


def run(cmd: list[str], env: dict[str, str], cwd: Path = REPO) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(cmd[:4])}…\n{(proc.stdout + proc.stderr)[-1500:]}")
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def main() -> int:
    seal_dir = newest_seal_dir()
    label = seal_dir.name.rsplit("-", 3)[0]  # seal-v1-2026-11-07 -> seal-v1
    source = seal_dir / "db" / f"fantasy-legacy-{label}.db"
    manifest = REPO / "docs" / "sleeper" / "seals" / f"{label}.manifest.json"
    scratch = Path(tempfile.mkdtemp(prefix="dz-restore-drill-"))
    try:
        db = scratch / "restored.db"
        shutil.copy2(source, db)
        db.chmod(0o644)
        conn = sqlite3.connect(db)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if integrity != "ok":
            raise SystemExit(f"FAILED: integrity_check {integrity}")
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "LOG_LEVEL": "WARNING"}
        ff = ["uv", "run", "ff-pipeline"]
        run([*ff, "migrate", "up"], env)
        status = run([*ff, "legacy", "status"], env)
        if "No seal" in status or "not adopted" in status.lower() or "none" in status.lower():
            run([*ff, "legacy", "seal", "--adopt", str(manifest), "--years", "2010-2025"], env)
        verified = run([*ff, "legacy", "verify"], env)
        smoke = run(["uv", "run", "python", "-c", SMOKE, f"sqlite:///{db}"], env, cwd=DASHBOARD)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(
        f"{datetime.now(tz=UTC):%Y-%m-%d} | local drill | {seal_dir.name} | integrity ok; "
        f"migrate up ok; {verified}; {smoke}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
