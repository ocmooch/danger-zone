# 02 — ff-pipeline Build Plan: Sleeper Era

**Status:** planned, not started · **Repo:** `danger-zone` · **Branch per milestone:** `feature/sleeper-s<N>-<name>` → PR to `dev`
**Architecture:** `01_PIPELINE_ARCHITECTURE.md` · **Preservation:** `03_LEGACY_PRESERVATION.md` · **Mapping:** `04_SLEEPER_FIELD_MAPPING.md`

Each milestone is one PR, follows the plan → build → verify session split, and ends with the full
gate (`pytest`, `ruff check`, `ruff format --check`, `mypy src`). Every milestone that writes to the
DB starts with `legacy verify` and ends with it.

---

## Milestone summary

| # | Milestone | Est. | Gate it opens | Depends on |
|---|-----------|------|---------------|------------|
| S0 | Legacy Seal v1 + off-machine copies | 2–3 hr | **G0** — everything | D-7 |
| S1 | Legacy guard: fingerprints, `legacy` CLI, seal flag, triggers, amendments | 3–4 hr | safe schema work | S0 |
| S2 | Sleeper league client + raw archive + capture command + live probe | 3–4 hr | real fixtures; mapping confirmed | S1, D-1 |
| S3 | Additive schema migration for the platform era | 2–3 hr | **G1** — dashboard fixture | S1, S2 |
| S4 | Identity: owner crosswalk, player stubs, DEF mapping, 2026 stub quarantine | 3 hr | loader can resolve everyone | S3, D-2, D-3 |
| S5 | Canonical loader: season, teams, matchups, rosters, transactions, draft | 4–6 hr | **G2** — 2026 visible | S4 |
| S6 | Scoring: rule derivation, nflverse 2026, rescore, verify vs Sleeper | 3–4 hr | official + reconstructed points | S5, D-10 |
| S7 | Rich data: brackets, picks, failed claims, status snapshots, avatars | 4–5 hr | **G4** — new features | S5 |
| S8 | Read surface: query helpers, capability matrix, API additions, docs | 2–3 hr | **G3** — dashboard parity | S6 |
| S9 | Operations: schedule, rollover, season close, rolling seal, runbook, release | 2–3 hr | **G5** | S7, S8 |

Total ≈ 30–40 focused hours. S7 and S8 can run in parallel after S6.

---

## S0 — Legacy Seal v1 (no code in `src/`)

**Goal:** an independent, verified, off-machine copy of the NFL.com era before anything changes.

Tasks
1. Pre-flight: both repos clean; no in-flight `scripts/*` repair; note current alembic head.
2. Read-only integrity: `PRAGMA integrity_check`, `PRAGMA foreign_key_check`.
3. Write `scripts/legacy_seal_v1.py` (stdlib + `sqlite3` only; opens the live DB read-only;
   uses `sqlite3.Connection.backup` to the vault): DB copy, `schema.sql`, gzip CSV + JSONL per
   guarded table, assets, seeds, NFL.com fixtures, dictionary docs, `MANIFEST.json` (`03` §5),
   `verify.py`.
4. Run it → `~/ff-legacy-vault/seal-v1-<date>/`; `python verify.py`; `chmod -R a-w`.
5. `age`-encrypt the seal directory into `seal-v1.tar.gz.age`; upload to Google Drive, iCloud Drive
   and a release on the private `ff-legacy-vault` repo (D-7); download each once, decrypt and run
   `verify.py`.
6. Commit `docs/sleeper/seals/seal-v1.manifest.json` (counts + hashes only).
7. Move `data/fantasy.db.bak-*` into `data/backups/historical/`; teach `_prune_backups` to skip
   that directory (tiny code change + unit test).

Tests: unit test for the fingerprint serializer (determinism across runs, float/NULL/JSON handling)
— shared with S1. Prune-skip unit test.

**Done when:** the local vault plus three verified off-machine copies exist (Drive, iCloud, private
repo release); the manifest hash is in git; `ls data/` shows no loose `.bak` files.

## S1 — Legacy guard — DONE (built 2026-11-07, merged to `dev` via PR #67, 2026-11-09)

Build contract and the as-built record (deviations, command sweep): `docs/plans/S1-legacy-guard.md`.

**Goal:** sealed seasons cannot change except through an audited amendment.

Files: `repository/legacy_guard.py`, `repository/models.py` (`LegacyGuard`, `LegacyAmendment`,
`LegacySeal`; `Season.is_sealed/sealed_at/seal_id`), alembic migration (columns, tables, triggers),
`cli.py` (`legacy seal|verify|status|amend|restore-drill`), preflight hook in `run`, `backfill`,
`reconstruct`, `rescore`, `migrate up`.

Notes
- Triggers per guarded table: `BEFORE INSERT/UPDATE/DELETE` checking `seasons.is_sealed` through
  the row's season (`season_id`, `season_year`, or `team_id → teams.season_id`).
- `players` gets a column-scoped trigger: `BEFORE UPDATE OF nfl_com_player_id`.
- The migration sets `is_sealed = 1` for 2010–2025 only after confirming fingerprints match seal-v1.
- `legacy amend` implements `03` §8 including the time-boxed unlock and seal-vN+1.
- `rescore` and nflverse refreshes must still pass on sealed years (re-derivable tables unguarded).

Tests: `tests/integration/test_legacy_guard.py` — forced UPDATE/DELETE/INSERT on a sealed fixture
season raises; same on an unsealed season succeeds; amendment writes an audit row and new seal;
migration upgrade/downgrade/upgrade keeps fingerprints; `rescore` on a sealed season succeeds;
`legacy verify --quick` detects a removed trigger.

**Done when:** live DB has the guard; `legacy verify` passes; `ff-pipeline run --source nflverse`
and `rescore --season 2015` still succeed; a deliberate `UPDATE matchups … WHERE season_id=<2015>`
in the sqlite shell is refused.

## S2 — Sleeper league capture — DONE (merged to `dev` via PR #69, 2026-11-09)

As-built record, probe results and follow-ups: `docs/plans/S2-sleeper-league-capture.md`.
Weekly capture runs as Windows task `dz-sleeper-capture` (Wed 06:00). Open follow-up: re-probe
brackets once playoff games are decided (after week 17, ~2027-01-05).

**Goal:** archive every Sleeper league payload, and confirm the field mapping against reality.

Files: `crawlers/sleeper_league/{client,endpoints,lineage,capture}.py`, `archive/raw_store.py`,
`SourcePayload` model + migration, settings (`SLEEPER_LEAGUE_ID`, `SLEEPER_RAW_DIR`; `NFL_COOKIE`
made optional), `cli.py` (`sleeper-league capture|lineage`), `.env.example`.

Tasks
1. `SleeperLeagueSource` protocol + `LiveSleeperLeagueSource` (reuse throttle/retry; honor 429
   `Retry-After`), `FixtureSleeperLeagueSource`, `ArchiveSleeperLeagueSource`.
2. Raw store writes gzip JSON by sha256 + `source_payloads` row per request.
3. Capture the real 2026 league (all weeks to date).
4. **Probe:** walk every ⚑ in `04_SLEEPER_FIELD_MAPPING.md` against the captured payloads; update
   the doc (bracket `p` semantics, bonus stacking, points-allowed, slot labels, FAAB type).
5. Build trimmed, pseudonymized fixtures (D-11) for the scenarios in `01` §14.

Tests: client against fixtures; raw store dedupe (same bytes ⇒ one file, two URLs ⇒ two rows);
archive replay returns newest payload; lineage walk on a 3-season fixture chain.

**Done when:** `ff-pipeline sleeper-league capture --season 2026` archives all endpoints, re-running
adds no duplicate files, every ⚑ in `04` is resolved or explicitly deferred, fixtures committed.
From here on a scheduled capture can run weekly even before S5.

## S3 — Platform-era schema — DONE (merged to `dev` via PR #71, 2026-11-09)

As-built record and rollout: `docs/plans/S3-platform-era-schema.md`. Live DB at `b8d0f2a4c6e3`.

**Goal:** the additive schema from `01` §5, verified harmless to the legacy era.

Files: `repository/models.py`, one alembic migration, `docs/04_DATA_MODEL.md` (new columns/tables).

Tasks: columns on `seasons`, `teams`, `matchups`, `transactions`, `players`; tables
`owner_platform_identities`, `playoff_brackets`, `traded_picks`, `player_status_snapshots`,
`legacy_nfl_com_2026_preseason`; partial unique indexes; ORM models with strict typing.

Verification: run the migration on a scratch copy of seal-v1 → `legacy verify` identical → run on
live DB → `legacy verify` identical. Record "fingerprints unchanged" in the PR body.

**Done when:** migration merged; live DB at new head; fingerprints unchanged; dashboard test suite
still green against the migrated DB (no consumer breakage from additive columns).

## S4 — Identity — DONE (built + applied to live 2026-11-09; PR → `dev`)

As-built record: `docs/plans/S4-identity.md`. No seal-v2 was needed: sealed data is unchanged, and
seal-v1 already holds the 2026 stub (decision S4-D1). The quarantine has its own audit row (#1).

**Goal:** every Sleeper user, roster and player resolves to a canonical row.

Files: `normalizer/owner_identity.py`, `normalizer/slots.py`, `data/owner_platform_identities.yaml`
(user-reviewed), `cli.py` (`owners map`), resolver policy update for rostered Sleeper-only players,
DEF mapping, quarantine amendment script for the 2026 NFL.com stub.

Tasks
1. Draft the YAML from the captured `users` payload with best-guess `owner_id`s; **user confirms**.
   (Drafted 2026-11-05 at `data/owner_platform_identities.yaml`, local and gitignored: 8 mapped,
   4 open.)
2. `owners map --platform sleeper` validates (every user mapped, no owner mapped twice per season).
3. D-2 (decided: placeholder, archive it): `legacy amend --reason "Archive 2026 NFL.com placeholder season"` copies the 12 team + 193
   roster rows verbatim into `legacy_nfl_com_2026_preseason`, then removes them from live tables;
   season 2026 row is re-labelled `platform='sleeper'`. Seal v2 is produced.
4. Player resolution dry run over all 2026 rostered/drafted/transacted Sleeper ids: report resolved
   / linked / stub-needed / DEF.

Tests: unmapped user fails loudly; co-owner mapping; DEF code folding (`LAR`→`LA`); stub creation
only for league-referenced players; quarantine amendment preserves rows byte-for-byte.

**Done when:** YAML approved; dry run shows 0 unresolved users and an explained list of stubs;
seal-v2 exists in all vault locations.

## S5 — Canonical loader — DONE (merged via PR #74, 2026-11-09; verified on a DB copy)

As-built record: `docs/plans/S5-canonical-loader.md`. All 12 team records equal Sleeper's; three box
scores confirmed against the Sleeper app. **The live DB is not loaded yet** (decision S5-D1): run
`ff-pipeline sleeper-league load` on live once the dashboard is ready to show 2026 (D0–D3).

**Goal:** the 2026 season in canonical tables, idempotently.

Files: `canonical/{dto,loader}.py`, `normalizer/sleeper_league.py`, `crawlers/sleeper_league/runner.py`,
`cli.py` (`sleeper-league load`, `run --source sleeper-league`).

Order inside a run: season → teams → draft → matchups + roster snapshots → transactions → records
derivation → `recompute_rostered_spans` → owners active/joined/left recompute.

Tests: per-entity normalizer unit tests from fixtures; loader twice ⇒ identical DB; one-owner-per-
player-per-week invariant; byes; commissioner `custom_points`; trade legs + counterpart stitching;
failed waiver rows; sealed-season write attempt raises; full load from archive == load from fixture.

**Done when:** `sleeper-league load --season 2026` loads weeks 1..current; team W-L-T/PF/PA match
Sleeper's roster settings; spot-check three box scores against the Sleeper app; `legacy verify` passes.

## S6 — Scoring — **DONE** (2026-11-09; plan `docs/plans/S6-scoring.md`)

**Goal:** 2026 has rules, reconstructed points, official points and a reconcile report.

Files: `scoring/sleeper_rules.py`, `cli.py` (`sleeper-league rules`), verify/reconcile adapters
reading `platform_points`, optional Sleeper stats as non-primary `player_stats_raw`.

Tasks: derive rules → diff vs 2025 → **user sign-off (D-10)** → write; `run --source nflverse` for
2026; `rescore --season 2026`; `verify --sweep --season 2026 --reconcile`; classify divergences.

Tests: mapping per Sleeper key incl. bracket keys; unknown non-zero key aborts; engine parity on a
fixture (Sleeper `players_points` == engine output for a clean player-week); reconcile classifier.

**Done when:** rules written and approved; 2026 scored; reconcile report committed under
`docs/sleeper/reports/`; residual divergences explained.

## S7 — Rich Sleeper data

**Goal:** the new facts the dashboard will surface.

Scope: `playoff_brackets` + derived `is_playoff/is_consolation`, champion/runner-up/Sacko,
`playoff_finish`; `traded_picks`; trade `picks[]`/`faab_transfers[]`; failed waiver claims;
`teams.division`, `teams.extra_data` (ppts, streak, moves); `player_status_snapshots` (daily from
the directory); Sleeper avatars into `assets`; `seasons.settings_snapshot`.

Tests: bracket derivation on a fixture with 3rd-place and toilet-bowl games; pick ownership over two
trades; status snapshot idempotency per day; avatar dedupe by sha256.

**Done when:** each facet populated for 2026 to date (brackets once playoffs begin — fixture-tested
before then); capability presence checks return `available` where data exists.

## S8 — Read surface

**Goal:** the dashboard can consume everything through supported helpers.

Files: `repository/queries.py` (`official_slot_points_expr`, `authoritative_week_points_expr`,
`season_platform`, `season_capabilities`, `brackets_for_season`, `traded_picks_for_league`,
`player_status_asof`, `failed_waiver_claims`), Phase 1 API (`/seasons` includes `platform`,
`capabilities`), `docs/06_API_CONTRACT.md`.

Tests: helper unit tests across a mixed-era fixture (NFL.com season + Sleeper season); API
contract tests; `authoritative_week_points_expr` returns identical values to the dashboard's
current expression for every legacy fixture row.

**Done when:** helpers merged; dashboard D1/D2 can start; legacy outputs of the moved expression
proven identical.

## S9 — Operations and release

**Goal:** the Sleeper era runs itself, and every finished season becomes protected history.

Tasks: scheduled jobs (daily capture+load in season; Tue/Thu correction passes; daily players
directory; weekly `legacy verify`); rollover check (`lineage` detects the renewed league);
season-close procedure (final pass → reconcile → user confirms champion → `legacy seal --season`);
quarterly restore drill; `docs/RUNBOOK.md`, `08_OPERATIONS.md`, `03_DATA_SOURCES.md` (Sleeper is
now the league platform), `09_ROADMAP.md` (Sleeper-era rows), `10_OPEN_QUESTIONS.md`; tag release.

**Done when:** a full scheduled week runs unattended with `success`; rollover dry-run against a
fixture chain passes; restore drill executed and logged; `dev → main` promotion tagged.

---

## Cross-cutting checklists

**Every PR in this program**
- [ ] `legacy verify` passes before and after on the live DB (paste the summary line in the PR)
- [ ] migrations are additive only; run against a seal copy first
- [ ] new parsing has fixtures; new scoring has unit tests
- [ ] settings read via `get_settings()`; `mypy --strict` clean; `uv.lock` untouched unless scoped
- [ ] docs updated where behavior/schema/ops changed
- [ ] commit trailers per `CONTRIBUTING.md`

**Things that must never happen**
- Any write to a sealed season outside `legacy amend`
- Deleting a vault copy, a `fantasy-pre-*` backup, or anything under `data/backups/historical/`
- Overwriting `owners.display_name` or legacy `team_name`s with Sleeper values
- Committing raw league payloads, `.env`, or unpseudonymized fixtures
