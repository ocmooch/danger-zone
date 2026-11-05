# 00 — Sleeper Era Integration: Master Plan (cross-project)

**Status:** planned, not started · **Written:** 2026-11-05 · **Branch:** `feature/sleeper-era-plan`
**Scope:** `danger-zone` (`ff-pipeline`, Phase 1) **and** `dz-dashboard` (Phase 2), together.
**Canonical home:** this file lives in `danger-zone` because the data foundation owns the contract.
`dz-dashboard/docs/sleeper/README.md` points back here.

---

## Document set (read side by side)

| # | Document | Repo | Purpose |
|---|----------|------|---------|
| 00 | **This file** — master plan | danger-zone | Program architecture, cross-repo contract, joint phasing, decisions, risks |
| 01 | [`01_PIPELINE_ARCHITECTURE.md`](01_PIPELINE_ARCHITECTURE.md) | danger-zone | How `ff-pipeline` ingests, normalizes, stores and serves Sleeper-era data |
| 02 | [`02_PIPELINE_BUILD_PLAN.md`](02_PIPELINE_BUILD_PLAN.md) | danger-zone | Milestones S0–S9: files, tests, "Done when", gates |
| 03 | [`03_LEGACY_PRESERVATION.md`](03_LEGACY_PRESERVATION.md) | danger-zone | **Priority one.** Sealing, verifying and backing up the NFL.com era |
| 04 | [`04_SLEEPER_FIELD_MAPPING.md`](04_SLEEPER_FIELD_MAPPING.md) | danger-zone | Field-by-field crosswalk: Sleeper endpoints → canonical schema |
| 01 | `dz-dashboard/docs/sleeper/01_DASHBOARD_ARCHITECTURE.md` | dz-dashboard | BFF + SPA design for mixed-platform history |
| 02 | `dz-dashboard/docs/sleeper/02_DASHBOARD_BUILD_PLAN.md` | dz-dashboard | Milestones D0–D8: files, tests, "Done when" |
| 03 | `dz-dashboard/docs/sleeper/03_CROSS_PLATFORM_UX.md` | dz-dashboard | Visual and UX consistency rules across platform eras |

Reading order for a new session: this file §1–§5 → the repo's own architecture doc → that repo's
build plan row for the current milestone. Do not read all eight documents in one session.

---

## 1. Situation

**The NFL.com era is over and cannot be re-captured.** NFL Fantasy is defunct. Everything the league
did on it from 2010 to 2025 now exists in exactly one place: `danger-zone/data/fantasy.db` (plus
same-disk backup copies). No raw HTML archive was kept (`SAVE_RAW_HTML` was off), so the database
*is* the primary source.

Live inventory (read-only query of `data/fantasy.db`, 2026-11-05, alembic head `d4e6f8a0b2c4`):

| Facet | Rows | Coverage |
|-------|------|----------|
| Seasons | 17 | 2010–2025 `completed`; **2026 `in_progress` (NFL.com stub — see D-2)** |
| Owners / teams | 18 / 204 | 12 active managers |
| Matchups | 3,002 | every week, 2010–2025, playoff + consolation flags |
| Roster snapshots (`team_rosters`) | 49,594 | 2010–2025 weekly, plus 193 rows for 2026 week 1 |
| Transactions | 41,974 | draft, add/drop, waiver (with FAAB), trade, lineup changes, setting changes |
| Scoring rules | 816 | 2010–2025, per season |
| Avatars (`assets`) | 171 | bytes on disk under `data/assets/` |
| nflverse stats / scored | 110,942 / 110,942 | 2010–2025 — re-derivable |
| Projections / ADP | 50,375 / 54,813 | Sleeper/FFC/MFL — re-derivable |

**The league now plays on Sleeper.** The 2026 season is in progress (Sleeper `leg = 9`,
`last_scored_leg = 8` on 2026-11-05). Verified league settings: 12 teams, 14-week regular season
(`playoff_week_start = 15`), 6 playoff teams, FAAB with a $100 budget, no divisions, no IR/taxi
slots, lineup `QB, RB×2, WR×2, TE, FLEX, K, DEF` + 6 bench — the same shape as the NFL.com era. Sleeper is not new to the pipeline — it already supplies projections, trending players,
ADP and `players.sleeper_id` (3,266 players carry one). What is new is **league data**: the
league's own teams, rosters, matchups, transactions, draft and brackets.

## 2. Goals and non-goals

**Goals**

1. **Preserve the NFL.com era at full fidelity** — sealed, independently verifiable, backed up off
   this machine, and protected from accidental mutation by future Sleeper work. *Highest priority;
   gates everything else.*
2. **Ingest current and future Sleeper seasons** into the existing canonical schema so that a 2026
   matchup, roster, trade or champion is the same kind of row as a 2015 one.
3. **Extend the schema additively** where Sleeper offers facts NFL.com never did (official
   brackets, failed FAAB bids, traded future picks, co-owners, potential points, player status).
4. **Make platform a provenance attribute, not a fork.** Downstream code asks "does this season have
   capability X?", never "is this Sleeper?".
5. **Give the dashboard one consistent experience** across platform eras, including aggregates that
   span both (careers, rivalries, records), with honest per-capability gaps.
6. **Roll forward automatically** — each new Sleeper season (new `league_id` every year) is picked up
   through Sleeper's `previous_league_id` chain without code changes.

**Non-goals**

- Rewriting, re-deriving or "normalizing" stored NFL.com facts to look like Sleeper data.
- Refactoring the NFL.com crawler or its write path. It is frozen with the platform.
- Live, real-time scoring. In-season refresh is scheduled (daily + post-stat-correction).
- Sleeper write actions (lineups, trades) or authenticated endpoints. Public read API only.
- League chat, polls or news text — not exposed by Sleeper's public API.
- Phase 3 (predictive advice). Unchanged boundary.

## 3. Principles (both repos)

1. **Legacy is sealed, not migrated.** NFL.com-era rows are never rewritten to fit the new world.
   New data adapts to the legacy shape; where it can't, the schema grows *beside* it.
2. **Seal before you touch.** No Sleeper schema migration runs before Legacy Seal v1 exists in
   three places and verifies (see `03_LEGACY_PRESERVATION.md`).
3. **Capture before model.** Every Sleeper league payload is archived raw (content-addressed JSON)
   *before* normalization. The NFL.com lesson: the platform can vanish; the raw record must not.
4. **One canonical model; platform is provenance.** Same tables, same keys, same semantics.
   `seasons.platform` records where a season came from. Platform-specific detail lives in typed
   additive columns or `extra_data`, never in a parallel table set.
5. **Additive schema only.** New columns are nullable or defaulted; new tables are new. No column
   drops, renames or type changes on legacy tables during this program.
6. **Source-neutral reads at the seam.** "The league's official points for a player-week" is one
   Phase 1 helper that knows both `nfl_com_points` and Sleeper's points. The dashboard never
   re-implements per-platform logic.
7. **Capabilities, not platform checks.** The BFF publishes a per-season capability set. The SPA
   renders from capabilities. A grep for `sleeper` in `web/src` business paths should find nothing
   but labels.
8. **Honest gaps, per capability.** A Sleeper season without lineup-change history and an NFL.com
   season without official brackets are both rendered as explained gaps — never as zero, never as
   silently missing.
9. **Rolling seal.** When a Sleeper season completes, it is finalized and sealed exactly like the
   legacy era. Every finished season becomes protected history.

## 4. System architecture at large

```
                       ┌──────────────────────────── EXTERNAL ────────────────────────────┐
                       │  NFL.com  ✝ (defunct)     nflverse releases      Sleeper public API  │
                       └──────┬───────────────────────────┬──────────────────────┬──────────┘
                              │ (never again)             │                      │
  ┌──────────────── danger-zone / ff-pipeline ────────────┼──────────────────────┼──────────────┐
  │                           │                           ▼                      ▼              │
  │   ┌───────────────────┐   │                  nflverse crawler     Sleeper league client     │
  │   │  LEGACY VAULT     │   │                  (stats, players,     (leagues, users, rosters,  │
  │   │  seal-v1 (2010–25)│   │                   injuries, PBP)       matchups, txns, drafts,   │
  │   │  3 copies, hashes │   │                         │              brackets, state)          │
  │   └─────────▲─────────┘   │                         │                      │                 │
  │             │ seal /      │                         │              ┌───────▼────────┐        │
  │             │ verify      │                         │              │ RAW PAYLOAD    │        │
  │   ┌─────────┴─────────┐   │                         │              │ ARCHIVE (sha)  │        │
  │   │ LEGACY GUARD      │   │                         │              └───────┬────────┘        │
  │   │ fingerprints,     │   │                         │                      ▼                 │
  │   │ triggers,         │◄──┼─── blocks writes ───────┼──── Sleeper normalizer → canonical DTOs │
  │   │ amendments log    │   │    to sealed seasons    │                      │                 │
  │   └───────────────────┘   │                         ▼                      ▼                 │
  │                           │                ┌────────────────────────────────────────┐        │
  │                           │                │ CANONICAL SCHEMA (one model, all eras) │        │
  │                           │                │ seasons.platform · owner_platform_ids  │        │
  │                           │                │ teams · matchups · team_rosters ·      │        │
  │                           │                │ transactions · brackets · picks ·      │        │
  │                           │                │ player_status_snapshots · scoring      │        │
  │                           │                └───────────────────┬────────────────────┘        │
  │                           │                                    ▼                             │
  │                           │        repository.queries — source-neutral read helpers          │
  │                           │        (official points, season platform, capabilities)          │
  └───────────────────────────┼────────────────────────────────────┼─────────────────────────────┘
                              │                                    │ read-only (WAL)
  ┌────────────────── dz-dashboard ────────────────────────────────▼─────────────────────────────┐
  │  BFF analytics/ (pure)  — platform.py: provenance + capability matrix                          │
  │                         — era-aware aggregation (careers, records, rivalries)                  │
  │  /v1/* contract         — SeasonOut.platform + capabilities; new feature endpoints             │
  │  SPA web/               — renders from capabilities; one design language; era markers          │
  └────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 The three contracts between the repos

| Contract | Owner | What changes in this program |
|----------|-------|------------------------------|
| **DB schema** (`repository.models`, alembic) | danger-zone | Additive: platform columns on `seasons`, `teams`, `matchups`, `transactions`; new tables `owner_platform_identities`, `playoff_brackets`, `traded_picks`, `player_status_snapshots`, `source_payloads`, legacy-guard tables. Detail in pipeline doc §5. |
| **Python read seam** (`repository.queries`) | danger-zone | New helpers: `official_slot_points_expr()`, `season_platform()`, `season_capabilities()`, `brackets_for_season()`, `traded_picks_for_league()`, `player_status_asof()`. The dashboard may also add read-only helpers here (existing rule). |
| **HTTP contract** (`/v1/*` → generated TS client) | dz-dashboard | Additive: `platform`, `platform_label`, `capabilities` on season-scoped payloads; `/v1/meta` era list; new endpoints for brackets, FAAB market, picks ledger, player status. |

### 4.2 The platform capability matrix (drives both repos)

This is the single most important artifact for UX consistency. Phase 1 computes it from data
presence plus platform defaults (pipeline doc §8); the BFF serves it; the SPA renders from it.

| Capability key | NFL.com era (2010–2025) | Sleeper era (2026→) | Notes |
|----------------|-------------------------|---------------------|-------|
| `standings` · `matchups` · `rosters_weekly` · `draft` | ✅ | ✅ | Core parity |
| `official_player_points` | ✅ `nfl_com_points` (rostered weeks) | ✅ Sleeper `players_points` | One helper, two sources |
| `reconstructed_player_points` | ✅ nflverse + dz-rules | ✅ same engine | Cross-era comparable baseline |
| `transactions_moves` (add/drop/waiver/trade) | ✅ | ✅ | |
| `faab_winning_bids` | ✅ (where captured) | ✅ | |
| `faab_failed_bids` | ❌ | ✅ new | Losing claims, bid wars |
| `lineup_change_log` | ✅ | ❌ not exposed | Gap in Sleeper era |
| `setting_change_log` | ✅ | ❌ not exposed | Settings *snapshot* exists instead |
| `official_brackets` | ◐ derived (`is_consolation`) | ✅ winners + losers brackets with placements | |
| `official_divisions` | ◐ derived by BFF (2010–2019) | ✅ if league uses divisions | |
| `potential_points` (max PF) | ◐ BFF-computed | ✅ official `ppts` + computed | |
| `trades_with_picks_faab` | ❌ | ✅ new | Picks and FAAB as trade assets |
| `traded_future_picks` | ❌ | ✅ new | |
| `co_owners` | ❌ | ✅ new | |
| `commissioner_score_overrides` | ❌ | ✅ new (`custom_points`) | |
| `player_status_daily` (injury, practice, depth) | ◐ nflverse injuries to 2024 | ✅ Sleeper snapshots | Fills the post-2024 injury gap |
| `availability_weekly` | ◐ current season only | ✅ derivable every week | |
| `avatars` | ✅ team logos | ✅ user + team avatars (downloaded upstream) | |

Legend: ✅ available · ◐ partial/derived · ❌ not available on that platform.

## 5. Joint phasing

Pipeline milestones (S) gate dashboard milestones (D). Nothing in either column starts before S0.

```
 Week →      1        2        3        4        5        6        7        8
 Pipeline  [S0 Seal][S1 Guard][S2 Capture][S3 Schema][S4 Identity][S5 Load][S6 Score][S7 Rich][S8 Read][S9 Ops]
                                   │           │                       │        │          │
 Dashboard                         │      [D0 Mixed fixture]  [D1 Provenance][D2 Points parity]
                                   │                                   [D3 Current season][D4 Cross-era]
                                   │                                            [D5 Brackets][D6 Market][D7 Status][D8 Polish]
```

| Gate | Pipeline delivers | Unblocks dashboard |
|------|-------------------|--------------------|
| G0 | S0: Legacy Seal v1 in 3 locations, manifest verified | Everything (no Phase 1 schema work before this) |
| G1 | S3: additive migration merged + legacy fingerprints unchanged | D0 mixed-era fixture DB |
| G2 | S5: 2026 season loaded (teams, matchups, rosters, transactions) | D1 provenance, D3 current season |
| G3 | S6 + S8: official points helper + scored 2026 | D2 points parity, D4 cross-era |
| G4 | S7: brackets, picks, failed bids, status snapshots | D5–D7 |
| G5 | S9: season-close + rolling seal runbook | D8 + v-next release of both repos |

**Fast path for the live season.** Sleeper retains full history for its leagues, so the 2026 season
is not at risk of loss the way NFL.com was. But S2 (raw capture) is deliberately early: from the
day it lands, a scheduled capture archives every Sleeper payload weekly, so the Sleeper era is
never single-sourced, even before the normalizer exists.

## 6. Decision register

Decisions marked **USER** need the league owner's input before the milestone shown. All others ship
at the recommended default and are reversible.

| ID | Decision | Recommended default | Needed by |
|----|----------|---------------------|-----------|
| D-1 | **Sleeper league ID** | ✅ **Verified 2026-11-05:** `1393640392104099840` = "The Danger Zone", season 2026, `in_season`, 12 rosters, `previous_league_id = 0` (Sleeper history starts in 2026). Store as `SLEEPER_LEAGUE_ID` in `.env`; discover future seasons via lineage. | done |
| D-2 | **2026 NFL.com stub** (12 teams + 193 week-1 roster rows captured 2026-06-05 into season 2026) | ✅ **Decided:** a placeholder, not a real league season — the whole 2026 season is on Sleeper. Archive it: include in Seal v1, then move it verbatim into `legacy_nfl_com_2026_preseason` via an audited amendment so Sleeper owns season 2026. Never delete. | done |
| D-3 | **Owner mapping** Sleeper `user_id` → `owner_id` | ✅ Manual YAML crosswalk at `data/owner_platform_identities.yaml` (local only, gitignored). **All 12 Sleeper users mapped** (2026-11-05; the four ambiguous ones confirmed by the user). Unmapped users in future seasons fail the load loudly. | done |
| D-4 | Canonical league key | Keep `leagues.league_id = '36271'` as the permanent franchise key; Sleeper league IDs live on `seasons.platform_league_id` | S3 |
| D-5 | Primary raw-stat source for the Sleeper era | nflverse stays primary (same engine across all eras); Sleeper stats stored as non-primary cross-check | S6 |
| D-6 | Official player points for the Sleeper era | Sleeper `players_points` (with `custom_points` overrides flagged), parallel to `nfl_com_points` | S5 |
| D-7 | **Off-machine backup targets** for the Legacy Vault | ✅ **Decided (user-controlled cloud accounts):** encrypted seal archive in (B) Google Drive, (C) iCloud Drive, (D) a separate **private** GitHub repo `ocmooch/ff-legacy-vault` (release asset; created, private ✅). Not in `danger-zone` / `dz-dashboard`: both are **public**, and git history is permanent. Weekly automated sync, retention "seal-v1 pinned + 4 newest" per destination, and the age key scheme: `05_VAULT_OFFSITE_SYNC.md`. | done; key ceremony by **USER** before S0 |
| D-8 | Commit the seal *manifest* (hashes only, no data) to git as tamper evidence | Yes — `docs/sleeper/seals/seal-v1.manifest.json`; contains counts + hashes, no names | S0 |
| D-9 | Sleeper roster slot labels | Store in the legacy vocabulary (`QB/RB/WR/TE/R/W/T/W/R/K/DEF/BN/RES`); keep raw label in `extra_data.platform_slot`; add new labels only for new slot types (`Q/R/W/T`, `TAXI`, `DL/LB/DB`) | S5 |
| D-10 | Sleeper scoring mapping sign-off | TBD. The live league's `scoring_settings` were compared with 2025 on 2026-11-05 (`04` §5.1): same core rules, plus new +1 bonuses for 40+ yard completions/rushes/receptions and a few rare DEF/ST events. Final sign-off happens in S6 against the derived rule set. | **USER**, S6 |
| D-11 | Fixtures from the real league | Trim to minimal payloads and replace display names, team names and user IDs with stable pseudonyms before committing — **required, both repos are public** | S2 |
| D-12 | Lineup-change history in the Sleeper era | Accept as a capability gap; optionally derive start/sit moves by diffing weekly starters later (flagged `derived`) | D4 |

## 7. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Legacy rows mutated by a Sleeper migration or loader bug | Medium | **Severe, irreversible** | Seal v1 + DB triggers on sealed seasons + fingerprint preflight on every run + migration dry-run on a vault copy |
| Single-disk loss (WSL VHDX corruption, laptop failure) | Low–medium | **Severe** | 3-2-1 vault copies, restore drill |
| Season-2026 collision with the NFL.com stub (`UNIQUE(league_id, year)`) | Certain if ignored | High | D-2 quarantine amendment before S5 |
| Owner mis-mapping (two "Dan"-style ambiguities already exist) | Medium | High (careers wrong) | Manual crosswalk, load-time hard fail, dashboard owner-continuity check |
| Scoring mapping drift (fractional yards, cumulative vs tiered bonuses, DST points-allowed definition) | Medium | Medium | `verify --sweep` vs Sleeper `players_points`; reconcile report; D-10 sign-off |
| Sleeper API shape change / undocumented fields | Medium | Medium | Raw archive + typed coercion layer + fixture tests; replay from archive |
| `team_name` uniqueness per season (blank Sleeper team names) | Medium | Low | Deterministic fallback to `"{display_name}'s team"`, plus `platform_team_key` unique index |
| Dashboard silently mixes incomparable cross-era metrics | Medium | Medium | Capability matrix, era markers, legacy-parity golden tests |
| Scope creep into real-time features | Medium | Low | Non-goal; scheduled refresh only |

## 8. Program "Done when"

- Legacy Seal v1 exists in three locations; `ff-pipeline legacy verify` passes on the live DB and on
  a restored copy; the restore drill is documented and has been run once.
- Every sealed season is protected by DB-level guards; the amendment path is the only way to change
  a sealed row, and it leaves an audit record.
- The 2026 Sleeper season (weeks to date) is in the canonical tables, scored, verified against
  Sleeper's points, and refreshing on schedule; the raw archive holds every payload.
- The dashboard renders 2010–2026 with one visual language: per-season provenance and capabilities,
  era-aware aggregates, honest per-capability gaps, and the new Sleeper-only features.
- Legacy-parity golden tests prove 2010–2025 dashboard responses are unchanged by the program.
- Season rollover to 2027 is a runbook step, not a code change.
- Docs updated in both repos (data model, data sources, API contract, operations, roadmap rows).
