# S5 — Canonical loader (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S5. Design sources: `01_PIPELINE_ARCHITECTURE.md`
§6 (loader), §8.1 (lifecycle); `04_SLEEPER_FIELD_MAPPING.md` §2–§6 (+ §10 probe evidence);
S4 helpers (`normalizer/{owner_identity,sleeper_players,slots}.py`, `archive_view.py`).

**Status:** PLAN approved 2026-11-09 (decisions S5-D1..D9 as recommended). BUILD in progress on
`feature/s5-canonical-loader`.

## Goal

Load the archived 2026 Sleeper season into the canonical tables (`seasons`, `teams`, `matchups`,
`team_rosters`, `transactions`) **idempotently and offline** (from the raw archive), so that it
looks to every reader exactly like an NFL.com season, with `platform='sleeper'`.

## Starting state (verified 2026-11-09)

- Live DB at S3 head; S4 applied: season 2026 (`season_id` 17) is `platform='sleeper'`,
  `platform_league_id` set, and holds no teams. 12 `owner_platform_identities` exist. All 289
  league-referenced Sleeper players resolve (2 via `player_id_overrides`, 4 DEF by nickname).
- Archive: weeks 1–9 (week 9 in progress), `league.settings.last_scored_leg = 8`, weekly capture
  every Wednesday.
- **NFL.com conventions the loader must match** (the dashboard reads these):
  - transaction types `free_agent_add` / `waiver_add` (in), `drop` (out), `trade` (in/out +
    `counterpart_team_id`), `draft` (`direction='add'`, `effective_week=0`, notes
    `"Round R, Pick P (overall N)"`, `executed_at` = draft time + N s); `waiver_add.extra_data.faab_bid`.
  - `team_rosters`: one row per (season_year, week, player); per-(team, week) replace;
    `extra_data.snapshot_kind` (`history`, `draft` for the week-0 draft snapshot); slots
    `QB RB WR TE R/W/T K DEF BN RES`.
  - `derive_team_records()` aggregates regular-season W-L-T / PF / PA from `matchups`.
- `legacy_guard.assert_writable(session, season_id)` exists. `owners.is_active / joined_year /
  left_year` are **sealed columns** for legacy owners.
- **dz-dashboard is not Sleeper-ready.** It shows any season that has matchups, and makes the
  newest one the default. Cross-season views (records, careers, rivalries) query `matchups`
  directly. Box scores read `nfl_com_points`, which Sleeper rows don't have. Its D0–D3 milestones
  prepare that (see decision S5-D1).

## Design

### 1. Package layout

```
src/ff_pipeline/
├── canonical/
│   ├── dto.py        # frozen dataclasses: SeasonDTO, TeamDTO, MatchupDTO, RosterRowDTO, TxLegDTO, DraftPickDTO
│   └── loader.py     # the only writer of Sleeper league facts (upserts, replace semantics, guard)
├── normalizer/
│   └── sleeper_league.py   # ArchivedLeague + identity maps → DTOs (pure; no DB writes)
└── crawlers/sleeper_league/
    └── runner.py     # capture (optional) → load → derive; used by CLI and `run`
```

The normalizer is pure: archived payloads + resolved ids in, DTOs out. Each entity is tested on its
own. The loader only writes DTOs.

### 2. Mapping (per `04`, with the S2 probe facts)

- **Season** (`season_id` 17): `status` from `league.status` (`in_season` → `in_progress`);
  `regular_season_weeks = playoff_week_start - start_week` (14); `playoff_weeks` = number of
  winners-bracket rounds (3); `platform_draft_id`. (`settings_snapshot` is S7.)
- **Teams** (12): `owner_id` via `owner_platform_identities`; `platform_team_key =
  'sleeper:roster:<roster_id>'`; `team_name` = Sleeper team name, else
  `"<owners.display_name>'s Team"` (S5-D5); `team_abbrev` = the owner's latest legacy abbreviation
  (S5-D6); `draft_position` from `draft.draft_order`; records from finalized matchups, then
  cross-checked against Sleeper's `rosters[].settings` (wins / losses / ties / fpts / fpts_against).
- **Matchups**: one row per (week, roster). The opponent comes from the pair sharing
  `matchup_id`; `matchup_id = null` means no game (`opponent_team_id` NULL). `team_score =
  custom_points` if set, else `points`; `extra_data = {points_official, custom_points?,
  finalized, sleeper_matchup_id}`; `platform_matchup_key = 'sleeper:<league>:<week>:<matchup_id>'`.
  Weeks > `last_scored_leg` are provisional: scores stored, `is_win` NULL, `finalized = false`
  (S5-D2). `is_playoff` / `is_consolation` stay false until brackets (S7); the regular season ends
  at week 14 anyway.
- **Weekly rosters** (`team_rosters`, `snapshot_kind='sleeper_matchup'`): starters map through
  `slots.starter_slots(roster_positions)`, aligned with `starters[]` (`"0"` = empty slot, skipped,
  as NFL.com does). Other players in `players[]` become `BN`, or `RES` when known (S5-D3).
  `extra_data = {snapshot_kind, platform_slot, platform_points}` (official Sleeper points, S5-D4).
  `was_locked_at_kickoff = finalized`.
- **Draft** (week 0): one `draft` transaction per pick (NFL.com convention, `executed_at` =
  `draft.start_time` + overall pick seconds), plus a week-0 `team_rosters` snapshot
  (`snapshot_kind='draft'`) of each team's picks.
- **Transactions** (`04` §6): `free_agent` → `free_agent_add` (in) + `drop` (out) per player;
  `waiver` complete → `waiver_add` (+ `drop`), `extra_data.faab_bid`; `waiver` failed →
  `waiver_add` with `status='failed'`, `faab_bid`, `failure_reason` (S5-D7); `trade` → `trade`
  legs in/out with `counterpart_team_id` (pick / FAAB legs → S7); `commissioner` → add/drop types
  with `extra_data.by_commissioner = true`. `executed_at = status_updated`, `effective_week =
  leg`, `platform_transaction_id`, `status`. Idempotent on the S3 partial expression index. There
  are no `lineup_change` rows: Sleeper exposes no lineup log (capability `not_on_platform`).
- **Players**: `sleeper_players.resolve()`. On load, stamp `players.sleeper_id` where the match
  came from a DEF name, franchise or override and the row has no `sleeper_id`. Stub
  league-referenced players that remain unresolved (0 today) with `sleeper_id`, name, position and
  team.

### 3. Loader rules

- Season resolution: `(league_id='36271', year)` must exist with `platform='sleeper'` and
  unsealed, else `SealedSeasonError` / `PlatformMismatchError`. `assert_writable()` runs before
  each batch.
- Order: season → teams → draft → matchups + rosters → transactions → records → rostered spans →
  owners recompute. One transaction per run; the caller commits.
- Replace semantics: per-(team, week) roster replace + the (season_year, week, player)
  cross-team upsert (same as NFL.com); matchups upsert on (season, week, team); transactions on the
  partial unique index; draft picks by (season, pick number) via `platform_transaction_id =
  'draft:<draft_id>:<pick_no>'`.
- Owners recompute (S5-D8): computed across all eras. It is written only for owners not
  registered in the seal; a needed change to a sealed owner is reported (it needs `legacy amend`),
  never forced. For 2026 it is a no-op: the same 12 managers.
- A load of week N never touches other weeks. A full reload equals the previous load (row by row).

### 4. CLI

- `ff-pipeline sleeper-league load [--season 2026] [--dry-run]`: archive → canonical. It prints
  counts per entity and the records cross-check (`ok` or a per-team diff). Writing command,
  so the legacy preflight runs first.
- `ff-pipeline run --source sleeper-league`: capture, then load. It is not in the default `run`
  until the live load is approved (S5-D1/D9).

## Tests

- **Normalizer (unit, `tests/fixtures/sleeper_league/season_2026` + `synthetic/`):** season
  fields; team mapping and the name fallback (the blank-team-name fixture); matchup pairing incl.
  a null `matchup_id` (bye); `custom_points` override (synthetic); starter/slot alignment and
  the empty `"0"` slot; provisional vs finalized weeks; draft notes and order; transaction legs for
  free agent, waiver complete, waiver **failed**, and trade with counterpart (synthetic);
  commissioner flag; a DEF code resolves to the DEF row.
- **Loader (integration, migrated temp DB + sealed fixture season + identities + fixture
  players):** load twice gives an identical DB; one owner per player per week; reload after a
  changed week replaces only that week; a write into a sealed season raises; fingerprints of the
  sealed season are unchanged; records equal Sleeper's roster settings; rostered spans include
  2026.
- **Archive equivalence:** loading from `ArchiveSleeperLeagueSource` equals loading from the
  fixture source for the same payloads.
- **Owners recompute:** no write to a sealed owner; a reported change when one would be needed.

## Rollout (BUILD session, with the user)

1. Load into a **copy** of the live DB: `legacy verify` → `load --season 2026` → `legacy verify`
   → records cross-check 12/12 → spot-check three box scores against the Sleeper app (the user
   compares; I prepare the three).
2. Live load per decision S5-D1.

## Done when

- [x] `sleeper-league load` loads weeks 1..current on a copy of the live DB (live per S5-D1: later).
- [x] Team W-L-T / PF / PA equal Sleeper's roster settings for all 12 teams (finalized weeks).
- [ ] Three box scores spot-checked against the Sleeper app by the user.
- [x] Load is idempotent; `legacy verify` passes; full gate green. [ ] `02` §S5 updated; PR → `dev`.

## As built (2026-11-09)

Code: `canonical/{dto,loader}.py`, `normalizer/sleeper_league.py`,
`crawlers/sleeper_league/runner.py`, `archive_view.load_league` (any source), CLI
`sleeper-league load [--dry-run]`. `run --source sleeper-league` was **not** added: with the live
load deferred (S5-D1), capture and load stay separate commands until the dashboard is ready.

### Deviations from the design above

1. **Sync instead of delete-and-insert.** Every entity is matched on its natural key and updated
   in place, inserted or removed (stale weekly roster rows and transaction legs only; teams and
   matchup weeks are never deleted). A reload is a true no-op and surrogate ids stay stable.
2. **Draft position fallback.** If `draft.slot_to_roster_id` is absent (as in the pseudonymized
   fixture), positions come from the round-1 picks' `draft_slot`.
3. **Commissioner overrides touch two rows.** `custom_points` changes the team's `team_score` and
   its opponent's `opponent_score`.
4. **Records come from the loader**, not `derive_team_records()`, which would count provisional
   weeks (`is_win` NULL with scores) as ties.

### Verification on a copy of the live DB (2026-11-09)

- First load: teams +12, matchups +108, team_rosters +1,929 (180 week-0 draft rows + weeks 1–9),
  transactions +818 (180 draft, 139 free-agent adds, 106 waiver adds, 162 failed claims, 231
  drops). 4 nickname-only DEF rows got `sleeper_id`; 0 stubs.
- **Records vs Sleeper: all 12 teams equal** (W-L-T, PF, PA over weeks 1–8).
- Second load: every entity unchanged (`+0 ~0 -0`). `legacy verify` PASSED; 41/41 triggers.
- IR: week 9 has its 14 IR players as `RES` (an archived rosters revision exists); weeks 1–8
  non-starters are `BN` + `slot_certainty='bench_or_reserve'`.
- Owners recompute: no change (the same 12 managers are active).
- Box-score spot-check: three games (weeks 1, 5, 8); starters' points sum exactly to each team
  score. **The user compares them with the Sleeper app.**

## Known issues (out of scope)

`prune-players` FK failure (5 players); `ruff format` debt (`crawlers/adp/sleeper.py`); PyYAML
undeclared; console debug lines at `LOG_LEVEL=WARNING`; vault-staleness warning (S9).

## Decisions (defaults approved by the user 2026-11-09)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| S5-D1 | Where the 2026 load goes, given the dashboard is not Sleeper-ready | S5 builds and verifies on a **DB copy**. The **live** load is a separate, one-command, approved step taken when the dashboard is ready to show 2026 (after its D0 legacy snapshots, ahead of D3). Until then your dashboard stays exactly as today. | Load live now and accept a rough 2026 in the dashboard (2026 becomes the default season; box scores show no points; career and record views include partial 2026 games). |
| S5-D2 | Finalized vs provisional weeks | `finalized` = week ≤ `league.settings.last_scored_leg` (Sleeper's own scored weeks). Provisional weeks keep scores, with `is_win` NULL, and don't count toward records. | Treat every loaded week as final (W-L would count the in-progress week). |
| S5-D3 | Bench vs IR for past weeks | `RES` only when a `rosters` archive revision from that week shows the player in `reserve`. Otherwise `BN` with `extra_data.slot_certainty='bench_or_reserve'`, an honest gap flag. Weeks 1–8 of 2026 predate the archive, so they get the flag. | Guess IR from "more non-starters than bench slots" (can't tell which player). |
| S5-D4 | Player points in rosters | Store Sleeper's official per-player points as `extra_data.platform_points`. Do **not** write `nfl_com_points`. `player_stats_scored` for 2026 waits for S6. | Also copy them into `nfl_com_points` so today's dashboard shows them (mislabels the source). |
| S5-D5 | Fallback team name | `"<owners.display_name>'s Team"` (your canonical owner name), not the Sleeper username. | Sleeper display name, as `04` §3 suggested. |
| S5-D6 | `teams.team_abbrev` | The owner's latest legacy abbreviation, so lineage-based dashboard logic keeps working. | NULL for Sleeper seasons. |
| S5-D7 | Failed waiver claims | Load them now as `waiver_add` rows with `status='failed'` (plus `faab_bid`, `failure_reason`); S7 builds views on them. | Skip them until S7. |
| S5-D8 | Owners active/joined/left | Recompute across eras; write only unsealed owner rows; report (never force) a needed change to a sealed owner, since it needs `legacy amend`. | Unlock and write. |
| S5-D9 | Weekly automation | `dz-sleeper-capture` stays capture-only until the live load is approved; then the wrapper adds `load`. | Add `load` to the weekly job now (against live). |
