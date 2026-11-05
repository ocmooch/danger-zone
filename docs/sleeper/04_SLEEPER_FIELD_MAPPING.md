# 04 — Sleeper → Canonical Field Mapping

**Status:** draft — **must be confirmed against live payloads in S2** · **Repo:** `danger-zone`
**Used by:** `normalizer/sleeper_league.py`, `normalizer/slots.py`, `scoring/sleeper_rules.py`

Sleeper's public API is read-only, unauthenticated and only partly documented. Shapes below come
from the documented API plus well-known undocumented fields. Every row marked **⚑ verify** must be
checked against the real league's payloads during S2, and this file updated, before S5 builds on it.

**Verified against the live league on 2026-11-05** (`/league`, `/users`, `/rosters` only):
`previous_league_id = "0"` (no earlier Sleeper season); `start_week = 1`, `playoff_week_start = 15`
(→ 14 regular-season weeks), `playoff_teams = 6`; `waiver_type = 2` with `waiver_budget = 100`
(FAAB); `divisions = null`; `type = 0` (redraft); `roster_positions` = `QB, RB, RB, WR, WR, TE,
FLEX, K, DEF, BN×6` (no IR or taxi); no co-owners; one user has no `metadata.team_name` (fallback
needed); the league owner (`is_owner = true`) is a long-time manager. Every other ⚑ is still open.

---

## 1. Endpoints consumed

Base `https://api.sleeper.app/v1` unless noted. All responses are archived raw (`01` §4).

| Endpoint | Cadence | Feeds |
|----------|---------|-------|
| `GET /state/nfl` | every run | current season/week, `season_type` |
| `GET /league/{league_id}` | every run | season settings, status, scoring, roster positions, lineage (`previous_league_id`), `draft_id` |
| `GET /league/{league_id}/users` | every run | owner identity, team names, avatars, commissioner flag |
| `GET /league/{league_id}/rosters` | every run | team ↔ owner, records, FAAB used, ppts, division, co-owners, current roster |
| `GET /league/{league_id}/matchups/{week}` | weeks 1..current | matchups, starters, per-player points, custom points |
| `GET /league/{league_id}/transactions/{round}` | rounds 1..current (round ≈ week) | adds/drops/waivers/trades/commissioner moves, failed claims |
| `GET /league/{league_id}/traded_picks` | every run | future pick ownership |
| `GET /league/{league_id}/winners_bracket`, `/losers_bracket` | from playoff start | official brackets |
| `GET /league/{league_id}/drafts`, `GET /draft/{draft_id}`, `/draft/{draft_id}/picks` | pre-season; once after draft | draft type/order, picks, keeper/auction info |
| `GET /user/{user_id}/leagues/nfl/{season}` | rollover only | discover the renewed league |
| `GET /players/nfl` | ≤ daily | player directory, status snapshot (existing client) |
| `https://sleepercdn.com/avatars/{avatar_id}` (+ `/thumbs/`) | on change | avatar bytes → `assets` |

Not available publicly (therefore capability gaps, not bugs): league chat, polls, news text,
lineup-change history, league-settings change log.

## 2. League / season

| Sleeper field | Canonical target | Notes |
|---------------|------------------|-------|
| `league_id` | `seasons.platform_league_id` | new id every season |
| `season` | `seasons.year` | string → int |
| `previous_league_id` | lineage only | `lineage.py` |
| `name` | `leagues.name` only if user approves | the franchise name stays "The Danger Zone" unless changed (D-4) |
| `status` | `seasons.status` | `pre_draft`/`drafting` → `pre_draft`; `in_season` → `in_progress`; `complete` → `completed` |
| `settings.playoff_week_start` | `seasons.regular_season_weeks = playoff_week_start - start_week` | ⚑ verify `start_week` |
| `settings.playoff_teams`, `playoff_round_type`, `playoff_type` | `seasons.playoff_weeks` (derived) + `settings_snapshot` | rounds = ceil(log2(playoff_teams)), adjusted for byes, ⚑ verify |
| `settings.waiver_type`, `waiver_budget`, `trade_deadline`, `divisions`, `reserve_slots`, `taxi_slots`, `type` (redraft/keeper/dynasty), `best_ball` | `settings_snapshot` | `waiver_type = 2` ⇒ FAAB ⚑ verify |
| `roster_positions[]` | `settings_snapshot`; drives slot mapping §4 | |
| `scoring_settings{}` | `scoring_rules` via §5; raw in `settings_snapshot` | |
| `metadata.division_<n>` / `division_<n>_avatar` | `teams.division` name lookup | ⚑ verify keys |
| `draft_id` | `seasons.platform_draft_id` | |
| `avatar` | `assets` (`kind='league_avatar'`) | new asset kind, additive |

## 3. Users, rosters → owners, teams

| Sleeper field | Canonical target | Notes |
|---------------|------------------|-------|
| `users[].user_id` | `owner_platform_identities.external_user_id` → `owner_id` | manual crosswalk (D-3) |
| `users[].display_name` | `owner_platform_identities.external_display_name` | **not** written over `owners.display_name` |
| `users[].metadata.team_name` | `teams.team_name` | fallback `"<display_name>'s Team"` |
| `users[].avatar` / `metadata.avatar` | `owner_avatar_asset_id` / `team_avatar_asset_id` | downloaded upstream; dashboard never calls Sleeper CDN |
| `users[].is_owner` | `commissioners` (new term row if changed) | ⚑ verify semantics (league owner = commissioner) |
| `rosters[].roster_id` | `teams.platform_team_key = 'sleeper:roster:<id>'` | stable within a league; ⚑ verify stability across renewals |
| `rosters[].owner_id` | `teams.owner_id` via crosswalk | |
| `rosters[].co_owners[]` | `teams.extra_data.co_owner_user_ids` | |
| `settings.wins/losses/ties` | `teams.regular_season_wins/losses/ties` | cross-checked against matchups |
| `settings.fpts` + `fpts_decimal/100` | `teams.regular_season_points_for` | combine integer + decimal parts |
| `settings.fpts_against` + `fpts_against_decimal/100` | `teams.regular_season_points_against` | |
| `settings.ppts` + `ppts_decimal/100` | `teams.extra_data.ppts` | official potential points |
| `settings.waiver_position`, `waiver_budget_used`, `total_moves`, `division` | `teams.extra_data.*`, `teams.division` | |
| `metadata.streak`, `metadata.record` | `teams.extra_data.streak`, `record_string` | ⚑ verify |
| `players[]`, `starters[]`, `reserve[]`, `taxi[]` | current roster only (week snapshots come from matchups) | |
| `keepers[]` | `team_rosters.acquisition_type = 'kept'` at week 0 | |

Season results (`final_rank`, `playoff_finish`, `made_playoffs`, `seasons.champion_team_id`,
`runner_up_team_id`, `last_place_team_id`) are derived from brackets (§7), never from W-L order.

## 4. Matchups → `matchups`, `team_rosters`

Each matchup payload row is one team-week:

| Sleeper field | Canonical target | Notes |
|---------------|------------------|-------|
| `roster_id` | `matchups.team_id` via `platform_team_key` | |
| `matchup_id` | pair two rows → `opponent_team_id`; `platform_matchup_key` | `null` ⇒ no game (bye/eliminated) → `opponent_team_id NULL` |
| `points` | `matchups.team_score` (and `opponent_score` on the pair) | |
| `custom_points` | if non-null: `team_score = custom_points`; `extra_data.points_official`, `custom_points` | commissioner override, flagged |
| `starters[]` + `roster_positions` | `team_rosters.roster_slot` via §4.1, `is_starter = true` | order of `starters` aligns with non-bench positions; `"0"` = empty slot |
| `players[]` minus starters | `roster_slot = 'BN'` (or `RES` / `TAXI` from roster reserve/taxi lists) | |
| `players_points{id: pts}` | `team_rosters.extra_data.platform_points` | official per-player points |
| `starters_points[]` | cross-check only | |

Snapshots are written as `snapshot_kind = 'sleeper_matchup'`, `was_locked_at_kickoff = true` for
completed weeks. Same per-(team, week) replace + cross-team upsert invariant as NFL.com.

### 4.1 Roster slot taxonomy (D-9)

| Sleeper label | Stored `roster_slot` | `is_starter` |
|---------------|----------------------|--------------|
| `QB` `RB` `WR` `TE` `K` `DEF` | same | yes |
| `FLEX` (RB/WR/TE) | `R/W/T` | yes |
| `WRRB_FLEX` | `W/R` (already used by NFL.com seasons) | yes |
| `REC_FLEX` | `W/T` *(new)* | yes |
| `SUPER_FLEX` | `Q/R/W/T` *(new)* | yes |
| `IDP_FLEX`, `DL`, `LB`, `DB` | same *(new)* | yes |
| `BN` | `BN` | no |
| reserve (IR) | `RES` | no |
| taxi | `TAXI` *(new)* | no |

The raw label always goes into `extra_data.platform_slot`. Legacy rows are unaffected.

## 5. Scoring settings → `scoring_rules`

Shape: `(category, stat_key, points_per_unit, unit_size, threshold_min, threshold_max, flat_points)`.
The engine computes `(value / unit_size) * points_per_unit` (continuous, no flooring), so NFL.com
"1 point per 25 passing yards" (`ppu=1, unit=25`) and Sleeper `pass_yd: 0.04` (`ppu=0.04, unit=1`)
score identically.

| Sleeper key | Category / stat_key | Mapping |
|-------------|---------------------|---------|
| `pass_yd` | passing / `passing_yards` | `ppu = v, unit = 1` |
| `pass_td` | passing / `passing_tds` | `ppu = v` |
| `pass_int` | passing / `passing_interceptions` | `ppu = v` |
| `pass_2pt` | passing / `passing_2pt_conversions` | |
| `bonus_pass_yd_300`, `bonus_pass_yd_400` | passing / `passing_yards` threshold rows | `threshold_min = 300/400, flat = v` — **⚑ verify cumulative vs tiered**: Sleeper bonuses stack; NFL.com 2025 rows (`1.0` @300, `3.0` @400) must be checked for whether 400+ yields 3 or 4 |
| `pass_td_40p`, `pass_td_50p` | passing / `passing_yards_bonus_long_td_40/50` | ⚑ verify stacking |
| `rush_yd`, `rush_td`, `rush_2pt`, `bonus_rush_yd_100/200`, `rush_td_40p/50p` | rushing / … | as passing |
| `rec`, `rec_yd`, `rec_td`, `rec_2pt`, `bonus_rec_yd_100/200`, `rec_td_40p/50p` | receiving / … | `rec` → `receptions` |
| `fum_lost` | misc / `fumbles_lost` | |
| `st_td`, `kr_td`, `pr_td` | misc / `special_teams_tds` | ⚑ verify which keys the league uses |
| `xpm` | kicking / `extra_point_made` | |
| `fgm_0_19` … `fgm_50p` | kicking / `field_goal_made_<range>` | ⚑ verify `fgm_50_59` / `fgm_60p` split vs `50_plus` |
| `fgmiss*`, `xpmiss` | kicking / new stat keys | only if non-zero |
| `def_td`, `int`, `fum_rec`, `sack`, `safe`, `blk_kick`, `def_st_td` | defense / … | |
| `pts_allow_0`, `pts_allow_1_6`, `pts_allow_7_13`, `pts_allow_14_20`, `pts_allow_21_27`, `pts_allow_28_34`, `pts_allow_35p` | defense / `points_allowed` bracket rows | `threshold_min` = lower bound, `flat = v` — **⚑ verify Sleeper's points-allowed definition (defense-only vs total)**, a known divergence class in DST reconciliation |
| `yds_allow_0_100` … `yds_allow_550p` | defense / `total_yards_allowed` bracket rows | Sleeper has more brackets than NFL.com — map each |
| any other key with `v != 0` | — | **abort**: list for the user |

The derived 2026 set is diffed against 2025 and presented for sign-off (D-10). Keys with `v == 0`
are skipped.

## 6. Transactions → `transactions`

Sleeper groups one move as one transaction with `adds{player: roster}` / `drops{player: roster}`.
Each player-team leg becomes one canonical row, as on NFL.com.

| Sleeper `type` / `status` | Canonical rows |
|---------------------------|----------------|
| `waiver`, `complete` | `waiver_add` (in) + `drop` (out) per dropped player; `extra_data.faab_bid = settings.waiver_bid` |
| `waiver`, `failed` | `waiver_add` with `status='failed'`, `extra_data.faab_bid`, `extra_data.failure_reason = metadata.notes` *(new capability)* |
| `free_agent` | `free_agent_add` / `drop` |
| `trade` | `trade` legs, `direction` in/out, `counterpart_team_id`; `extra_data.picks[]` from `draft_picks`, `extra_data.faab_transfers[]` from `waiver_budget` |
| `commissioner` | `commissioner_move` *(new type)* legs; `notes` = "commissioner" |
| draft picks (`/draft/{id}/picks`) | `draft` rows, `effective_week = 0`; `notes = "Round R, Pick P (overall O)"` (legacy format); `extra_data`: `draft_round`, `draft_pick_in_round`, `draft_overall`, `is_keeper`, `auction_amount` |

| Sleeper field | Canonical |
|---------------|-----------|
| `transaction_id` | `platform_transaction_id` |
| `created` (ms epoch) | `executed_at` (UTC) |
| `leg` (week) | `effective_week` |
| `creator` | `notes` "By <owner display>" via crosswalk |
| `consenter_ids` | `extra_data.consenters` |

Draft board metadata (type snake/auction/linear, `draft_order`, `slot_to_roster_id`, rounds) goes
into `seasons.settings_snapshot.draft` and is mirrored to `teams.draft_position`.

Not available: lineup changes and setting changes → no rows; the capability matrix reports
`not_on_platform`.

## 7. Brackets → `playoff_brackets`, `matchups`, `teams`, `seasons`

Bracket match objects: `r` (round), `m` (match id), `t1`, `t2` (roster ids), `w`, `l`,
`t1_from`/`t2_from` (`{w: m}` or `{l: m}`), `p` (placement the match decides, e.g. 1, 3, 5).

| Derivation | Rule |
|------------|------|
| `playoff_brackets` row | one per (bracket, r, m) |
| `matchups.is_playoff` | team-week appears in winners bracket for that round |
| `matchups.is_consolation` | appears in losers bracket, or in a winners-bracket placement match with `p > 1` ⚑ verify league convention |
| `champion_team_id` / `runner_up_team_id` | winners bracket match with `p = 1`: `w` / `l` |
| `last_place_team_id` (the Sacko) | losers bracket deciding last place ⚑ verify `p` semantics in losers bracket (toilet-bowl direction) |
| `teams.playoff_finish` / `final_rank` | from `p` of each team's last match; teams without a placement keep NULL |

## 8. Players directory → `players`, `player_status_snapshots`

| Field | Target |
|-------|--------|
| `player_id` | `players.sleeper_id` (existing) |
| `gsis_id`, `espn_id`, `yahoo_id`, `rotowire_id`, `sportradar_id` | existing ID columns; extras → `players.extra_data.ids` |
| `years_exp`, `college`, `height`, `weight`, `fantasy_positions`, `birth_date` | `players.extra_data.profile` (`birth_date` only if NULL today) |
| `injury_status`, `injury_body_part`, `injury_start_date`, `injury_notes` | `player_status_snapshots` |
| `practice_participation`, `practice_description` | `player_status_snapshots` |
| `depth_chart_position`, `depth_chart_order`, `team`, `status` | `player_status_snapshots` |
| `news_updated` | `player_status_snapshots.extra` (timestamp only; no news text) |

nflverse remains the authority for `players.nfl_team`, `is_active`, `last_season` (existing
conflict policy in `03_DATA_SOURCES.md`); Sleeper values are snapshots, not overwrites.

## 9. NFL team codes

Sleeper uses `LAR`, `JAX`, `WAS`, `LV`, `LAC`; DEF player ids are team codes. Fold every code
through `nfl_teams.canonical_franchise` before matching — the same fix that resolved the DST
relocation join (`SD/OAK/STL` vs `LAC/LV/LA`).
