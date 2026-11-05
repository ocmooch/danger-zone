# 01 — ff-pipeline Architecture for the Sleeper Era

**Status:** planned, not started · **Repo:** `danger-zone` (`ff_pipeline`)
**Companions:** `00_SLEEPER_ERA_MASTER_PLAN.md` (program), `02_PIPELINE_BUILD_PLAN.md` (milestones),
`03_LEGACY_PRESERVATION.md` (vault + guard), `04_SLEEPER_FIELD_MAPPING.md` (field crosswalk)

---

## 1. Scope

Add **Sleeper league data** as the platform source for 2026 and every later season, alongside the
frozen NFL.com era, without changing what the NFL.com era says. This document defines the modules,
schema additions, identity rules, scoring path, write safety and operations. Field-level mapping
lives in `04_SLEEPER_FIELD_MAPPING.md`.

## 2. Where Sleeper already lives in the code

| Today | Module | Keep / change |
|-------|--------|---------------|
| Players directory → `players.sleeper_id` | `crawlers/sleeper/endpoints.py::SleeperClient.players` | Keep; extend the dataclass with status/profile fields for S7 |
| Weekly projections → `projections` | `crawlers/sleeper/runner.py` | Keep unchanged |
| Trending adds/drops → `trending_players` | same | Keep unchanged |
| ADP → `player_adp` | `crawlers/adp/sleeper.py` | Keep unchanged |
| HTTP, throttle, retries, fixture seam | `crawlers/sleeper/client.py` (`SleeperSource` protocol, `LiveSleeperSource`, `LocalFixtureSource`) | **Reuse the pattern**, new protocol for league endpoints |

None of these touch league facts, so none are at risk from this program.

## 3. Module layout (new and changed)

```
src/ff_pipeline/
├── crawlers/
│   ├── sleeper/                       # existing: players, projections, trending (unchanged)
│   └── sleeper_league/                # NEW — the league platform adapter
│       ├── __init__.py
│       ├── client.py                  # SleeperLeagueSource protocol; Live / Archive / Fixture sources
│       ├── endpoints.py               # typed frozen dataclasses (SleeperLeague, SleeperRoster, …)
│       ├── lineage.py                 # walk previous_league_id chain → {year: league_id}
│       ├── capture.py                 # fetch-all-for-season → raw archive (no canonical writes)
│       └── runner.py                  # capture → normalize → load → score → verify; run bookkeeping
├── normalizer/
│   ├── sleeper_league.py              # NEW — pure: typed payloads → canonical DTOs
│   ├── slots.py                       # NEW — roster slot taxonomy (Sleeper label → legacy label)
│   └── owner_identity.py              # NEW — owner_platform_identities resolution
├── canonical/                         # NEW — platform-neutral DTOs + the one shared loader
│   ├── dto.py                         # SeasonFacts, TeamFacts, MatchupFacts, LineupFacts,
│   │                                  # TransactionFacts, DraftPickFacts, BracketFacts, PickFacts
│   └── loader.py                      # writes DTOs through repository.upsert; enforces legacy guard
├── scoring/
│   └── sleeper_rules.py               # NEW — scoring_settings → scoring_rules mapping
├── archive/
│   └── raw_store.py                   # NEW — content-addressed raw payload store + source_payloads rows
├── repository/
│   ├── legacy_guard.py                # NEW — seal flag checks, unlock/amend helpers, fingerprints
│   ├── models.py                      # additive models (§5)
│   └── queries.py                     # additive read helpers (§9)
└── cli.py                             # new commands (§11)
```

**What deliberately does not change:** `crawlers/nfl_com/**`, the NFL.com write functions in
`league.py` / `history.py`, `overrides/`, and the NFL.com fixtures. The NFL.com adapter is not
ported onto `canonical/` — it is frozen with its platform, and porting it would put legacy rows
through new code for no gain.

### 3.1 The adapter shape

```
SleeperLeagueSource (HTTP or archive)  →  endpoints.py (typed, coerced)
        │                                        │
        └── capture.py → raw_store (sha256) ─────┘
                                                 ▼
                         normalizer/sleeper_league.py   (pure, no Session)
                                                 ▼
                         canonical/dto.py  (platform-neutral facts, with provenance)
                                                 ▼
                         canonical/loader.py  (Session; upserts; legacy guard; idempotent)
```

Pure normalization means every mapping rule is unit-testable from fixture JSON with no database,
and a future platform move (the league has moved once already) is one new adapter plus the same
loader.

## 4. Capture first: the raw payload archive

Every Sleeper league response is stored **before** it is parsed.

- **Bytes:** `data/raw/sleeper/<sha256[:2]>/<sha256>.json.gz` (gitignored; same content-addressed
  pattern as `data/assets/`).
- **Index table `source_payloads`:** `payload_id, source ('sleeper_league'), endpoint
  ('league'|'users'|'rosters'|'matchups'|'transactions'|'drafts'|'draft_picks'|'traded_picks'|
  'winners_bracket'|'losers_bracket'|'state'|'players'), platform_league_id, season_year, week,
  request_url, sha256, byte_size, fetched_at, http_status, run_id`.
  UNIQUE(`sha256`, `request_url`) — identical re-fetches dedupe; changed payloads (stat
  corrections, late trades) become new rows, giving a full revision history.
- **`ArchiveSleeperLeagueSource`** replays the newest (or an as-of) payload per request without
  network. Re-normalizing a whole season is therefore offline, deterministic and testable.
- The players directory (~5 MB) is archived at most daily; league endpoints every run.
- Raw archives for a completed season go into that season's rolling seal (`03` §10).

## 5. Schema additions (all additive)

One migration per milestone, each verified against a seal-v1 copy (`03` §9).

### 5.1 Columns on existing tables

| Table | New column | Type | Legacy value | Sleeper value |
|-------|-----------|------|--------------|---------------|
| `seasons` | `platform` | TEXT NOT NULL DEFAULT `'nfl_com'` | `nfl_com` (default) | `sleeper` |
| | `platform_league_id` | TEXT | NULL (league_id is the NFL.com ID) | Sleeper league_id for that year |
| | `platform_draft_id` | TEXT | NULL | Sleeper draft_id |
| | `settings_snapshot` | JSON | NULL | league `settings`, `roster_positions`, `scoring_settings`, `metadata` at last sync |
| | `is_sealed`, `sealed_at`, `seal_id` | BOOLEAN/TIMESTAMP/TEXT | set by seal-v1 | set at season close |
| `teams` | `platform_team_key` | TEXT | NULL | `sleeper:roster:<roster_id>` |
| | `division` | TEXT | NULL (BFF derives legacy divisions) | division name/number from league metadata |
| | `extra_data` | JSON | NULL | `co_owner_user_ids`, `waiver_position`, `waiver_budget_used`, `total_moves`, `ppts`, `streak`, `record_string` |
| `matchups` | `platform_matchup_key` | TEXT | NULL (`nfl_com_game_id` stays) | `sleeper:<league_id>:<week>:<matchup_id>` |
| | `extra_data` | JSON | NULL | `custom_points`, `points_official`, `finalized`, bracket refs |
| `transactions` | `platform_transaction_id` | TEXT | NULL | Sleeper `transaction_id` |
| | `status` | TEXT | NULL (all legacy rows are executed) | `complete` / `failed` |
| `players` | `extra_data` | JSON | NULL | static Sleeper profile: `years_exp`, `college`, `height`, `weight`, `fantasy_positions`, other cross-IDs |

New indexes: UNIQUE(`season_id`, `platform_team_key`) where not null; UNIQUE(`season_id`, `week`,
`team_id`) already covers matchups; UNIQUE(`season_id`, `platform_transaction_id`, `player_id`,
`team_id`, `direction`) where `platform_transaction_id` is not null — this gives Sleeper
transactions a natural idempotency key that NFL.com rows never had.

`team_rosters` gets **no** new columns. Sleeper adds extra_data keys only:
`platform_points` (official), `platform_slot` (raw label), `snapshot_kind` (`'sleeper_matchup'`).

### 5.2 New tables

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `owner_platform_identities` | One human, many platform accounts | `owner_id` FK, `platform`, `external_user_id`, `external_display_name`, `first_season`, `last_season`, `notes`; UNIQUE(`platform`, `external_user_id`) |
| `playoff_brackets` | Official winners/losers brackets | `season_id`, `bracket` (`winners`/`losers`), `round`, `match_no`, `team1_id`, `team2_id`, `winner_team_id`, `loser_team_id`, `placement`, `team1_from`/`team2_from` JSON, `source`; UNIQUE(`season_id`,`bracket`,`round`,`match_no`) |
| `traded_picks` | Future/current draft pick ownership | `league_id`, `pick_season`, `round`, `original_owner_id`, `current_owner_id`, `previous_owner_id`, `observed_at`, `source_transaction_id` |
| `player_status_snapshots` | Daily injury / practice / depth state | `player_id`, `as_of_date`, `source`, `injury_status`, `injury_body_part`, `injury_start_date`, `practice_participation`, `depth_chart_position`, `depth_chart_order`, `nfl_team`, `status`; UNIQUE(`player_id`,`as_of_date`,`source`) |
| `source_payloads` | Raw archive index (§4) | see §4 |
| `legacy_guard`, `legacy_amendments`, `legacy_seals` | Preservation (`03` §6–§8) | see `03` |
| `legacy_nfl_com_2026_preseason` | Quarantine for the NFL.com 2026 stub (decision D-2) | copy of the 12 team + 193 roster rows, verbatim, with original PKs |

### 5.3 Why not a parallel `sleeper_*` table set

Parallel tables would force every downstream query to `UNION` two shapes forever and would make
cross-era aggregation the dashboard's problem. The canonical tables already express everything the
two platforms share; the gaps are exactly the new tables above.

## 6. The canonical loader

`canonical/loader.py` is the only code that writes Sleeper league facts.

- Resolves the target season by `(league_id='36271', year, platform='sleeper')`; creates it if
  missing. If an existing season row for that year has `platform != 'sleeper'` or `is_sealed`, it
  stops with `SealedSeasonError` — the D-2 quarantine must run first.
- Calls `legacy_guard.assert_writable()` before every season-scoped write batch.
- Writes through `repository.upsert` with these conflict keys:

| Entity | Conflict key | Replace semantics |
|--------|-------------|-------------------|
| season | `(league_id, year)` | update platform fields, status, week counts |
| team | `(season_id, platform_team_key)` | update name, division, records, extra_data |
| matchup | `(season_id, week, team_id)` | update scores, opponent, flags, extra_data |
| roster snapshot | `(season_year, week, player_id)` + per-(team, week) replace | identical to the NFL.com invariant: one owner per player per week |
| transaction leg | `(season_id, platform_transaction_id, player_id, team_id, direction)` | update status/notes/extra_data |
| draft pick | transaction leg with type `draft` | as above, keyed by draft pick number |
| bracket match | `(season_id, bracket, round, match_no)` | update participants/winner |
| status snapshot | `(player_id, as_of_date, source)` | update same-day values |

- Re-running any week converges to the same rows (idempotency tests per entity).
- `teams.team_abbrev` for Sleeper seasons is set from the owner's existing lineage abbreviation
  (`team_abbrev` is a 2025-lineage label, not per-season — see memory "Owner history
  reconstruction"), so dashboard period-name logic keeps working.
- `teams.team_name` falls back to `"<display_name>'s Team"` when Sleeper has no custom team name,
  keeping `UNIQUE(season_id, team_name)` satisfiable.
- After loading, it calls existing `recompute_rostered_spans` and the season-record derivation so
  `players.first/last_rostered_season` and team W-L-T stay consistent across eras.

## 7. Identity

### 7.1 Owners

Sleeper `user_id`s are mapped to existing `owner_id`s through `owner_platform_identities`, seeded
from `data/owner_platform_identities.yaml` (reviewed by the user; D-3). Rules:

- No automatic name matching — display names already collide across this league's history.
- An unmapped Sleeper user **fails the load** with a message listing the user and the YAML stanza
  to add. A genuinely new manager gets a new `owners` row only through that YAML (`new_owner: true`).
- Co-owners map to `owners` too and are recorded on the team's `extra_data.co_owner_user_ids`; the
  primary `teams.owner_id` is Sleeper's `owner_id` for the roster.
- `owners.is_active`, `joined_year`, `left_year` are recomputed from all eras after each load.

### 7.2 Players

- Sleeper league payloads reference Sleeper `player_id`s. Resolution order: `players.sleeper_id`
  (honoring `player_identity_links`) → `PlayerResolver` via the Sleeper directory's `gsis_id` →
  fuzzy name+position (existing tested rules).
- **Policy change, scoped:** a Sleeper player who is *rostered, drafted or transacted in this
  league* and still unresolved is stubbed as a new `players` row (rostered ⇒ league-relevant). The
  existing "don't stub Sleeper-only players" rule still holds for projections/trending.
- Team defenses: Sleeper uses the team code as the player id (`"PHI"`). Map through
  `nfl_teams.canonical_franchise` to the existing synthetic DEF player for that franchise
  (Sleeper `LAR` → canonical `LA`, etc.), and stamp `sleeper_id` on that row.

## 8. Season lifecycle and capabilities

### 8.1 Lifecycle

| Sleeper `league.status` | `seasons.status` | Pipeline behavior |
|-------------------------|------------------|-------------------|
| `pre_draft` | `pre_draft` | capture league/users/rosters; no matchups |
| `drafting` | `pre_draft` | capture draft picks continuously |
| `in_season` | `in_progress` | daily capture; weekly load; Tuesday + Thursday stat-correction passes |
| `complete` | `completed` | final pass after correction window → reconcile → seal (`03` §10) |

`/v1/state/nfl` provides the current NFL week; a week is `finalized` in `matchups.extra_data` once
the NFL week has ended and Sleeper points are unchanged across two consecutive captures after the
Wednesday correction window.

### 8.2 Rollover

`lineage.py` walks `previous_league_id` back from the configured `SLEEPER_LEAGUE_ID` and, for the
current NFL season, discovers a renewed league via `/user/<commissioner_user_id>/leagues/nfl/<year>`
whose `previous_league_id` points at last year's. Rollover to 2027 is therefore a scheduled run,
not a code change; the new league_id is written to `seasons.platform_league_id` and logged.

### 8.3 Capability matrix

`queries.season_capabilities(session, season_id) -> dict[str, CapabilityState]` returns, per
capability key in the master plan §4.2, one of `available`, `partial`, `derived`,
`not_on_platform`, `not_yet` (in-progress season, data not produced yet). It combines a static
platform table with **data presence** checks (e.g. `faab_failed_bids` is `available` only if
failed-claim rows exist or the league uses FAAB). Data presence wins over platform defaults, so a
future platform quirk can't make the matrix lie.

## 9. Scoring

**Two numbers per player-week, same as the NFL.com era:**

1. **Official league points** — Sleeper `players_points` from the matchup payload →
   `team_rosters.extra_data.platform_points` (rostered weeks). If the matchup carries
   `custom_points` (a commissioner override), the team score uses it and `matchups.extra_data`
   records both values.
2. **Reconstructed points** — nflverse stats (primary, D-5) scored by `scoring/engine.py` with the
   2026 `scoring_rules`, as for every other season. This is the cross-era comparable baseline.

**Rule derivation.** `scoring/sleeper_rules.py` maps `league.scoring_settings` to `scoring_rules`
rows (mapping table in `04` §5). Unmapped keys with non-zero values abort rule writing and are
listed for the user. The derived rule set is diffed against 2025 and shown to the user before
writing (D-10).

**Verification.** The existing `verify --sweep` / `--reconcile` machinery compares reconstructed vs
official points per started player-week; the Sleeper era uses `platform_points` where the NFL.com
era used `nfl_com_points`. Divergences are classified (rules, stat corrections, DST definitions) in
a reconcile report; none are auto-"fixed".

**Optional cross-check.** Sleeper's weekly stats can be stored as `player_stats_raw` with
`source='sleeper'`, `is_primary=false`, to localize stat disagreements. Off by default.

### 9.1 Source-neutral read helpers (`repository/queries.py`)

| Helper | Returns |
|--------|---------|
| `official_slot_points_expr(roster=TeamRoster)` | SQL expr `coalesce(json(platform_points), json(nfl_com_points))` |
| `authoritative_week_points_expr(roster, scored)` | `coalesce(official_slot_points_expr, scored.total_points)` — moves the dashboard's current helper upstream so both platforms share one definition |
| `season_platform(session, season_id)` | `'nfl_com' | 'sleeper'` + label |
| `season_capabilities(session, season_id)` | §8.3 |
| `brackets_for_season`, `traded_picks_for_league`, `player_status_asof`, `failed_waiver_claims` | new-feature reads |

## 10. Rich Sleeper data (beyond parity)

| Facet | Source | Lands in |
|-------|--------|----------|
| Official brackets incl. 3rd place, consolation, toilet bowl | `winners_bracket`, `losers_bracket` | `playoff_brackets`; `matchups.is_playoff/is_consolation` derived from it |
| Failed waiver claims and their bids | `transactions` with `status='failed'` | `transactions` (`waiver_add`, `status='failed'`, `extra_data.faab_bid`) |
| Trades with picks and FAAB | `transactions.draft_picks`, `waiver_budget` | trade legs + `extra_data.picks[]`, `extra_data.faab_transfers[]` |
| Future pick ownership | `traded_picks` | `traded_picks` |
| Potential points / record string / streak / moves | roster `settings` / `metadata` | `teams.extra_data` |
| Divisions | league `settings.divisions`, `metadata.division_*`, roster `settings.division` | `teams.division` |
| Co-owners | roster `co_owners` | `teams.extra_data`, `owner_platform_identities` |
| Commissioner overrides | matchup `custom_points` | `matchups.extra_data` |
| Player status (injury, practice, depth chart) | players directory, daily | `player_status_snapshots` |
| Avatars | `sleepercdn.com/avatars/…`, user `metadata.avatar` | `assets` (`kind='user_avatar'`/`'team_avatar'`) → `teams.*_avatar_asset_id` |
| League settings history | `league` payload per capture | `seasons.settings_snapshot` (latest) + raw archive (full history) |

## 11. CLI surface

| Command | Purpose |
|---------|---------|
| `ff-pipeline legacy seal / verify [--quick] / status / amend / restore-drill` | Preservation (`03`) |
| `ff-pipeline sleeper-league capture --season 2026 [--week N]` | Fetch → raw archive only |
| `ff-pipeline sleeper-league load --season 2026 [--week N] [--from-archive]` | Normalize + load |
| `ff-pipeline run --source sleeper-league` | Scheduled: capture → load → score → verify, with `pipeline_runs` / `source_health` |
| `ff-pipeline sleeper-league lineage` | Print the year → league_id chain |
| `ff-pipeline sleeper-league rules --season 2026 [--apply]` | Derive + diff + (optionally) write scoring rules |
| `ff-pipeline owners map --platform sleeper` | Validate / load `owner_platform_identities.yaml` |

Every write command runs `legacy verify --quick` as a preflight and refuses to run if it fails.

## 12. Settings

New, read at the boundary via `get_settings()`:

| Setting | Default | Notes |
|---------|---------|-------|
| `SLEEPER_LEAGUE_ID` | none (required for league commands only) | D-1: `1393640392104099840` for 2026 (verified) |
| `SLEEPER_RAW_DIR` | `./data/raw/sleeper` | resolved like other paths |
| `LEGACY_VAULT_DIR` | `~/ff-legacy-vault` | copy A |
| `SLEEPER_PLAYERS_REFRESH_HOURS` | `24` | directory capture cadence |

`NFL_COOKIE` is currently required by `Settings`. Make it **optional** (required only by NFL.com
commands, which are now historical) so Sleeper-era operation does not depend on a dead platform's
credential. Update `.env.example` accordingly.

## 13. Failure modes

| Failure | Detection | Response |
|---------|-----------|----------|
| Legacy fingerprint mismatch | preflight `legacy verify --quick` | abort run, name table/season/PKs, point at vault |
| Write targets a sealed season | app guard / DB trigger | abort batch, `SealedSeasonError` |
| Unmapped Sleeper user | owner resolution | abort load, print YAML stanza |
| Unresolved rostered player | resolver | stub + log `identity_stub_created`; listed in run summary |
| Unknown scoring key | rule derivation | refuse to write rules; list keys |
| Unknown roster slot label | slot taxonomy | store raw label in `extra_data.platform_slot`, legacy label `NULL`, `parse_failures += 1` |
| Sleeper 429 / 5xx | client | `Retry-After`/exponential backoff (shared with ADP hardening plan) |
| Payload shape change | typed coercion | skip row, `parse_failures += 1`, raw payload kept for replay |
| Late stat corrections | new payload hash | new `source_payloads` row; reload week; rescore; reconcile |

## 14. Testing

- **Fixtures:** `tests/fixtures/sleeper_league/<scenario>/` — trimmed real payloads captured in S2,
  pseudonymized display names (D-11). Scenarios: regular week, bye, playoff week, trade with picks
  + FAAB, failed waiver, commissioner `custom_points`, co-owner, blank team name, DEF slot.
- **Unit:** normalizer mapping per entity; slot taxonomy; scoring key mapping; lineage walk;
  fingerprint determinism; amendment diff.
- **Integration:** load a fixture season twice → identical DB (idempotency); load into a DB holding
  a sealed fixture season → sealed fingerprints unchanged; triggers block a forced update; migration
  upgrade/downgrade/upgrade preserves fingerprints; archive replay equals live-fixture load.
- **Gate:** the repo's full gate (`pytest`, `ruff check`, `ruff format --check`, `mypy --strict`).
