# 04 — Data Model

The schema is designed for:
- **SQLite compatibility today**, PostgreSQL compatibility tomorrow (no SQLite-only types or syntax)
- **Multi-source provenance**: every fact is tagged with its source and ingestion timestamp
- **Idempotent upserts**: every write goes through `INSERT ... ON CONFLICT ... DO UPDATE`
- **Append-only stat data**: corrections from nflverse produce new versions; old data is preserved

## Conventions

- All tables use snake_case
- All primary keys are explicit (`{entity}_id`) — never just `id`
- All foreign keys are explicit and named (`league_id REFERENCES leagues(league_id)`)
- All tables have `created_at` and `updated_at` timestamp columns (`TEXT ISO-8601` in SQLite, `TIMESTAMPTZ` in PostgreSQL — abstracted via SQLAlchemy)
- JSON fields use `TEXT` in SQLite (validated as JSON via constraint) and `JSONB` in PostgreSQL
- All boolean columns are `BOOLEAN` (SQLAlchemy maps to INTEGER 0/1 on SQLite)
- Stat values are `REAL` (SQLite) / `DOUBLE PRECISION` (Postgres)
- **Extensibility pattern**: most entity tables include an `extra_data` JSON column for opportunistic capture of fields we haven't yet promoted to first-class columns. The crawler can stash newly-discovered data immediately; we promote useful fields to typed columns in a later migration. This decouples "we saw something new on the page" from "we modeled it cleanly," which is essential when sources keep evolving.

## Schema diagram (logical)

```
                          ┌─────────────┐
                          │   leagues   │
                          └──────┬──────┘
                                 │ 1:N
                          ┌──────▼──────┐         ┌──────────────────┐
                          │   seasons   │◄────────┤  scoring_rules   │
                          └──┬─────┬────┘         └──────────────────┘
                             │     │
                       1:N   │     │   1:N
                  ┌──────────┘     └──────────┐
                  │                            │
           ┌──────▼─────┐               ┌─────▼──────┐
           │    teams   │               │  matchups  │
           └──┬─────────┘               └────────────┘
              │ 1:N
              ▼
       ┌──────────────┐
       │ team_rosters │  ←─ point-in-time roster snapshots
       └──────┬───────┘
              │ N:1
              │           ┌──────────────────┐
              └──────────►│     players      │
                          └────────┬─────────┘
                                   │ 1:N
                                   ▼
                          ┌──────────────────┐
                          │ player_stats_raw │
                          └────────┬─────────┘
                                   │ 1:N
                                   ▼
                          ┌────────────────────┐
                          │ player_stats_scored│
                          └────────────────────┘
```

## Tables

### `leagues`
The user's league (the schema supports multiple leagues but Phase 1 uses one).

| Column | Type | Notes |
|--------|------|-------|
| `league_id` | TEXT PK | The NFL.com league ID |
| `name` | TEXT | Human-readable name |
| `platform` | TEXT | `'nfl_com'` for now |
| `current_season_year` | INTEGER | The most recent season's year |
| `created_at` | TIMESTAMP | |
| `updated_at` | TIMESTAMP | |

### `seasons`
One row per (league, year). 13 years on NFL.com → 13 rows.

| Column | Type | Notes |
|--------|------|-------|
| `season_id` | INTEGER PK AUTOINCREMENT | Surrogate key |
| `league_id` | TEXT FK → leagues | |
| `year` | INTEGER | 2014, 2015, ... |
| `status` | TEXT | `'completed'`, `'in_progress'`, `'pre_draft'` |
| `regular_season_weeks` | INTEGER | Usually 14 or 17 depending on league config |
| `playoff_weeks` | INTEGER | |
| `champion_team_id` | INTEGER FK → teams | NULL if not completed |
| `runner_up_team_id` | INTEGER FK → teams | NULL if not completed |
| `last_place_team_id` | INTEGER FK → teams | NULL if not completed |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`league_id`, `year`)

### `owners`
A persistent person — one human, even if they had different team names across seasons.

| Column | Type | Notes |
|--------|------|-------|
| `owner_id` | INTEGER PK AUTOINCREMENT | |
| `league_id` | TEXT FK → leagues | |
| `display_name` | TEXT | Their NFL.com display name |
| `nfl_user_id` | TEXT | Their NFL.com user ID, if scrapeable |
| `aliases` | TEXT (JSON array/object) | Other names they've gone by; may include structured `display_names` / `nfl_user_ids` for manually merged identities |
| `is_active` | BOOLEAN | Still in the league this season? |
| `joined_year` | INTEGER | First season in this league |
| `left_year` | INTEGER | NULL if active |
| `created_at`, `updated_at` | TIMESTAMP | |

### `owner_identity_overrides`
Manual pins that force multiple NFL.com owner identities to resolve to one canonical manager before owner rows are upserted. Used for known same-person aliases such as the two Adam user IDs and the two Ill user IDs; reconstruction still keeps different managers distinct unless an override exists.

| Column | Type | Notes |
|--------|------|-------|
| `override_id` | INTEGER PK AUTOINCREMENT | |
| `league_id` | TEXT FK → leagues | |
| `external_id_kind` | TEXT | `display_name` or `nfl_user_id` |
| `external_id_value` | TEXT | Observed NFL.com display/user value |
| `canonical_display_name` | TEXT | Canonical owner display name to use |
| `notes` | TEXT | Manual-review context |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`league_id`, `external_id_kind`, `external_id_value`)

### `teams`
One row per (season, team). A single owner shows up in N rows, one per season they were in.

| Column | Type | Notes |
|--------|------|-------|
| `team_id` | INTEGER PK AUTOINCREMENT | |
| `season_id` | INTEGER FK → seasons | |
| `owner_id` | INTEGER FK → owners | |
| `team_name` | TEXT | The team's name THAT season |
| `team_abbrev` | TEXT | NFL.com 3-letter abbrev for the team |
| `draft_position` | INTEGER | 1..N where N is league size |
| `final_rank` | INTEGER | After playoffs; NULL if season in progress |
| `regular_season_wins` | INTEGER | |
| `regular_season_losses` | INTEGER | |
| `regular_season_ties` | INTEGER | |
| `regular_season_points_for` | REAL | |
| `regular_season_points_against` | REAL | |
| `made_playoffs` | BOOLEAN | |
| `playoff_finish` | INTEGER | 1=champ, 2=runner-up, 3=3rd place, etc. |
| `team_avatar_asset_id` | INTEGER FK → assets | Team logo as it appeared THAT season; NULL until the avatar backfill runs |
| `owner_avatar_asset_id` | INTEGER FK → assets | Owner avatar that season; NULL (NFL.com renders only a team logo per row today) |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`season_id`, `team_name`). `owner_id` is intentionally not unique within
a season because historical/manual identity cleanup can prove that one person
managed multiple NFL.com teams in the same year.

### `scoring_rules`
One row per (season, rule). Scraped from the league settings page each season.

| Column | Type | Notes |
|--------|------|-------|
| `rule_id` | INTEGER PK AUTOINCREMENT | |
| `season_id` | INTEGER FK → seasons | |
| `category` | TEXT | `'passing'`, `'rushing'`, `'receiving'`, `'kicking'`, `'defense_st'`, `'misc'` |
| `stat_key` | TEXT | `'passing_yards'`, `'passing_tds'`, `'rushing_yards_per_unit'`, etc. — see `05_SCORING_ENGINE.md` for the full list |
| `points_per_unit` | REAL | E.g., 0.04 for points per passing yard |
| `unit_size` | REAL | E.g., 1 = per yard, 10 = per 10 yards |
| `threshold_min` | REAL | For bonus rules: only counts above this value |
| `threshold_max` | REAL | NULL for no upper bound |
| `flat_points` | REAL | For all-or-nothing rules (e.g., 300+ yard bonus = 3 points flat) |
| `raw_text` | TEXT | The actual text scraped from NFL.com, for human audit |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`season_id`, `category`, `stat_key`, `threshold_min`)

### `players`
One row per NFL player relevant to this league.

**Scope.** nflverse's `load_players` returns the entire NFL player universe back to 1999 (and older) — every IDP, lineman, and long-snapper, plus everyone who retired before the league existed. We don't keep all of it. Ingestion filters `load_players` metadata to (a) positions the league can roster (`RELEVANT_POSITIONS`, default `QB,RB,WR,TE,K`; team DEF is synthesized) and (b) players whose career overlaps the league era (`last_season >= LEAGUE_START_YEAR`). The same `RELEVANT_POSITIONS` gate applies to the *stub* path: nflverse's weekly stats file carries a line for every IDP and lineman, so an unknown gsis_id with an irrelevant position is no longer stubbed in (its stat row then resolves to no player and is skipped). A player already known to the DB (e.g. one NFL.com rostered) keeps all of its stat rows regardless of any single stat line's position label, so nothing rosterable is ever dropped.

The `ff-pipeline prune-players` command cleans rows that predate these filters, in two passes:

- **Irrelevant position** — players whose `position` is outside `RELEVANT_POSITIONS` *and* that no real-league table references (`team_rosters`, `transactions`, `player_availability`, `player_id_overrides`). Their incidental rows (`player_stats_raw`, `player_stats_scored`, `projections`, `trending_players`) are cascade-deleted. Position labels are unreliable — NFL.com tags team defenses with a scrape artifact, fullbacks get rostered at flex, two-way players carry a defensive label — so a roster/transaction/availability/override row is treated as ground truth and **protects** the player from this pass no matter what its position string says.
- **Fully orphaned** — players referenced by no other table at all (e.g. pre-league-era skill players).

Both passes preview first (`--dry-run` shows the position breakdown and the cascade blast radius) and take a timestamped backup before any delete.

**League relevance vs. NFL facts.** Two different questions get asked of a player row, and they have two different answer columns. *Is this player currently an NFL thing?* is a **current-NFL fact** sourced from nflverse — `is_active`, `nfl_team`, `last_season`. *Was this player ever part of THIS league?* is a **historical league fact** derived from `team_rosters` — the `first_rostered_season` / `last_rostered_season` span. These do not agree, and shouldn't: nflverse ships the entire NFL universe, so thousands of rows are current-NFL-active but never touched this league (the "ghost" players). A consumer that wants "players in this league's history" must filter on a non-NULL rostered span (`league_relevant` on the players API), **not** on `is_active`. A consumer that wants an active/retired-in-league badge should read the rostered span, not `is_active`.

- `is_active` is the **raw nflverse status snapshot** as of the last metadata crawl (`status == 'ACT'`, or unknown/`NULL` status treated as active). It is deliberately *not* overloaded into a league-relevance signal — a player can be NFL-active yet never have been in this league, or league-historical yet now NFL-retired. Treat it as "nflverse's current view of the player," nothing more. (Historical reason it reads as unreliable in a league index: the `status is None → active` fallback, plus `nfl_team` being a single mutable "latest team" that nflverse keeps populated even for retired players.)
- `last_season` is likewise a current-NFL fact (the last NFL season nflverse saw the player). It powers the ingestion **era filter** (drop metadata for players whose career ended before `LEAGUE_START_YEAR`). It is NULL only for rows nflverse can't identify — players first seen on NFL.com with no `gsis_id`, team-DEF rows (which are synthetic and have no nflverse player record), and rare stale `gsis_id`s no longer returned by `load_players()`. That NULL is an honest source gap, not a population bug.
- `first_rostered_season` / `last_rostered_season` are **materialized** from `team_rosters` (`MIN`/`MAX` `season_year`; NULL ⇒ never rostered here). They are recomputed at the end of every NFL.com roster sync (`recompute_rostered_spans`) and backfilled by their migration, so a fresh DB and an incrementally-synced DB agree.

**Operational audit (2026-06-07, `data/fantasy.db`).** The D1/D2 refresh path is
idempotent and already populated every player that current nflverse can match:
`scripts/refresh_player_metadata.py` dry-run matched 2,771 existing `gsis_id`s,
updated through `_upsert_players`, inserted 0 rows, and left the populated
`last_season` count unchanged at 2,771 / 3,048. The remaining 277
`last_season IS NULL` rows break down as 276 rows with no `gsis_id` plus one
stale `gsis_id` (`M. Wilson`, `00-0034703`) absent from current
`load_players()`. Among league-rostered players, the 38 `rookie_year IS NULL`
rows are 32 synthetic team DEF rows, 5 NFL.com-only historical aliases, and the
same stale `M. Wilson` nflverse miss. Never-rostered / never-scored ghost rows
are currently 400 (242 active per raw nflverse status, 158 inactive). Duplicate
same-player / same-season / same-week roster rows are 0.

| Column | Type | Notes |
|--------|------|-------|
| `player_id` | INTEGER PK AUTOINCREMENT | Internal stable ID |
| `name_full` | TEXT NOT NULL | |
| `name_first` | TEXT | |
| `name_last` | TEXT | |
| `position` | TEXT | `'QB'`, `'RB'`, `'WR'`, `'TE'`, `'K'`, `'DEF'` |
| `nfl_team` | TEXT | nflverse "latest team" (current-NFL fact); 3-letter abbrev. Stale for retired players — see note above |
| `birth_date` | DATE | When available |
| `rookie_year` | INTEGER | nflverse `rookie_season`. NULL for non-nflverse rows (NFL.com-only stubs) and team-DEF rows |
| `last_season` | INTEGER | Last NFL season the player appeared in (nflverse `last_season`); a current-NFL fact, used to scope ingestion to the league era. NULL ⇒ player not identifiable in nflverse |
| `is_active` | BOOLEAN | **Raw nflverse status snapshot, NOT league relevance** — see note above |
| `first_rostered_season` | INTEGER | First season rostered in THIS league (`MIN(team_rosters.season_year)`). NULL ⇒ never rostered here. The canonical league-relevance signal |
| `last_rostered_season` | INTEGER | Last season rostered in THIS league (`MAX(team_rosters.season_year)`). NULL ⇒ never rostered here |
| `nfl_com_player_id` | TEXT | ID used in NFL.com URLs |
| `gsis_id` | TEXT | Canonical NFL ID, used by nflverse |
| `sleeper_id` | TEXT | |
| `espn_id` | TEXT | (for future-proofing) |
| `yahoo_id` | TEXT | (for future-proofing) |
| `created_at`, `updated_at` | TIMESTAMP | |

INDEX on `gsis_id`, `sleeper_id`, `nfl_com_player_id` (join columns)

**Querying league-relevant players.** The read API exposes this as an additive
filter rather than making callers join: `GET /players?league_relevant=true`
returns only players with a non-NULL rostered span, `=false` returns only the
never-rostered ghosts, and omitting it returns everything. `PlayerOut` carries
`last_season`, `first_rostered_season`, and `last_rostered_season` so a client
can render "rostered 2012-2018" and a league active/retired badge with no
business logic of its own.

**Refreshing nflverse metadata.** `last_season` (and the other nflverse fields)
are kept current by any `ff-pipeline run --source nflverse`. To refresh metadata
on the players already in the DB *without* re-ingesting a season's weekly stats
or regrowing the ghost set, run `scripts/refresh_player_metadata.py` (dry-run by
default; `--apply` commits after a backup). It re-runs the production upsert path
restricted to existing `gsis_id`s, then recomputes the rostered spans. It does
not fabricate values for NFL.com-only rows, team DEF rows, or stale `gsis_id`s
that nflverse no longer returns.

### `team_rosters`
A **game-time snapshot** of a team's roster. Captured once per week, at the moment NFL.com locks rosters for game day (typically Sunday 12:55 PM ET for most slots; Thursday 8:15 PM ET for the TNF slot if pulled forward). This is the authoritative record of "who was on whose team when the game started."

For mid-week roster moves (Tuesday waivers, Wednesday free-agent adds, etc.), the most recent state before the next NFL game kicks off is what gets persisted as that week's snapshot. The full transaction trail lives in `transactions` — `team_rosters` is the materialized point-in-time view.

| Column | Type | Notes |
|--------|------|-------|
| `roster_id` | INTEGER PK AUTOINCREMENT | |
| `team_id` | INTEGER FK → teams | |
| `player_id` | INTEGER FK → players | |
| `season_year` | INTEGER | denormalized for query speed |
| `week` | INTEGER | 1-18 (regular season), 19-22 (playoffs), 0 (post-draft, pre-week-1) |
| `roster_slot` | TEXT | `'QB'`, `'RB1'`, `'WR2'`, `'FLEX'`, `'BN1'`, `'IR'`, etc. |
| `is_starter` | BOOLEAN | True for all non-BN, non-IR slots |
| `was_locked_at_kickoff` | BOOLEAN | True if NFL.com had locked the slot when game started |
| `acquisition_type` | TEXT | `'draft'`, `'waiver'`, `'free_agent'`, `'trade'`, `'kept'` |
| `acquisition_week` | INTEGER | The week they joined the team (may be < this row's week) |
| `acquisition_date` | TIMESTAMP | The exact NFL.com timestamp of their acquisition by this team |
| `drop_date` | TIMESTAMP | If dropped from this team by end of this week; NULL otherwise |
| `extra_data` | TEXT (JSON) | Opportunistic fields (e.g., waiver bid amount, pre-locked projected points) |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`season_year`, `week`, `player_id`)
INDEX(`team_id`, `week`), INDEX(`player_id`, `season_year`), INDEX(`player_id`, `acquisition_date`)

**Idempotency / the "one owner per week" invariant.** The unique key is keyed on
`(season_year, week, player_id)` — deliberately *without* `team_id` — so the DB
itself enforces that a player belongs to at most **one** team in a given scoring
week. The loader is idempotent in two complementary ways: it **replaces per
scope** (deletes a team's existing rows for the `(season, week)` before writing
the fresh snapshot, so a re-ingest yields exactly one snapshot and players
dropped between snapshots don't linger), and it **upserts on the cross-team
key** (a player who changed teams mid-week conflicts with his stale row on the
*old* team and is moved, not double-rostered). Re-running a week is therefore
safe and the `same player on two teams in one week` query returns zero rows.

### `player_availability`
**Every player in the league universe**, with their availability state at game time of every week. This is the league-wide companion to `team_rosters` (which only covers rostered players). With this table, you can answer "who was available on waivers in week 5?", "when did this player first become a free agent?", etc.

A new row exists for every (player, season, week) where the player is in the league's "universe" — meaning they're either currently rostered, were ever rostered this season, or are an active NFL player nflverse knows about. We do not row-explode for every NFL player every week (would be millions of rows); we focus on players who could conceivably matter to the league.

| Column | Type | Notes |
|--------|------|-------|
| `availability_id` | INTEGER PK AUTOINCREMENT | |
| `player_id` | INTEGER FK → players | |
| `season_year` | INTEGER | |
| `week` | INTEGER | |
| `status` | TEXT | `'OWNED'`, `'FREE_AGENT'`, `'ON_WAIVERS'`, `'NOT_IN_LEAGUE'` |
| `owning_team_id` | INTEGER FK → teams | NULL unless status='OWNED' |
| `waiver_claim_deadline` | TIMESTAMP | NULL unless status='ON_WAIVERS'; when waivers clear |
| `last_status_change` | TIMESTAMP | When the status last transitioned |
| `is_pre_kickoff_snapshot` | BOOLEAN | True if this is the canonical "at game time" row for the week |
| `extra_data` | TEXT (JSON) | Opportunistic fields |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`player_id`, `season_year`, `week`, `is_pre_kickoff_snapshot`)
INDEX(`season_year`, `week`, `status`)
INDEX(`owning_team_id`, `week`)

The pipeline writes one canonical "pre-kickoff" row per (player, week) per season — that's the one with `is_pre_kickoff_snapshot=True`. Mid-week observations (e.g., a player gets dropped Tuesday morning and re-added Thursday) generate additional rows with `is_pre_kickoff_snapshot=False`, providing a fine-grained audit trail when needed.

### `matchups`
One row per (season, week, team). Two rows make a single head-to-head game.

| Column | Type | Notes |
|--------|------|-------|
| `matchup_id` | INTEGER PK AUTOINCREMENT | |
| `season_id` | INTEGER FK → seasons | |
| `week` | INTEGER | |
| `team_id` | INTEGER FK → teams | |
| `opponent_team_id` | INTEGER FK → teams | NULL if bye |
| `team_score` | REAL | Total fantasy points scored by this team |
| `opponent_score` | REAL | Cached for query convenience |
| `is_win` | BOOLEAN | NULL if not yet completed |
| `is_playoff` | BOOLEAN | |
| `is_consolation` | BOOLEAN | For losers-bracket games; history reconstruction derives this from the NFL.com playoff bracket's championship-team set |
| `nfl_com_game_id` | TEXT | The `gameId` in NFL.com URLs — for re-fetching |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`season_id`, `week`, `team_id`)

### `transactions`
The full chronological league diary: trades, waivers, free-agent adds, drops, IR placements — **and** lineup/start-sit moves and commissioner/league-setting changes. The whole season log is swept (every paginated page), not just the most-recent page.

| Column | Type | Notes |
|--------|------|-------|
| `transaction_id` | INTEGER PK AUTOINCREMENT | |
| `season_id` | INTEGER FK → seasons | |
| `transaction_type` | TEXT | `'draft'`, `'trade'`, `'waiver_add'`, `'free_agent_add'`, `'drop'`, `'ir_placement'`, `'ir_activation'`, `'lineup_change'`, `'setting_change'` |
| `executed_at` | TIMESTAMP | When NFL.com recorded it |
| `effective_week` | INTEGER | Week the move takes effect |
| `team_id` | INTEGER FK → teams | The team this row affects (NULL for `lineup_change`/`setting_change` — NFL.com renders no team anchor on those rows) |
| `counterpart_team_id` | INTEGER FK → teams | For trades; NULL otherwise |
| `player_id` | INTEGER FK → players | |
| `direction` | TEXT | `'in'`/`'out'` — relative to `team_id`; for `lineup_change`, `'in'`=started, `'out'`=benched |
| `waiver_priority_used` | INTEGER | For waivers, the priority slot consumed |
| `notes` | TEXT | Free-text scraped from NFL.com (e.g., the "By" owner) |
| `extra_data` | TEXT (JSON) | Payload for rows that don't fit the player-move columns: `lineup_change` carries `{"from_slot","to_slot"}`; `setting_change` carries the change detail. NULL for add/drop/trade |
| `created_at`, `updated_at` | TIMESTAMP | |

INDEX(`season_id`, `team_id`), INDEX(`season_id`, `player_id`)

A trade between teams A and B involving 2 players generates 4 rows (one per player per team). Cross-page trade legs are stitched into `counterpart_team_id` after the sweep.

### `assets`
Content-addressed binary blobs — team logos / owner avatars downloaded from NFL.com. Raw bytes live on disk under the assets root (`data/assets/<sha[:2]>/<sha>.<ext>`, gitignored); only metadata lives here so the SQLite file stays small and ports cleanly to Postgres. Identical default avatars across teams dedupe to one row via UNIQUE `sha256`.

| Column | Type | Notes |
|--------|------|-------|
| `asset_id` | INTEGER PK AUTOINCREMENT | |
| `league_id` | TEXT FK → leagues | NULL allowed |
| `kind` | TEXT | `'team_avatar'` \| `'user_avatar'` |
| `source_url` | TEXT | Original NFL.com CDN URL (first one seen for these bytes) |
| `sha256` | TEXT | Content hash; **UNIQUE** (dedup key) |
| `content_type` | TEXT | From the download response |
| `byte_size` | INTEGER | |
| `storage_path` | TEXT | Path **relative to the assets root** |
| `fetched_at` | TIMESTAMP | When the bytes were downloaded |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`sha256`), INDEX(`league_id`). Bytes are streamed by the read API at `GET /assets/{asset_id}`.

### `player_stats_raw`
The atomic stat record. **One row per (player, season, week, source)**. Multiple rows can exist for the same (player, season, week) — one per source. The normalizer chooses which source's row feeds the scoring engine; the others are preserved for audit and cross-check.

| Column | Type | Notes |
|--------|------|-------|
| `stat_id` | INTEGER PK AUTOINCREMENT | |
| `player_id` | INTEGER FK → players | |
| `season_year` | INTEGER | |
| `week` | INTEGER | |
| `season_type` | TEXT | `'REG'`, `'POST'`, `'PRE'` |
| `nfl_team` | TEXT | The player's own NFL team that week, season-correct (a 2015 Raider reads `OAK`, not `LV`). The per-season counterpart to `players.nfl_team`'s single current snapshot. Populated from nflverse's per-week `team`; NULL for sources that omit it. Resolve a player-season's team of record via `repository.queries.player_season_teams` (modal team, tie-broken by latest week) |
| `nfl_opponent` | TEXT | 3-letter abbrev |
| `source` | TEXT | `'nflverse'`, `'nfl_com_api'`, `'sleeper'`, `'nfl_com_league'` |
| `stats` | TEXT (JSON) | The raw stat dict — passing_yards, completions, etc. |
| `is_primary` | BOOLEAN | True if this is the source feeding the scoring engine |
| `ingested_at` | TIMESTAMP | |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`player_id`, `season_year`, `week`, `source`)
INDEX(`season_year`, `week`)

### `player_stats_scored`
The output of the scoring engine. One row per (player, season, week, scoring_rule_set). When scoring rules change between seasons, each season's stats are scored separately with the right rules.

| Column | Type | Notes |
|--------|------|-------|
| `scored_id` | INTEGER PK AUTOINCREMENT | |
| `stat_id` | INTEGER FK → player_stats_raw | Which raw row this was computed from |
| `season_id` | INTEGER FK → seasons | Which season's rules were used |
| `player_id` | INTEGER FK → players | denormalized for query speed |
| `week` | INTEGER | denormalized for query speed |
| `total_points` | REAL | The headline number |
| `points_breakdown` | TEXT (JSON) | `{"passing": 12.4, "rushing": 0, "receiving": 8.2, "bonus": 3.0, ...}` |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`stat_id`, `season_id`)

### `projections`
Projected stats from external sources, scored using league rules.

| Column | Type | Notes |
|--------|------|-------|
| `projection_id` | INTEGER PK AUTOINCREMENT | |
| `player_id` | INTEGER FK → players | |
| `season_year` | INTEGER | |
| `week` | INTEGER | |
| `source` | TEXT | `'sleeper'`, `'nfl_com'` |
| `projected_stats` | TEXT (JSON) | Same shape as `player_stats_raw.stats` |
| `projected_points` | REAL | Already-scored by league rules |
| `fetched_at` | TIMESTAMP | |
| `created_at`, `updated_at` | TIMESTAMP | |

UNIQUE(`player_id`, `season_year`, `week`, `source`, `fetched_at`)
INDEX(`season_year`, `week`)

Projections are append-only (`fetched_at` is part of the unique key) so we can analyze how projections changed over the course of a week.

### `pipeline_runs`
Observability table — one row per `ff-pipeline run` invocation.

| Column | Type | Notes |
|--------|------|-------|
| `run_id` | INTEGER PK AUTOINCREMENT | |
| `started_at` | TIMESTAMP NOT NULL | |
| `finished_at` | TIMESTAMP | NULL if still running |
| `status` | TEXT | `'running'`, `'success'`, `'partial_success'`, `'failed'` |
| `mode` | TEXT | `'full_sync'`, `'incremental'`, `'backfill'`, `'cookie_test'` |
| `sources_summary` | TEXT (JSON) | `{"nflverse": {"rows_added": 234, "errors": 0}, "nfl_com": {...}}` |
| `error_summary` | TEXT | Top-level error if status is failed |
| `created_at`, `updated_at` | TIMESTAMP | |

### `source_health`
One row per (run, source) — lightweight per-source health record.

| Column | Type | Notes |
|--------|------|-------|
| `health_id` | INTEGER PK AUTOINCREMENT | |
| `run_id` | INTEGER FK → pipeline_runs | |
| `source` | TEXT | |
| `status` | TEXT | `'success'`, `'failed'`, `'skipped'`, `'auth_failure'` |
| `rows_added` | INTEGER | |
| `rows_updated` | INTEGER | |
| `parse_failures` | INTEGER | |
| `error_message` | TEXT | NULL on success |
| `duration_ms` | INTEGER | |
| `created_at` | TIMESTAMP | |

---

## Notable design decisions

### Why separate `player_stats_raw` and `player_stats_scored`

Scoring rules are mutable. If your league changes from 0.5 PPR to full PPR, you don't want to re-fetch every historical stat from nflverse — you just want to re-score. By keeping raw and scored separate, recomputation is a single SQL query.

### Why store full breakdown in `points_breakdown` JSON

For Phase 2's dashboard. When a user clicks a player's box score, they want to see "8.4 passing + 3.2 rushing + 2.0 TD bonus = 13.6". The breakdown is the data behind that view.

### Why `season_type = 'REG'/'POST'/'PRE'`

nflverse uses this convention. We mirror it. Postseason stats matter for some leagues that include playoff weeks 19+.

### Why owners are separate from teams

Because they survive across seasons. The dashboard's "owner stats" page (Phase 2) shows per-owner aggregates across all seasons they participated in. That requires owner identity to be persistent.

### Why we don't have a `draft_picks` table

Draft picks ARE just `transactions` with `transaction_type = 'draft'`. They have an `effective_week = 0` and an `acquisition_type = 'draft'` on the resulting `team_rosters` row.

### Migrations from day 1

The first alembic migration creates this schema. Every change goes through a new migration file. This is overkill for a single-user system, but trivial to set up and saves enormous pain later — particularly when Phase 2 starts wanting schema tweaks.
