# S2 — Sleeper league capture (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S2. Design sources: `01_PIPELINE_ARCHITECTURE.md`
§4 (raw archive) and §12 (settings), `04_SLEEPER_FIELD_MAPPING.md` (endpoints and ⚑ items),
`01` §14 (fixture scenarios), `00` D-1 / D-10 / D-11.

**Status:** PLAN approved 2026-11-09 (all decisions S2-D1..D10 accepted as recommended). BUILD in progress.

## Goal

Every Sleeper league response is archived, byte for byte, before anything parses it. The archive
lets us re-normalize a season offline, and the S2 probe checks the field mapping against real
2026 payloads. S2 writes **no** league tables (`seasons`, `teams`, `matchups`, …). That is S3–S5.

## Starting state (verified 2026-11-09)

- `dev` @ 691dd99 includes S1 (legacy guard live on `data/fantasy.db`, `legacy verify` passes,
  41/41 triggers) and the evergreen check (907d6ea).
- Existing `crawlers/sleeper/` = public projections, trending and players directory (tenacity
  retry, min-interval throttle, `SleeperSource` protocol and fixture source). It does **not**
  honor `Retry-After` and has no league endpoints.
- `Settings.nfl_cookie: SecretStr` is required, and the default `ff-pipeline run` sources still
  include the dead `nfl_com`.
- `data/*` is gitignored, so `data/raw/sleeper/` cannot be committed by accident.
- League `1393640392104099840` (D-1): 2026 season, `in_season`, 12 rosters,
  `previous_league_id = 0`.

## Design

### 1. Package layout

```
src/ff_pipeline/
├── archive/
│   ├── __init__.py
│   └── raw_store.py              # content-addressed gzip store + source_payloads index
└── crawlers/sleeper_league/
    ├── __init__.py
    ├── client.py                 # SleeperLeagueSource protocol + Live / Fixture / Archive sources
    ├── endpoints.py              # Endpoint enum, URL builders, request plan per season state
    ├── lineage.py                # previous_league_id walk (cycle- and depth-guarded)
    └── capture.py                # orchestrates one capture run → raw_store; returns a summary
```

`crawlers/sleeper/` (projections) stays as it is. The shared parts (throttle and tenacity retry)
go into the new client, and the new client also honors `429 Retry-After` (capped at 120 s, then
counted as a retry). See decision S2-D1.

### 2. Source seam

```python
class SleeperLeagueSource(Protocol):
    def get(self, req: PayloadRequest) -> FetchedPayload: ...
# PayloadRequest(endpoint, league_id, season_year, week | None, url)
# FetchedPayload(request, body: bytes, http_status, fetched_at)
```

- `LiveSleeperLeagueSource`: httpx, throttle (`SLEEPER_REQUESTS_PER_MIN`), tenacity on 5xx and
  network errors, `Retry-After` on 429.
- `FixtureSleeperLeagueSource`: reads `tests/fixtures/sleeper_league/<scenario>/<endpoint>[_<week>].json`.
- `ArchiveSleeperLeagueSource(as_of=None)`: newest `source_payloads` row per `request_url` (or the
  newest at or before `as_of`), with bytes read from the raw store. No network.

Capture always stores the **raw bytes**, never re-serialized JSON, so the sha256 is the sha256
of exactly what Sleeper sent.

### 3. Endpoints captured per run

| Endpoint key | URL | When |
|---|---|---|
| `state` | `/v1/state/nfl` | every run; sets the current `week` / `leg` |
| `league` | `/v1/league/{id}` | every run |
| `users` | `/v1/league/{id}/users` | every run |
| `rosters` | `/v1/league/{id}/rosters` | every run |
| `matchups` | `/v1/league/{id}/matchups/{w}` | w = 1..current (all weeks re-fetched, so stat corrections land as new revisions) |
| `transactions` | `/v1/league/{id}/transactions/{r}` | r = 1..current leg |
| `traded_picks` | `/v1/league/{id}/traded_picks` | every run |
| `winners_bracket` / `losers_bracket` | `/v1/league/{id}/…_bracket` | every run (empty/404 before playoffs is stored with its status) |
| `drafts` / `draft` / `draft_picks` | `/v1/league/{id}/drafts`, `/v1/draft/{did}`, `/v1/draft/{did}/picks` | every run (cheap; the picks are final after the draft) |
| `players` | `/v1/players/nfl` (~5 MB) | at most every `SLEEPER_PLAYERS_REFRESH_HOURS` (24) |
| `stats_week` | `api.sleeper.com/stats/nfl/{yr}/{w}?season_type=regular` | w = 1..current (decision S2-D4) |

That comes to about 45 requests per in-season run, well under the 1000/min ceiling.

### 4. Raw store and `source_payloads` (one additive migration, `s2_source_payloads`)

Bytes: `data/raw/sleeper/<sha[:2]>/<sha>.json.gz` (`SLEEPER_RAW_DIR`). The write is atomic
(tmp + rename), and nothing is written when the file already exists.

```
source_payloads
  payload_id        INTEGER PK
  source            TEXT NOT NULL            -- 'sleeper_league'
  endpoint          TEXT NOT NULL            -- key from §3
  platform_league_id TEXT                    -- NULL for state/players/stats_week
  season_year       INTEGER
  week              INTEGER                  -- matchups/transactions/stats_week only
  request_url       TEXT NOT NULL
  sha256            TEXT NOT NULL
  byte_size         INTEGER NOT NULL
  http_status       INTEGER NOT NULL
  first_fetched_at  TEXT NOT NULL            -- decision S2-D3
  last_fetched_at   TEXT NOT NULL
  fetch_count       INTEGER NOT NULL DEFAULT 1
  run_id            TEXT NOT NULL            -- first run that saw this revision
  UNIQUE (sha256, request_url)
  INDEX (request_url, last_fetched_at)
```

When a re-fetch returns identical bytes, the existing row's `last_fetched_at` and `fetch_count`
are bumped. No new file and no new row are created. When the bytes change, a new row is added,
and that row is the next revision. The writes go through `ON CONFLICT … DO UPDATE` (the repo's
idempotent-write rule).

The table is **not** legacy-guarded (it holds no sealed-season rows). The migration follows
`03` §9: `legacy verify` on a DB copy before and after, plus upgrade → downgrade → upgrade. It is
applied to the live DB **only with the user's approval** in the BUILD session.

### 5. Settings and CLI

- New: `SLEEPER_LEAGUE_ID` (optional, needed by league commands only), `SLEEPER_RAW_DIR`
  (`./data/raw/sleeper`), `SLEEPER_PLAYERS_REFRESH_HOURS` (24). `.env.example` updated.
- `NFL_COOKIE` becomes `SecretStr | None`. Only NFL.com commands require it, and they fail with a
  clear message when it is missing. (Decision S2-D5 covers retiring `nfl_com` from the default
  `run`.)
- `ff-pipeline sleeper-league capture [--season 2026] [--league-id …] [--dry-run]` resolves the
  league for the season via lineage, runs the request plan and prints a summary
  (`requests / new revisions / unchanged / errors`). This is a writing command, so the
  `legacy verify --quick` preflight runs first.
- `ff-pipeline sleeper-league lineage [--league-id …]` prints the season → league_id chain (read-only,
  network only).
- `ff-pipeline sleeper-league archive ls [--endpoint … --week …]` lists revisions (read-only;
  useful for the probe).

### 6. Probe (task 4) — walk every ⚑ in `04`

Run it against the captured 2026 payloads, through the archive source, so it is offline and
repeatable. Each ⚑ ends in one of three states: **resolved** (doc updated, with the evidence
quoted as keys and values only, no names), **deferred to S#** (with the reason), or **cannot
be known yet** (for example, brackets before week 15 → deferred to a re-probe once the playoffs
start).

| ⚑ | Evidence source |
|---|---|
| `start_week` / `playoff_week_start` / playoff rounds | `league.settings` |
| `waiver_type = 2` ⇒ FAAB; FAAB type (int vs float) | `league.settings`, `transactions[].settings.waiver_bid` |
| division metadata keys | `league.metadata` |
| `is_owner` = commissioner | `users` |
| `roster_id` stability across renewals | **deferred**: no renewal exists until 2027 |
| `metadata.streak` / `record` | `rosters[].metadata` |
| slot labels (`roster_positions`, `FLEX`, `SUPER_FLEX`, `DEF`, `BN`, `IR`) | `league.roster_positions`, `matchups[].starters` |
| bonus stacking (300/400 pass yd; 40+/50+ TD) | `matchups[].players_points` vs `stats_week` stats × `scoring_settings` |
| FG 50+ split (`fgm_50_59`/`fgm_60p` vs `fgm_50p`) | `scoring_settings` keys and kicker `players_points` |
| `st_td` / `kr_td` / `pr_td` keys in use | `scoring_settings` |
| points-allowed definition (defense-only vs total) | DEF `players_points` vs `stats_week` `pts_allow` |
| bracket `p` semantics, consolation, toilet-bowl direction | `winners_bracket` / `losers_bracket`. **Deferred** to a re-probe after the playoff start (week ≥ `playoff_week_start`) |

**D-10 evidence.** The probe *records* what Sleeper actually does (stacking, FG 50+ split,
points allowed) in `04` §5 under "Evidence for D-10 (S6)". D-10 itself is **not** decided in
S2.

The probe is a small script, `scripts/sleeper/probe_field_mapping.py`, that reads the archive and
prints a markdown evidence table. It writes nothing to the DB, and its output is pasted into
`04` by hand.

### 7. Fixtures (task 5, D-11)

`scripts/sleeper/build_fixtures.py` reads the archive, trims the payloads to a few rosters and
players per scenario, and pseudonymizes them:

- `user_id` → `u0001…`; `display_name` / `username` → `Manager 01…`; team names
  (`metadata.team_name`) → `Team 01…`; avatars → removed; league name → `Fixture League`;
  `league_id` / `draft_id` / `transaction_id` → stable fake IDs; free-text fields
  (`metadata.*` notes, messages) → dropped.
- NFL `player_id`s are kept, since they are public NFL identities and the normalizer needs them.
- The mapping is deterministic (same input, same pseudonym) and is held in memory only. It is
  never written to disk.

Scenarios from `01` §14: regular week, bye, trade with picks + FAAB, failed waiver,
commissioner `custom_points`, co-owner, blank team name, DEF slot, and (later) playoff week. A
scenario the real league hasn't produced yet (co-owner, `custom_points`, playoff) is built
**synthetically** from a real payload and marked `"_synthetic": true` in a `README.md`.

The 3-season lineage fixture is synthetic, because the real chain has one season.

A **privacy test** fails when any fixture string matches a denylist pattern (the real league ID,
a 15+ digit Sleeper ID outside the fake range, or any non-pseudonym `display_name`). Before the
commit, the user also reviews the fixture diff.

## Tests

- `tests/unit/test_raw_store.py`: same bytes → one file, one row, `fetch_count = 2`; the same
  bytes from two URLs → one file, two rows; changed bytes → a new row; atomic write (no partial
  file after a simulated failure); gzip round-trip is byte-exact.
- `tests/unit/test_sleeper_league_client.py`: the fixture source maps every endpoint; 429 with
  `Retry-After` waits and retries (fake clock); 5xx retries then raises; 404 is stored with its
  status, not raised (brackets before playoffs).
- `tests/unit/test_sleeper_league_endpoints.py`: request plan per `state` (preseason, week 10,
  playoffs); players skipped inside the refresh window.
- `tests/unit/test_sleeper_lineage.py`: 3-season chain; `previous_league_id = 0` stops the walk;
  a cycle and the depth cap are detected.
- `tests/integration/test_sleeper_capture.py`: fixture capture into a temp DB plus raw dir →
  expected rows; a second run adds 0 files and 0 rows; the archive replay returns the newest
  revision (and the `as_of` revision); capture on a DB holding a sealed fixture season leaves the
  sealed fingerprints unchanged.
- `tests/integration/test_migration_s2.py`: upgrade → downgrade → upgrade on a sealed fixture DB;
  `legacy verify` passes throughout.
- `tests/unit/test_settings.py`: missing `NFL_COOKIE` is accepted; NFL.com commands error
  clearly without it.
- `tests/unit/test_fixture_privacy.py`: the denylist scan over `tests/fixtures/sleeper_league/`.

## Rollout on the live DB (BUILD session, with the user)

1. Copy `data/fantasy.db` → scratch; `legacy verify`; migrate the copy; `legacy verify`; run
   capture against the copy.
2. **Ask.** Then migrate the live DB, run `legacy verify`, and run
   `sleeper-league capture --season 2026`. Re-run it and confirm 0 new files and 0 new rows
   (unless Sleeper data changed in between).
3. Run the probe, update `04`, build the fixtures, and have the user review the fixture diff.

## Done when

- [x] `ff-pipeline sleeper-league capture --season 2026` archives every §3 endpoint on the live DB.
- [x] Re-running adds no duplicate files (and no rows for unchanged payloads).
- [x] Every ⚑ in `04` is resolved or explicitly deferred (with its S# and reason); D-10 evidence
      is recorded, not decided.
- [x] The pseudonymized fixtures are committed and the privacy test passes. Review basis
      (2026-11-09): 41 files are too many to check by hand; the user accepted them at a glance
      as a scaffold. The guarantee is automated: the builder refuses to write if any real user
      id, display name, team name, league/draft/transaction id leaks, and
      `tests/unit/test_fixture_privacy.py` blocks non-fake 15+ digit ids, the real league id,
      emails, URLs, avatars, chat/import/nickname keys and non-pseudonym names.
- [x] `legacy verify` passes before and after the migration (copy, then live).
- [x] Full gate green on the files S2 touches (see "Known issues" for pre-existing format debt).
- [x] RUNBOOK gains a "Sleeper league capture" entry. [ ] `02` §S2 marked done; PR → `dev`.

## As built (2026-11-09)

Code: `archive/raw_store.py`; `crawlers/sleeper_league/{endpoints,client,lineage,capture,cli}.py`;
`SourcePayload` + migration `a7c9e1b3d5f2_s2_source_payloads`; settings (`SLEEPER_LEAGUE_ID`,
`SLEEPER_RAW_DIR`, `SLEEPER_PLAYERS_REFRESH_HOURS`, optional `NFL_COOKIE` +
`require_nfl_cookie()`); `run` default = `nflverse, sleeper, team_defense`. Scripts:
`scripts/sleeper/probe_field_mapping.py`, `scripts/sleeper/build_fixtures.py`.

### Deviations from the design above

1. **`archive` is one flat command** (`sleeper-league archive --endpoint … --week …`), not
   `archive ls`.
2. **Fixture file names derive from the URL** (`PayloadRequest.fixture_name`, e.g.
   `v1_league_<id>_matchups_3.json`), so one fixture dir can hold several leagues (lineage).
3. **4xx responses are archived with their status, not raised.** 5xx / network errors / 429
   retry (4 attempts, exponential 2–16 s; 429 waits `Retry-After`, max 120 s). A request that
   still fails is reported and the run continues; exit code 1 at the end.
4. **Drafts are fetched every run** (3 cheap requests; identical bytes dedupe).
5. **No synthetic playoff week** (D-S2-7 said synthetic). Bracket and playoff-week semantics are
   exactly what is unverified until games are decided, so a made-up week 15 would bake guesses
   into tests. The real week is captured after week 15 instead (D-S2-8 re-probe).
6. **`tests/integration/test_legacy_guard.py`** downgrade test now targets S1's parent revision
   (`d4e6f8a0b2c4`) instead of `-1`, which would have downgraded S2 once S2 became head.
7. **`stats_week` churn (found by the live capture).** Sleeper embeds live player metadata
   (`injury_status`, `news_updated`) in the stats payload and its caches flip those values, so a
   re-capture can add stats revisions with no stat change. Kept as is (raw bytes are the record;
   ~100 KB gzip each); documented in `04` §10 and the RUNBOOK.

### Live rollout — 2026-11-09 (approved by the user)

- Copy (`sqlite3 .backup` of the live DB): `legacy verify` PASSED → `migrate up` (e2a4c6e8f0b1 →
  a7c9e1b3d5f2) → PASSED; capture: 38 requests, 38 new revisions, 0 errors (weeks 1–9, 4.2 MB);
  re-run: 37 requests, 0 new revisions, 0 new files; `legacy verify` PASSED.
- Live: `SLEEPER_LEAGUE_ID` added to `.env`; verify PASSED → migrate → PASSED; capture 38/38 new;
  re-run 34 unchanged + 3 new `stats_week` revisions (deviation 7); verify PASSED; 41/41 guard
  triggers; sealed 2010–2025 unchanged.

### Probe results (`04` §10)

All ⚑ resolved or deferred. Headline: `players_points` = Σ stats × `scoring_settings` for all
1,749 rostered player-weeks; yardage bonuses tiered, long-TD bonuses stacked, 50+ FG = 5,
`pts_allow` excludes return TDs / safeties scored against the offense (0 unexplained of 269).

### Follow-ups (not S2)

- Re-probe brackets after week 15 (D-S2-8) and capture a real playoff week fixture.
- S5: label past weeks' IR players (`matchups.players[]` includes them; `rosters.reserve` is
  current state only).
- 2027 rollover: `roster_id` stability; forward discovery (`/user/{id}/leagues`) not built —
  set `SLEEPER_LEAGUE_ID` to the new league.
- D-S2-9: the `dz-sleeper-capture` scheduled task, set up with the user after merge.
- Per-request `GET` debug lines print even with `LOG_LEVEL=WARNING` (console logging config,
  pre-existing; also affects `crawlers.sleeper`).

## Known issues (out of scope unless the user says otherwise)

- `prune-players` fails with a FOREIGN KEY error on 5 players (pre-existing).
- `ruff format --check` is red on `dev` for 4 files (`crawlers/adp/sleeper.py`,
  `alembic/versions/d4e6f8a0b2c4_add_player_adp.py`, `scripts/backfill_faab_bids.py`,
  `scripts/backfill_fumbles_lost.py`). S2 keeps its own files format-clean.
- Vault spec §6 says `ff-pipeline status` should warn on a stale vault copy, but that warning
  is not built. It is an S9 item (it could also surface `evergreen.ok`).

## Decisions (defaults approved by the user 2026-11-09)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| S2-D1 | Where the league client lives | New `crawlers/sleeper_league/` package; leave `crawlers/sleeper/` (projections) untouched. | Merge it into `crawlers/sleeper/` (one package, but it entangles the projections code with the S2 diff). |
| S2-D2 | Raw bytes location | `data/raw/sleeper/` (gitignored); the S9 rolling seal carries completed seasons to the vault. | Write straight into `~/ff-legacy-vault/` (couples capture to the vault; rejected). |
| S2-D3 | Identical re-fetch | Bump `last_fetched_at` / `fetch_count` on the existing row; no new row. This keeps a "still unchanged as of X" record without growth. | Strict `INSERT … DO NOTHING` (no "last seen" information). |
| S2-D4 | Also capture Sleeper weekly **stats** (`stats_week`) | Yes. It is the like-for-like comparator for D-10 (stacking, points allowed) without nflverse-vs-Sleeper stat drift, and it is cheap. | Skip it, and use nflverse stats for the probe (it mixes stat-source differences into the scoring evidence). |
| S2-D5 | Retire `nfl_com` from the default `ff-pipeline run` | Yes, in S2. The default becomes `nflverse, sleeper, team_defense`, and `--source nfl_com` still works when a cookie is set. | Leave the default as it is and only make the cookie optional. |
| S2-D6 | Fixture pseudonymization | Deterministic in-memory mapping, the denylist privacy test, and **user review of the fixture diff** before the commit. | Hand-written synthetic fixtures only (safer, but loses real-payload shape). |
| S2-D7 | Scenarios the league hasn't produced (co-owner, `custom_points`, playoff) | Build them synthetically from real payloads and label them `_synthetic`; re-capture the real playoff week after the playoff start. | Wait for real occurrences (blocks the S2 Done when). |
| S2-D8 | Bracket ⚑ items | Defer to a re-probe once week ≥ `playoff_week_start`, recorded as a dated follow-up in `04`. S2 can close before then. | Hold S2 open until the playoffs. |
| S2-D9 | Scheduled weekly capture | After S2 merges, add a **separate** Task Scheduler job `dz-sleeper-capture` (Wed 06:00, after stat corrections settle), set up with the user. It is kept apart from `dz-vault-sync` so a Sleeper outage can't fail the vault job. | Append it to `dz-vault-sync`. |
| S2-D10 | Live-DB rollout | Copy first, then ask, then live (the S1 pattern). | Capture into a separate DB until S3 (adds a second DB to manage). |
