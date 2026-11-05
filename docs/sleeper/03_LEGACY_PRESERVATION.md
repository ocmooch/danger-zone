# 03 — Legacy Preservation: the NFL.com Era Vault

**Status:** planned, not started · **Priority:** ONE — gates every other milestone in this program
**Repo:** `danger-zone` · **Companion docs:** `00_SLEEPER_ERA_MASTER_PLAN.md`, `02_PIPELINE_BUILD_PLAN.md` (S0, S1)

> NFL Fantasy is defunct. The 2010–2025 league record in `data/fantasy.db` cannot be re-scraped,
> re-downloaded or reconstructed from anywhere else. Treat it like a museum collection: sealed,
> catalogued, copied off-site, and only ever amended through a logged procedure.

---

## 1. What exists today (audit, 2026-11-05)

| Finding | Evidence | Risk |
|---------|----------|------|
| The DB is the **only** primary record | `SAVE_RAW_HTML=false` default; no `data/raw_pages/` | No second source to rebuild from |
| All backups are on the **same disk** as the live DB | 16 `fantasy.db.bak-*` files in `data/`, 3.1 GB in `data/backups/` | One disk failure loses everything |
| That disk is a **WSL2 virtual disk** | `Linux …microsoft-standard-WSL2` | A single `ext4.vhdx` file on the Windows host holds every copy |
| No checksums or manifests | Backup helper writes files only | Silent corruption or edits go unnoticed |
| `ff-pipeline backup --keep-days` **deletes** older dated backups | `observability._prune_backups` | A corrupted live DB can rotate out the last good copy |
| Many legacy repairs were made **in place** | `scripts/archive/`, hamlin override, owner repairs | Correct, but nothing proves *which* state is canonical |
| A 2026 NFL.com stub season exists | season 2026 `in_progress`, 12 teams, 193 week-1 rows (2026-06-05) | Collides with the Sleeper 2026 season (decision D-2) |
| Avatar bytes live outside the DB | `data/assets/` (171 assets) | Easy to forget in a DB-only backup |
| Curated seed files carry irreplaceable judgment | `data/commissioner_history.yaml`, `player_identity_links.yaml`, `source_player_identity_repairs.json` | Small, but lost context is expensive |

Good news: the live DB is healthy and the backup command already uses SQLite's online backup API, so
copies are page-consistent. The work is about **independence, verification and protection**, not
recovery.

## 2. Threat model

| # | Threat | Example | Primary control |
|---|--------|---------|-----------------|
| T1 | Hardware / VHDX loss | Laptop dies; WSL distro unregistered | Off-machine copies (§4) |
| T2 | Accidental mutation | A Sleeper loader matches season 2015 by mistake; a migration rewrites a column | DB triggers + fingerprint preflight (§6, §7) |
| T3 | Well-meant "repair" that is wrong | An identity merge re-points 2012 roster rows | Amendment protocol: backup + reason + diff (§8) |
| T4 | Silent corruption / bit rot | A page flips in an old backup | SHA-256 manifest, `PRAGMA integrity_check`, periodic verify |
| T5 | Retention deleting the last good copy | `--keep-days 30` rotates out pre-incident backups | Sealed vault lives outside every prune path |
| T6 | Format / tooling obsolescence | SQLAlchemy or schema drift in 10 years | Plain-text exports (CSV + JSONL) alongside the `.db` |
| T7 | Lost meaning | Nobody remembers what `team_abbrev` means | Data dictionary snapshot inside the vault |
| T8 | Sync/ransomware deleting cloud copies | Cloud folder mirrors a deletion | Snapshot-based, append-only backup tool (restic) with retention |

## 3. What is precious — table classification

The seal covers **irreplaceable** data. **Re-derivable** data is included in the full DB copy for
convenience but is *not* fingerprinted and *not* write-locked, because the pipeline legitimately
re-derives it (rescoring, stat corrections, new nflverse releases).

| Class | Tables / files | Guarded? |
|-------|----------------|----------|
| **Irreplaceable — league facts** | `leagues`, `seasons` (2010–2025 + 2026 stub), `owners`, `owner_identity_overrides`, `commissioners`, `teams`, `matchups`, `team_rosters` (incl. `extra_data.nfl_com_points`), `transactions` (incl. FAAB `extra_data`), `player_availability`, `scoring_rules` | **Yes** — fingerprinted + triggers |
| **Irreplaceable — identity judgment** | `players.nfl_com_player_id` column (the NFL.com ↔ player mapping), `player_id_overrides`, `player_identity_links` | Fingerprinted; changes go through amendments |
| **Irreplaceable — files** | `data/assets/**` bytes, `data/*.yaml`, `data/*.json` seeds, `tests/fixtures/nfl_com_html/**` | Hashed in manifest |
| **Re-derivable** | `player_stats_raw`, `player_stats_scored`, `projections`, `player_adp`, `player_injury_reports`, `trending_players`, `player_season_positions`, nflverse columns on `players` | Not guarded (copied in the `.db` only) |
| **Operational history** | `pipeline_runs`, `source_health` | Copied, not guarded |

> The "legacy column set" for each guarded table is frozen **at seal time** and written into the
> manifest. Columns added later by additive migrations (e.g. `seasons.platform`) are outside the
> fingerprint, so additive schema work cannot falsely trip verification — but any change to an
> original column value will.

## 4. The vault

### 4.1 Layout

```
ff-legacy-vault/
└── seal-v1-2026-11-XX/
    ├── README.md                     # what this is, how to verify, how to restore (plain English)
    ├── MANIFEST.json                 # see §5; its own sha256 recorded in git (D-8)
    ├── MANIFEST.json.sha256
    ├── db/
    │   ├── fantasy-legacy-seal-v1.db         # SQLite online backup; integrity_check = ok
    │   └── schema.sql                        # `.schema` dump at alembic head d4e6f8a0b2c4
    ├── tables/                       # format-independent copies of every guarded table
    │   ├── teams.csv.gz   teams.jsonl.gz
    │   ├── matchups.csv.gz  …                # sorted by PK, UTF-8, ISO-8601 timestamps
    ├── assets/                       # byte-exact copy of data/assets/**
    ├── seeds/                        # data/*.yaml, data/*.json
    ├── fixtures/                     # tests/fixtures/nfl_com_html/** (real NFL.com page captures)
    ├── dictionary/                   # copies of docs/04_DATA_MODEL.md, 05_SCORING_ENGINE.md,
    │                                 # PRE2016_STRUCTURAL_REFERENCE.md, RUNBOOK.md at seal commit
    └── verify.py                     # stdlib-only verifier: hashes, row counts, integrity_check
```

Why both SQLite and CSV/JSONL: SQLite is a recommended long-term archival format and keeps
relationships intact; CSV/JSONL survives any future tooling and can be diffed by eye. `verify.py`
uses only the Python standard library so the vault can be checked without this repo or `uv`.

### 4.2 Copies (3-2-1)

Decided 2026-11-05 (D-7): off-machine copies go to cloud accounts the user controls.

| Copy | Location | Medium | Mutability |
|------|----------|--------|------------|
| A | `~/ff-legacy-vault/` inside WSL, **outside** the repo `data/` dir | WSL VHDX | `chmod -R a-w` after write |
| B | Google Drive — `ff-legacy-vault/seal-vN.tar.age` | cloud | keep every version; enable Drive file versioning |
| C | iCloud Drive — same archive | cloud | keep every version |
| D | Separate **private** GitHub repo `ocmooch/ff-legacy-vault` — archive attached as a release asset per seal (`gh release create seal-v1 seal-v1.tar.age`), plus `MANIFEST.json` and `verify.py` committed in plain text | cloud | releases are never deleted; repo stays private |

**Not in `danger-zone` or `dz-dashboard`.** Both repos are **public**. Anything committed there,
even in a "protected" directory, is publicly readable and stays in git history forever. Only the
hash manifest (no names, no data) is committed to `danger-zone` (D-8).

**Encryption.** The vault contains league members' names, usernames and avatars, so every
off-machine copy is one `age`-encrypted tarball (`seal-vN.tar.age`, ≈ 25–30 MB compressed; the
full live DB compresses to ~24 MB with `gzip -1`). The passphrase lives in the user's password
manager, never in `.env` or a repo. `verify.py` checks the decrypted copy.

Encryption uses two `age` public keys, so the weekly job never holds a decryption secret. Uploads
are automated (weekly via Windows Task Scheduler, and right after every seal), verified by hash per
destination, and rotated to at most 5 copies per destination with seal-v1 pinned. Full design,
setup checklist and the key/passphrase guidance: **`05_VAULT_OFFSITE_SYNC.md`**.

### 4.3 Seal versions

- **seal-v1** — NFL.com era, 2010–2025, plus the 2026 NFL.com stub as found on 2026-06-05.
- **seal-v2, v3…** — created after any approved legacy amendment (§8) and at each Sleeper season close
  (rolling seal, §10). Older seals are never deleted.

## 5. Manifest and fingerprints

`MANIFEST.json` (abridged):

```json
{
  "seal": "seal-v1",
  "created_at": "2026-11-XXT..Z",
  "git_commit": "<danger-zone HEAD>",
  "alembic_revision": "d4e6f8a0b2c4",
  "sqlite_integrity_check": "ok",
  "files": { "db/fantasy-legacy-seal-v1.db": "sha256:…", "assets/ab/ab12….png": "sha256:…" },
  "tables": {
    "matchups": {
      "legacy_columns": ["matchup_id","season_id","week","team_id","opponent_team_id",
                          "team_score","opponent_score","is_win","is_playoff","is_consolation",
                          "nfl_com_game_id"],
      "row_count": 3002,
      "table_sha256": "…",
      "by_season": { "2010": {"rows": 182, "sha256": "…"}, "…": {} }
    }
  }
}
```

**Fingerprint algorithm (deterministic, stdlib-reproducible):**

1. Select only `legacy_columns`, ordered by primary key.
2. Serialize each row as canonical JSON: keys sorted, floats via `repr()`, booleans as `true/false`,
   timestamps as stored text, JSON columns re-dumped with sorted keys, NULL as `null`.
3. `sha256` over the newline-joined rows → `table_sha256`; the same per season (via `season_id` /
   `season_year`, or through `team_id → teams.season_id`) → `by_season`.
4. `created_at` / `updated_at` are **excluded** (bookkeeping, not facts).

Per-season hashes make a failure *locatable*: "matchups 2015 changed" instead of "something changed".

## 6. Write guard (live DB)

### 6.1 Seal flag

Additive migration: `seasons.is_sealed BOOLEAN NOT NULL DEFAULT 0`, `seasons.sealed_at`,
`seasons.seal_id`. Seal v1 sets `is_sealed = 1` for 2010–2025 (and the 2026 stub rows are covered
by the quarantine decision D-2).

### 6.2 DB-level triggers (defence in depth)

SQLite triggers on every guarded table abort `UPDATE` and `DELETE` on rows that belong to a sealed
season, and `INSERT` of new rows into a sealed season, unless an unlock row is present:

```sql
CREATE TABLE legacy_guard (id INTEGER PRIMARY KEY CHECK (id = 1),
                           unlocked_until TEXT, unlocked_reason TEXT);

CREATE TRIGGER guard_matchups_update BEFORE UPDATE ON matchups
WHEN (SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1
 AND COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%S','now')
BEGIN SELECT RAISE(ABORT, 'legacy_guard: matchups row belongs to a sealed season'); END;
```

- Triggers fire for **any** writer — the ORM, raw SQL in a one-off script, or the `sqlite3` shell.
- They are created by an alembic migration and recorded in the manifest so their removal is
  detectable (`legacy verify` checks `sqlite_master` for every guard trigger).
- Postgres portability: the same guard is expressible as a `BEFORE` trigger function; noted, not built.
- The guard intentionally does **not** cover re-derivable tables, so `rescore`, nflverse refreshes
  and projection backfills keep working on legacy years.

### 6.3 Application-level guard

`repository/legacy_guard.py` adds a fast, readable failure before the DB trigger:
`assert_writable(session, season_id)` is called by the shared canonical loader (pipeline doc §6)
and raises `SealedSeasonError` naming the season and the amendment command. The Sleeper loader
resolves seasons only by `(league_id, year, platform='sleeper')`, so a legacy season can never be
its target in the first place.

## 7. Verification

| Check | Command | When | Cost |
|-------|---------|------|------|
| Quick | `ff-pipeline legacy verify --quick` (row counts + per-table hashes + trigger presence) | Preflight of **every** `ff-pipeline run`, `backfill`, `migrate up`; failure aborts the run | ~seconds |
| Full | `ff-pipeline legacy verify` (adds per-season hashes, `integrity_check`, asset hashes) | Weekly in season; after any amendment or migration | ~1 min |
| Vault | `python verify.py` inside each vault copy | Monthly; after copying to a new medium | ~1 min |
| Restore drill | `ff-pipeline legacy restore-drill --seal seal-v1 --to <scratch>` then dashboard smoke against it | Quarterly + before each release | ~10 min |

A failing quick check prints the table, season and the first differing primary keys, and points at
the newest vault copy for comparison. It never auto-repairs.

## 8. Amendment protocol (the only way to change sealed data)

Legitimate legacy corrections will still happen (the Hamlin no-contest override is the model case).
They go through one path:

```
ff-pipeline legacy amend --reason "Correct 2019 wk7 trade leg pairing (issue #NN)" -- python scripts/fix_x.py --apply
```

1. Requires a clean `legacy verify` first.
2. Takes a named backup (`fantasy-pre-amend-<slug>-<ts>.db`) **outside** retention pruning.
3. Writes a time-boxed unlock row (default 10 minutes), runs the script, removes the unlock.
4. Computes the fingerprint diff (tables, seasons, primary keys changed) and appends a row to
   `legacy_amendments` (append-only: `amendment_id, created_at, reason, script, git_commit,
   before_seal, tables_changed JSON, rows_changed JSON`).
5. Produces **seal-vN+1** and pushes it to all vault copies; the previous seal is kept.

Preferred alternative to mutation: **overlays.** Where a correction is interpretive rather than a
fix of a capture error, store it as a new row (e.g. a `matchup_annotations` entry) and leave the
captured fact untouched.

## 9. Migration policy during the Sleeper program

- Every migration in this program is **additive** (new nullable/defaulted columns, new tables,
  new indexes, new triggers). No drops, renames, type changes or data rewrites on guarded tables.
- Each migration's PR includes a run against a **copy of seal-v1** with `legacy verify` before and
  after; the PR body records "fingerprints unchanged".
- A test fixture (`tests/integration/test_legacy_guard.py`) builds a tiny sealed season, runs every
  migration `upgrade` → `downgrade` → `upgrade`, and asserts fingerprints are identical.
- SQLite `ADD COLUMN … DEFAULT` does not rewrite existing row values for original columns, so
  fingerprints over `legacy_columns` remain stable — the test above enforces it.

## 10. Rolling seal for Sleeper seasons

A Sleeper season is sealed when: the league status is `complete`, the final week has passed the
stat-correction window (Wednesday after the last week), the S6 reconcile shows no unexplained
divergence, and the user confirms the champion. Then `ff-pipeline legacy seal --season 2026`
produces the next seal version (covering the new season too), flips `is_sealed`, and pushes copies.
The season's **raw Sleeper payload archive** (pipeline doc §4) is included in that seal, so the
Sleeper era never depends on Sleeper staying online.

## 11. Immediate checklist (S0 — do before any Sleeper schema work)

1. Confirm no repair is mid-flight (`git status` clean on both repos; no pending `scripts/*` apply).
2. Run `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on `data/fantasy.db` (read-only).
3. Produce seal-v1 with a stand-alone script (`scripts/legacy_seal_v1.py`, stdlib + sqlite3 only,
   read-only on the live DB) — the CLI wrapper comes in S1.
4. Verify the vault copy with `verify.py`; make it read-only.
5. Encrypt (`age`) and upload to Google Drive, iCloud Drive and the private `ff-legacy-vault`
   release (D-7); download each once, decrypt to scratch and run `verify.py`.
6. Commit `docs/sleeper/seals/seal-v1.manifest.json` (hashes and counts only) — tamper evidence (D-8).
7. Move existing `data/fantasy.db.bak-*` files into `data/backups/historical/` (excluded from
   pruning) so retention can never remove them; do not delete any.
8. Record the seal in `docs/RUNBOOK.md` → "Legacy vault" and in memory notes.

## 12. Restore procedure (summary)

1. Pick the newest seal whose `verify.py` passes.
2. Copy `db/fantasy-legacy-seal-vN.db` to a scratch path; run `PRAGMA integrity_check`.
3. If restoring the live DB: stop scheduled runs, move the damaged DB aside (never delete), copy
   the seal DB into place, `ff-pipeline migrate up` (additive migrations re-apply), then re-run the
   re-derivable pipelines (nflverse, rescore, projections) and any Sleeper loads (replayable from
   the raw archive without network).
4. `ff-pipeline legacy verify` must pass; dashboard smoke test must pass.
5. Copy `assets/` back if the asset store was affected.
