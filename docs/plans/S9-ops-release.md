# S9 — Operations and release (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S9. Design sources: `03_LEGACY_PRESERVATION.md`
§7 (verification), §8 (amendments), §10 (rolling seal), §12 (restore); `05_VAULT_OFFSITE_SYNC.md`;
`01_PIPELINE_ARCHITECTURE.md` §8.1-8.2 (lifecycle, rollover); `docs/08_OPERATIONS.md`.

**Status:** PLAN, written 2026-11-09 on `feature/s9-ops-release`. Nothing is built yet.
**The decisions at the end need the user's approval before the BUILD session.**

## Goal

The Sleeper era runs itself with as few scheduled jobs as possible, a finished season becomes
protected history (sealed, vaulted), the next season is picked up without code changes, and the
pipeline is released (`dev → main`, tagged).

## What changed since §S9 was written

§S9 assumed daily in-season capture and load, Tuesday/Thursday correction passes and a daily
players directory. Since then:

- The user prefers **retroactive capture after the games** over time-critical jobs (S7-D7).
- The live load is **gated** until the dashboard is ready (S5-D1).
- **Each capture re-fetches every week of the season.** A stat correction made after one Wednesday
  is caught the next Wednesday as a new revision, and the load's sync applies it.

## Starting state (verified 2026-11-09)

- **Scheduled (Windows Task Scheduler, runs the checked-out branch):**
  - `dz-sleeper-capture`: Wednesday 06:00. Runs `weekly_capture.sh`, which captures and then runs
    `legacy verify --quick`.
  - `dz-vault-sync`: Sunday 03:00. Weekly encrypted snapshot (DB + `raw/sleeper` + assets) to
    local, iCloud, Drive and GitHub; keeps 4 rotating copies plus pinned seal-v1.
  - No cron is installed (the NFL.com-era `scripts/cron.example` and `08_OPERATIONS.md`
    "Scheduling" are stale).
- **Seal machinery (S1):**
  - `legacy seal --adopt <manifest> --years` is one-time (seal-v1, 2010-2025); `amend` produces
    seal-vN+1 after an audited change. There is **no "seal a finished season" command**.
  - The triggers guard each table's **seal-v1 column list**, so the Sleeper-era columns (S3:
    `extra_data`, `platform_*`, `status`, …) and the new tables (`playoff_brackets`, …) would
    stay writable even after 2026 is sealed.
- **Rollover:** `lineage.walk_lineage` walks `previous_league_id` **backward** from
  `SLEEPER_LEAGUE_ID`. Nothing yet discovers the renewed 2027 league (`01` §8.2 planned it via
  the commissioner's `/user/<id>/leagues/nfl/<year>`).
- **Restore check:** `scripts/vault/restore_check.sh` decrypts with the user's key and must be run
  **by the user** (passphrase; "never through Claude"). No drill has been logged yet.
- **Release:** `main` is 68 commits behind `dev` (last promotion 2026-06-23, tag `v1.6.0`).
- The open danger-zone PR #64 (`.gitignore` for `/exports/`) awaits the user's merge.

## Design

### 1. One weekly job (S9-D1, S9-D2)

`weekly_capture.sh`, still Wednesday 06:00, becomes:

1. Capture (as today; also follows a renewed league, §3).
2. **Only when `SLEEPER_AUTO_LOAD=1`** (set by the go-live, off until then), run the season
   update:
   - `sleeper-league load`;
   - `adopt-gsis --apply` (exact, unambiguous pairs only; the rest is reported);
   - `injury-reports`;
   - `run --source nflverse`;
   - `run --source team_defense`;
   - `sleeper-league stats`;
   - `rescore`;
   - `reconcile --out data/reports/<season>-reconcile.md` (not committed).

   Rules are never rewritten unattended: a change to the league's scoring settings makes the
   job report "rules changed, run `sleeper-league rules --diff`" and skip the rescore.
3. `legacy verify` (**full**, not `--quick`).
4. One summary line per step plus the exit code, in `data/logs/sleeper_capture-<date>.log` and
   in `pipeline_runs` / `source_health` (the dashboard's data-as-of already reads those).

Each step is a writing command with its own preflight. Any failure stops the run, with exit ≠ 0
("Last Result" in Task Scheduler). There are no silent retries; the next Wednesday re-runs
everything idempotently.

### 2. `ff-pipeline sleeper-league update`

The step-2 sequence as one command (so the job, the go-live and a manual catch-up run the same
thing), with `--dry-run` (stop after the load's dry run) and a summary. The go-live sequence
becomes: `update` once by hand (approved), then set `SLEEPER_AUTO_LOAD=1`.

### 3. Rollover (S9-D4)

When `/state/nfl` reports a season newer than the newest league in the chain, capture asks
Sleeper for the commissioner's leagues for that season (`/user/<commissioner>/leagues/nfl/<yr>`;
the commissioner is the `is_owner` user) and picks the one whose `previous_league_id` is the
current league. It then archives it like any league. The log says "renewed league found:
<id>"; `lineage` shows it. The load of a new season is **not** automatic: it needs the user's
go-ahead (new owners to map via `owners map`, a new season row, rules sign-off).

Test: a three-season fixture chain plus a renewal response → discovered, archived, no load.

### 4. Season close and the rolling seal (S9-D5, S9-D6)

`ff-pipeline legacy seal-season --season 2026 --champion "<team name>"` refuses unless:

- the league is `complete`;
- the final week is ≥ 7 days old (past the correction window);
- the last reconcile has no `rule` / `unexplained` divergences;
- the bracket re-probe passed (places written, no inverted warning);
- the `--champion` the user types matches `seasons.champion_team_id`.

It then:

1. takes a named backup outside retention (`fantasy-pre-seal-2026-<ts>.db`);
2. builds `seal-vN` (the S0 layout: DB snapshot, `raw/sleeper`, assets, manifest, `verify.py`)
   and runs `verify.py` on it;
3. sets `is_sealed` for 2026, writes the new `legacy_seals` baseline (fingerprints incl. the
   Sleeper-era coverage, §5) and records it in `legacy_amendments` style;
4. commits-ready: `docs/sleeper/seals/seal-vN.manifest.json` (+ sha256), hashes and counts only,
   no names;
5. hands off to `vault_sync.sh pack-seal` to encrypt and upload to every destination.

Retention keeps **every** `seal-*` forever (today only seal-v1 is pinned). The
`ff-legacy-evergreen` folders are never touched (they stay seal-v1 only).

Built and tested on a fixture (a completed synthetic Sleeper season); **run for real only at the
2026 season close (~mid-January 2027) with the user**.

### 5. Guard coverage for Sleeper seasons (S9-D6)

An additive migration (native `CREATE TRIGGER`, never batch) adds a second trigger set, which
guards for **sealed Sleeper seasons**:

- the S3 columns of the guarded tables (`teams.platform_team_key, division, extra_data`;
  `matchups.platform_matchup_key, extra_data`; `transactions.platform_transaction_id, status`;
  `team_rosters.extra_data`; `seasons.platform*`, `settings_snapshot`; S7 placements);
- `playoff_brackets` as a new season-scoped guarded table.

Fingerprints for `verify` cover the same set. Left out on purpose (re-derivable from the sealed
raw archive or nflverse): `player_stats_raw/scored`, `player_status_snapshots`,
`player_injury_reports`, `source_payloads`. `traded_picks` is out too (future picks, not a past
season). No NFL.com row is affected (they have no Sleeper-era values; seal-v1 is unchanged).
Live migration needs the user's OK; it changes nothing until a Sleeper season is sealed.

### 6. Restore drill (S9-D7)

- Quarterly and before each release, the user runs `restore_check.sh primary <newest archive>`
  in a plain terminal and pastes the result line.
- Claude then runs a key-free **local drill** of the `03` §12 procedure: copy the DB out of the
  newest plaintext seal directory (`~/ff-legacy-vault/seal-v<N>-*`, read-only, never modified) into
  scratch, `PRAGMA integrity_check`, `migrate up`, `legacy verify`, then a dashboard BFF smoke
  (season list and one box score per era) against the copy. Weekly snapshots keep no plaintext
  (it is deleted after encryption), so the encrypted path is the user's check.
- Both are logged in `docs/sleeper/drills.md` (date, archive, result).

### 7. Docs and release

- `RUNBOOK.md`: one "Sleeper era operations" page (the weekly job, go-live, rollover, season
  close, failures).
- `08_OPERATIONS.md`: Scheduling rewritten (Task Scheduler; cron is NFL.com history).
- `03_DATA_SOURCES.md`: Sleeper is the league platform, NFL.com is defunct.
- `09_ROADMAP.md`: Sleeper-era rows.
- `10_OPEN_QUESTIONS.md`: close the S-program questions.
- After the S9 merge, promote `dev → main` and tag (S9-D8).

## Tests

- `update`: the full sequence on the mixed-era fixture; gated off without `SLEEPER_AUTO_LOAD`;
  a scoring-settings change skips the rescore with the message; failure stops with exit ≠ 0.
- Rollover discovery on a fixture chain (renewed / not renewed / wrong `previous_league_id`).
- `seal-season`: each refusal; success on a completed synthetic season. Then: triggers refuse an
  update of a guarded Sleeper column and a bracket row; `verify` passes; an un-sealed Sleeper
  season stays writable; seal-v1 fingerprints unchanged; manifest has no names.
- Migration round trip on a sealed copy; NFL.com seasons are unaffected.

## Done when

- [ ] The weekly job runs unattended with `success` for one real Wednesday (capture + verify; the
  load part once the go-live sets `SLEEPER_AUTO_LOAD`).
- [ ] Rollover dry run against a fixture chain passes.
- [ ] Restore drill executed and logged (the user's `restore_check.sh` + the local drill).
- [ ] `seal-season` built and fixture-tested (real run at season close).
- [ ] Docs updated; PR → `dev`; `dev → main` promoted and tagged.

## Decisions (recommended defaults — need the user's approval)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| S9-D1 | Schedule | **Keep one weekly job** (Wed 06:00, after Monday night and Tuesday's corrections). Corrections made later land the next Wednesday, since every capture re-fetches all weeks. Full `legacy verify` weekly. No daily or Tue/Thu jobs. | Add a Friday correction pass (a second task) so corrections land within the week. |
| S9-D2 | Unattended load + scoring | `sleeper-league update` runs in the weekly job **only after the go-live sets `SLEEPER_AUTO_LOAD=1`**. Unambiguous rookie id adoption runs automatically (the rest is reported). Rules are never rewritten unattended. | Load by hand each week (a command per week; the dashboard lags until someone runs it). |
| S9-D3 | Failure handling | Stop at the first failing step, exit ≠ 0, log + `pipeline_runs`; no notification beyond Task Scheduler's "Last Result" and the dashboard's data-as-of. | Add a Windows toast or email on failure. |
| S9-D4 | Rollover | Auto-discover and **archive** the renewed league (no config change needed to keep capturing); loading the new season waits for the user (new owners, rules sign-off). | Report only; the user updates `SLEEPER_LEAGUE_ID` by hand. |
| S9-D5 | Season close | `legacy seal-season` with the preconditions in §4, incl. the user typing the champion; seal-vN with the raw archive; all seals pinned forever; evergreen untouched. Built now, run with the user at season close. | Seal by hand with `amend` + scripts at season close (no command). |
| S9-D6 | What a sealed Sleeper season protects | Extend the guard (additive migration) to the Sleeper-era columns of the guarded tables and `playoff_brackets`; re-derivable tables stay out. | Guard only the seal-v1 column set (Sleeper extras protected by the vault copy and `verify` only). |
| S9-D7 | Restore drill | You run `restore_check.sh` (key + passphrase) on the newest archive; Claude runs a key-free local drill from the plaintext seal directory (restore → migrate → verify → dashboard smoke). Both logged. Quarterly + before release. | Only the user's `restore_check.sh`. |
| S9-D8 | Release | After S9 merges, promote danger-zone `dev → main` and tag **`v2.0.0`** (the Sleeper-era pipeline; legacy stays read-compatible). The dashboard's promotion comes with its D8. | Tag `v1.7.0` (minor). |
