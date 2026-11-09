# S4 — Identity (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S4. Design sources: `01_PIPELINE_ARCHITECTURE.md`
§7 (identity), `04_SLEEPER_FIELD_MAPPING.md` §3–§4.1 (users/rosters, slot taxonomy D-9), `03` §8
(amendments), decisions D-2 / D-3 / D-9 (`00`), S1 decision 4 (stub moved in S4, audited).

**Status:** PLAN, written 2026-11-09 on `feature/s4-identity`. Nothing is built yet. The
decisions at the end need the user's approval before the BUILD session.

## Goal

Every 2026 Sleeper user, roster and player resolves to a canonical row, and season 2026 belongs to
Sleeper: the NFL.com placeholder is moved out, audited, and stays preserved.

## Starting state (verified 2026-11-09, live DB at S3 head `b8d0f2a4c6e3`)

- **Owner crosswalk** `data/owner_platform_identities.yaml` (local, gitignored, D-3): 12 entries,
  `{sleeper_user_id, sleeper_display_name, sleeper_team_name, roster_id, owner_id, confidence}`.
  Confidence: 6 `high`, 4 `confirmed`, 2 `medium`. Cross-checks: all 12 `owner_id`s exist and are
  distinct; they equal **exactly** the owners of the 12 stub-2026 teams and of the 12 2025 teams;
  12/12 `roster_id` ↔ `sleeper_user_id` pairs match the archived `rosters` payload; every archived
  Sleeper user is in the YAML.
- **2026 NFL.com stub (D-2):** `season_id` 17, `in_progress`, unsealed, `platform = 'nfl_com'`;
  12 `teams` + 193 week-1 `team_rosters` rows. Nothing else references those teams (no matchups,
  transactions, bracket or avatar references). **193 players have `last_rostered_season = 2026`
  only because of the stub**, so the spans must be recomputed after the move
  (`repository.maintenance.recompute_rostered_spans`).
- **`legacy amend` does not fit the stub move as-is.** It records an amendment, and produces
  seal-vN+1, only when **sealed** rows change. 2026 is unsealed, so an amend-wrapped move would
  leave no audit row and no seal-v2. `legacy_amendments` is append-only (UPDATE/DELETE triggers;
  INSERT allowed).
- **Players (2026 archive, weeks 1–9):** 289 league-referenced Sleeper ids (rostered, IR,
  matchups, transactions, draft). 28 DEF codes; DEF players already carry Sleeper codes as
  `sleeper_id` (`LAR`, `LAC`, `WAS` …). 257 of the 261 numeric ids resolve by `players.sleeper_id`.
  **4 are unresolved** (2 WR, 1 RB, 1 DB; none has a `gsis_id` in Sleeper's directory).
- `normalizer/player_ids.PlayerResolver` exists (gsis / fuzzy rules). There is no
  `normalizer/slots.py` and no owner-identity module yet.

## Design

### 1. Owner identity — `normalizer/owner_identity.py` + `ff-pipeline owners map`

- `load_crosswalk(path) -> list[CrosswalkEntry]` (strict schema; unknown keys fail).
- `validate(entries, users_payload, rosters_payload, session) -> list[Problem]`:
  - every archived Sleeper user is mapped (else **fail loudly** and print the YAML stanza to add);
  - every `owner_id` exists, unless the entry says `new_owner: true`;
  - no owner is mapped twice in one platform season, and no Sleeper user maps to two owners;
  - `roster_id` matches the archived roster's `owner_id`; co-owners (`co_owners[]`) must be mapped
    too;
  - `confidence` must be `high` or `confirmed`; `medium` / `low` fail (decision S4-D3).
- `owners map --platform sleeper [--season 2026]`: validate only (default, read-only). With
  `--apply`, upsert `owner_platform_identities` (`platform='sleeper'`, `external_user_id`,
  `external_display_name`, `first_season`/`last_season`) in one transaction, idempotent on
  UNIQUE(`platform`, `external_user_id`). A writing command, so the legacy preflight runs first.
- The YAML stays local and gitignored (it holds real names); tests use pseudonymous YAML.

### 2. 2026 stub quarantine — `ff-pipeline legacy quarantine-2026-stub`

One command, one transaction, audited (decision S4-D1):

1. Preflight: `legacy verify` clean; season 2026 exists, is unsealed and `platform='nfl_com'`; the
   quarantine table is empty (re-running after success is a no-op that says so).
2. Copy every `teams` row of season 2026 and every `team_rosters` row of those teams into
   `legacy_nfl_com_2026_preseason` (`source_table`, `original_pk`, `row_data` = all columns,
   verbatim).
3. Re-read each copied row and compare it with the live row, column by column. Any mismatch
   aborts and rolls back.
4. Delete those `team_rosters`, then those `teams`.
5. `seasons` 2026: `platform = 'sleeper'`, `platform_league_id = <2026 Sleeper league id from
   the archive>`. `season_id`, `year` and `status` are kept.
6. Insert a `legacy_amendments` row (`reason`, the command, `git_commit`, `before_seal =
   after_seal = <current seal>`, `tables_changed = {"teams": [2026], "team_rosters": [2026]}`,
   `rows_changed` = moved PKs + counts), and set `amendment_id` on every quarantine row.
7. `recompute_rostered_spans()`.
8. Postflight: `legacy verify` clean and fingerprints unchanged (sealed data was never touched).

**No seal-v2.** The sealed scope does not change, so the baseline stays `seal-v1`. The stub stays
preserved twice: in the seal-v1 vault archive (whose manifest hashed every row, including 2026,
and which also sits in the evergreen copies) and in the quarantine table. This replaces the old
Done-when item "seal-v2 exists in all vault locations" (decision S4-D1). A real seal-v2 waits for
the first sealed Sleeper season (`03` §10, S9).

### 3. Player resolution — `normalizer/sleeper_players.py` + `sleeper-league resolve-players`

`resolve(ids, directory, session) -> Resolution` in this order:

1. `players.sleeper_id` (following `player_identity_links` to the canonical player);
2. DEF code: `sleeper_id` on a DEF row, else `canonical_franchise(code)` → that franchise's DEF row
   (`LAR` → `LA`, `OAK`→`LV` …), stamping `sleeper_id` when applied;
3. the directory's `gsis_id` → `players.gsis_id`;
4. `PlayerResolver` fuzzy name + position (existing tested rules);
5. otherwise **stub-needed**, allowed only for league-referenced ids (rostered, drafted,
   transacted).

`sleeper-league resolve-players --season 2026` reads the archive and prints counts by bucket and
the stub-needed list (name, position, NFL team, Sleeper id). It is a dry run in S4. Stubs are
written by the S5 loader through the same function (decision S4-D4).

### 4. Slot taxonomy — `normalizer/slots.py` (D-9)

`map_slot(label) -> (roster_slot, is_starter)` per `04` §4.1: `QB RB WR TE K DEF` as is; `FLEX`
→ `R/W/T`; `WRRB_FLEX` → `W/R`; `REC_FLEX` → `W/T`; `SUPER_FLEX` → `Q/R/W/T`; `IDP_FLEX`/`DL`/`LB`/
`DB` as is; `BN` → bench; reserve → `RES`; taxi → `TAXI`. Unknown labels raise. The raw label
always goes to `extra_data.platform_slot` (applied by S5). Also
`starter_slots(roster_positions)` → the ordered non-bench slots that `starters[]` aligns to.

### 5. Docs

`02` §S3 marked done (carried on this branch); `02` §S4 marked done at the end. RUNBOOK: `owners
map`, the quarantine command, `resolve-players`. `04_DATA_MODEL.md`: the quarantine is filled
(S4).

## Tests

- **Owner identity (unit, pseudonymous YAML):** a valid crosswalk passes; an unmapped user fails
  and prints the stanza; an owner mapped twice fails; a `roster_id` mismatch fails; a co-owner must
  be mapped; `medium` confidence fails; `--apply` is idempotent (twice → same rows).
- **Quarantine (integration, sealed fixture + a 2026 stub season):** rows are copied byte for
  byte (every column, compared as JSON and to the original values); live rows are gone; the 2026
  season is re-labelled `sleeper`; exactly one `legacy_amendments` row is written, and the
  quarantine rows reference it; fingerprints are unchanged and verify is clean; a re-run is a no-op;
  it refuses when 2026 is sealed or already `sleeper`; it rolls back completely if a copy check
  fails (fault injection); rostered spans are recomputed.
- **Player resolution (unit):** the sleeper_id hit; identity links followed; DEF folding (`LAR` →
  the `LA` DEF row, `OAK` → `LV`); gsis fallback; a stub is proposed only for league-referenced
  ids; the 2026 pseudonymized fixtures resolve except a known list.
- **Slots (unit):** every label in `04` §4.1, unknown labels raise, and the starter-slot alignment
  with the 2026 `roster_positions`.

## Rollout (BUILD session, with the user)

1. On a copy of the live DB: `owners map --apply`, then `legacy quarantine-2026-stub`, then
   `legacy verify` and `resolve-players`. dz-dashboard 53-route parity against the copy: expect no
   change (2026 has no matchups, so the dashboard already hides it). Any difference gets explained
   before going live.
2. **Ask**, then the same steps on live; migrate nothing (S4 has no migration).
3. Because the dashboard imports the checked-out branch: S4 has no schema change, so restarts are
   safe.

## Done when

- [ ] YAML approved (no `medium` left) and `owners map` validates 12/12; identities applied on live.
- [ ] The stub is quarantined on live, verified byte for byte, with one `legacy_amendments` row;
      season 2026 is `platform='sleeper'`; fingerprints unchanged; rostered spans recomputed.
- [ ] `resolve-players` shows 0 unresolved users and an explained stub list (currently 4).
- [ ] Full gate green; dashboard parity check explained; RUNBOOK + `02` updated; PR → `dev`.

## Known issues (out of scope)

`prune-players` FK failure (5 players); `ruff format` debt (`crawlers/adp/sleeper.py` + 3
scripts/alembic files); vault-staleness warning (S9); console debug lines at `LOG_LEVEL=WARNING`;
dz-dashboard `CLAUDE.md` points at `tests/dashboard` (tests are in `tests/`).

## Decisions (recommended defaults — need the user's approval)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| S4-D1 | How the stub move is audited, and seal-v2 | A dedicated `legacy quarantine-2026-stub` command writes its own `legacy_amendments` row in the same transaction as the move. **No seal-v2**: sealed data is unchanged, and seal-v1 (vault + evergreen) already holds the stub rows. | Wrap the move in `legacy amend` (records nothing for unsealed rows), or force a seal-v2 that equals seal-v1. |
| S4-D2 | Season 2026 after the move | Keep `season_id` 17 (UNIQUE league/year) and relabel it `platform='sleeper'` with the Sleeper league id; S5 fills weeks and status. | Delete it and create a new season row (breaks the "never delete" rule for no gain). |
| S4-D3 | The 2 `medium`-confidence mappings | You confirm them, then they become `confirmed`. The validator rejects anything below `high` from now on. | Accept `medium` as is (D-3 already recorded "all 12 mapped"). |
| S4-D4 | When the 4 unresolved players are stubbed | S4 reports them (dry run); the S5 loader creates the stubs through the same resolver at load time. | Stub them now in S4. |
| S4-D5 | Where the crosswalk lives | The YAML stays local and gitignored (real names). `owner_platform_identities` in the DB is the loaded copy; tests use pseudonymous YAML. | Commit a pseudonymized YAML (the real mapping would still need a local file). |
| S4-D6 | Slot labels | Exactly the `04` §4.1 table; unknown labels fail loudly (no silent `BN`). | Map unknown labels to `BN` with a warning. |
| S4-D7 | Rollout | Copy → dashboard parity → ask → live (the S1–S3 pattern). | Live directly (no migration involved, but the stub move deletes rows). |
