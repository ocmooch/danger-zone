# 04 — Sleeper → Canonical Field Mapping

**Status:** ⚑ items probed against live payloads in S2 (2026-11-09; §10) · **Repo:** `danger-zone`
**Used by:** `normalizer/sleeper_league.py`, `normalizer/slots.py`, `scoring/sleeper_rules.py`

Sleeper's public API is read-only, unauthenticated and only partly documented. Shapes below come
from the documented API plus well-known undocumented fields. Rows that were marked **⚑ verify** were
checked against the real league's payloads in S2; each is now ✅ (resolved) or ⏸ (deferred, with when).

**Verified against the live league on 2026-11-05** (`/league`, `/users`, `/rosters` only):
`previous_league_id = "0"` (no earlier Sleeper season); `start_week = 1`, `playoff_week_start = 15`
(→ 14 regular-season weeks), `playoff_teams = 6`; `waiver_type = 2` with `waiver_budget = 100`
(FAAB); `divisions = null`; `type = 0` (redraft); `roster_positions` = `QB, RB, RB, WR, WR, TE,
FLEX, K, DEF, BN×6` plus `reserve_slots = 2` (IR; no taxi); no co-owners; one user has no `metadata.team_name` (fallback
needed); the league owner (`is_owner = true`) is a long-time manager.

**S2 probe (2026-11-09, weeks 1–9, archive replay):** every ⚑ below is now marked ✅ resolved
or ⏸ deferred (with its milestone). Evidence and the re-runnable script: §10.

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
| `GET /league/{league_id}/winners_bracket`, `/losers_bracket` | every run (pre-generated before the playoffs; S2) | official brackets |
| `GET /league/{league_id}/drafts`, `GET /draft/{draft_id}`, `/draft/{draft_id}/picks` | every run (cheap; S2) | draft type/order, picks, keeper/auction info |
| `GET /user/{user_id}/leagues/nfl/{season}` | rollover only | discover the renewed league (not built in S2: at rollover, set `SLEEPER_LEAGUE_ID` to the new league; lineage finds the rest) |
| `GET /players/nfl` | ≤ daily | player directory, status snapshot (existing client) |
| `GET https://api.sleeper.com/stats/nfl/{season}/{week}?season_type=regular` | weeks 1..current (S2) | Sleeper's own per-player stats: the scoring input (§10) and D-10 evidence |
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
| `settings.playoff_week_start` | `seasons.regular_season_weeks = playoff_week_start - start_week` | ✅ S2: `start_week = 1`, `playoff_week_start = 15` → 14 |
| `settings.playoff_teams`, `playoff_round_type`, `playoff_type` | `seasons.playoff_weeks` (derived) + `settings_snapshot` | ✅ S2: derive from the bracket, not a formula — `playoff_teams = 6` gives 3 rounds (`r` 1–3, top two seeds bye in r1), one week per round (`playoff_round_type = 0`) → weeks 15–17. `playoff_type = 1` kept raw in `settings_snapshot` |
| `settings.waiver_type`, `waiver_budget`, `trade_deadline`, `divisions`, `reserve_slots`, `taxi_slots`, `type` (redraft/keeper/dynasty), `best_ball` | `settings_snapshot` | ✅ S2: `waiver_type = 2` is FAAB (`waiver_budget = 100`, 268 bids, all `int`). Also seen: `max_keepers = 1` but no keeper picks; `settings.draft_rounds = 3` is not the draft length (use `draft.settings.rounds = 15`) |
| `roster_positions[]` | `settings_snapshot`; drives slot mapping §4 | |
| `scoring_settings{}` | `scoring_rules` via §5; raw in `settings_snapshot` | |
| `metadata.division_<n>` / `division_<n>_avatar` | `teams.division` name lookup | ⏸ no divisions in 2026 (`settings.divisions` absent, no `division*` metadata keys); key format unverifiable until a season has divisions |
| `draft_id` | `seasons.platform_draft_id` | |
| `avatar` | `assets` (`kind='league_avatar'`) | new asset kind, additive |

## 3. Users, rosters → owners, teams

| Sleeper field | Canonical target | Notes |
|---------------|------------------|-------|
| `users[].user_id` | `owner_platform_identities.external_user_id` → `owner_id` | manual crosswalk (D-3) |
| `users[].display_name` | `owner_platform_identities.external_display_name` | **not** written over `owners.display_name` |
| `users[].metadata.team_name` | `teams.team_name` | fallback `"<display_name>'s Team"` |
| `users[].avatar` / `metadata.avatar` | `owner_avatar_asset_id` / `team_avatar_asset_id` | downloaded upstream; dashboard never calls Sleeper CDN |
| `users[].is_owner` | `commissioners` (new term row if changed) | ✅ S2: exactly one user `true`, the rest `null`; it is the only commissioner signal the API exposes |
| `rosters[].roster_id` | `teams.platform_team_key = 'sleeper:roster:<id>'` | stable within a league; ⏸ stability across renewals is unverifiable until the first renewal (2027 re-probe) |
| `rosters[].owner_id` | `teams.owner_id` via crosswalk | |
| `rosters[].co_owners[]` | `teams.extra_data.co_owner_user_ids` | |
| `settings.wins/losses/ties` | `teams.regular_season_wins/losses/ties` | cross-checked against matchups |
| `settings.fpts` + `fpts_decimal/100` | `teams.regular_season_points_for` | combine integer + decimal parts |
| `settings.fpts_against` + `fpts_against_decimal/100` | `teams.regular_season_points_against` | |
| `settings.ppts` + `ppts_decimal/100` | `teams.extra_data.ppts` | official potential points |
| `settings.waiver_position`, `waiver_budget_used`, `total_moves`, `division` | `teams.extra_data.*`, `teams.division` | |
| `metadata.streak`, `metadata.record` | `teams.extra_data.streak`, `record_string` | ✅ S2: `streak` = `<n><W\|L>` (e.g. `2W`); `record` = one `W`/`L` per scored week (length 8 after week 8; ties unseen) |
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
| `starters[]` + `roster_positions` | `team_rosters.roster_slot` via §4.1, `is_starter = true` | order of `starters` aligns with non-bench positions; `"0"` = empty slot (S2: 9 starters every team-week, no `"0"` seen yet) |
| `players[]` minus starters | `roster_slot = 'BN'` (or `RES` / `TAXI` from roster reserve/taxi lists) | S2: `players[]` **includes IR players** (sizes 15–21), but `rosters[].reserve` is current state only. ⏸ S5 must decide how past weeks' IR players are labeled (archive revisions of `rosters` give week-end snapshots from S2 on) |
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

✅ S2: the 2026 league uses only `QB RB×2 WR×2 TE FLEX K DEF BN×6` + 2 reserve (IR) slots; no
super-flex, IDP or taxi labels appear.

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
| `bonus_pass_yd_300`, `bonus_pass_yd_400` | passing / `passing_yards` threshold rows | `threshold_min = 300/400, flat = v` — ✅ S2: **tiered, not stacked**: every 400+ game (n=4) carries only `bonus_pass_yd_400` → +3, never +4. Rush/rec 100/200 assumed the same (no 200+ game yet; re-check) |
| `pass_td_40p`, `pass_td_50p` | passing / `passing_yards_bonus_long_td_40/50` | ✅ S2: **stacked**: every 50+ TD also counts as 40+ (pass/rush/rec, n=54) → +4 |
| `rush_yd`, `rush_td`, `rush_2pt`, `bonus_rush_yd_100/200`, `rush_td_40p/50p` | rushing / … | as passing |
| `rec`, `rec_yd`, `rec_td`, `rec_2pt`, `bonus_rec_yd_100/200`, `rec_td_40p/50p` | receiving / … | `rec` → `receptions` |
| `fum_lost` | misc / `fumbles_lost` | |
| `st_td`, `kr_td`, `pr_td` | misc / `special_teams_tds` | ✅ S2: league scores `st_td 6` (player) and `def_st_td 6` / `def_td 6` (DEF); `kr_td` / `pr_td` are 0 |
| `xpm` | kicking / `extra_point_made` | |
| `fgm_0_19` … `fgm_50p` | kicking / `field_goal_made_<range>` | ✅ S2: league uses generic `fgm 3` + `fgm_50_59 2` + `fgm_60p 2`; per-range keys and `fgm_50p` score 0. Every 50+ FG also counts in `fgm` (n=84) → 5 |
| `fgmiss*`, `xpmiss` | kicking / new stat keys | only if non-zero |
| `def_td`, `int`, `fum_rec`, `sack`, `safe`, `blk_kick`, `def_st_td` | defense / … | |
| `pts_allow_0`, `pts_allow_1_6`, `pts_allow_7_13`, `pts_allow_14_20`, `pts_allow_21_27`, `pts_allow_28_34`, `pts_allow_35p` | defense / `points_allowed` bracket rows | `threshold_min` = lower bound, `flat = v`; points-allowed definition (a known divergence class in DST reconciliation) ✅ S2: **defense-only**: `pts_allow` = opponent final score − 6 × opponent `def_td` − 2 × opponent `safe` (252/269 equal the final score; the other 17 are exactly that; 0 unexplained). The PAT after such a TD stays in |
| `yds_allow_0_100` … `yds_allow_550p` | defense / `total_yards_allowed` bracket rows | Sleeper has more brackets than NFL.com — map each |
| any other key with `v != 0` | — | **abort**: list for the user |

The derived 2026 set is diffed against 2025 and presented for sign-off (D-10). Keys with `v == 0`
are skipped.

### 5.1 D-10 preparation — live league `scoring_settings` vs 2025 NFL.com rules (2026-11-05)

Source: `GET /v1/league/1393640392104099840` → `scoring_settings` (non-zero keys only), compared
with the 51 rules stored for 2025. **Status: TBD — awaiting user sign-off.**

**Same as 2025 (once mapped):** passing 0.04/yd (= 1 per 25), 4/TD, −2/INT, 2/2-pt; rushing and
receiving 0.1/yd (= 1 per 10), 6/TD, 2/2-pt; **full PPR** (`rec = 1`); 100/200-yard rush and receiving
bonuses (+1/+3) and 300/400-yard passing bonuses (+1/+3); long-TD bonuses 40+ (+1) and 50+ (+3)
for passing, rushing and receiving; fumble lost −2; XP 1; DEF TD 6, INT 2, fumble recovery 2, sack 1,
safety 2, special-teams TD 6; points-allowed brackets 10/7/4/1/0/−1/−4 (same breakpoints);
yards-allowed values (Sleeper splits 300–399 and 500+ into finer brackets with the same points).

**How Sleeper combines the keys (✅ S2 evidence 2026-11-09, §10; D-10 itself is decided in S6):**

| Sleeper | Effect | 2025 NFL.com |
|---------|--------|--------------|
| `fgm 3` + `fgm_50_59 2` + `fgm_60p 2` | 3 for any FG, +2 at 50+ → **5** (confirmed) | 0–49 = 3, 50+ = 5 |
| `bonus_pass_yd_300 1` + `bonus_pass_yd_400 3` (likewise 100/200 rush/rec) | **tiered**: a 400-yard game = **3** (only the 400 key fires) | stored as tiers (+1 at 300, +3 at 400); S6 checks whether NFL.com paid 3 or 4 |
| `pass_td_40p 1` + `pass_td_50p 3` (and rush/rec) | **stacked**: a 55-yard TD = 1 + 3 = **4** | S6 checks NFL.com history |

**New on Sleeper (no 2025 equivalent):**

| Key | Points | Meaning | Data needed |
|-----|--------|---------|-------------|
| `pass_cmp_40p` | +1 | each completion of 40+ yards (QB) | count from nflverse play-by-play (same approach as the long-TD backfill) |
| `rush_40p` | +1 | each rush of 40+ yards | PBP |
| `rec_40p` | +1 | each reception of 40+ yards | PBP |
| `fum_rec_td` | +6 | offensive player recovers a fumble for a TD | PBP |
| `blk_kick` | +2 | DEF blocks a kick | nflverse team-defense / PBP |
| `def_2pt` | +2 | DEF returns a 2-pt try | PBP |
| `st_fum_rec`, `def_st_fum_rec` | +2 | special-teams fumble recovery | PBP |

So the 2026 rules are "2025 + big-play (40+) bonuses on non-TD plays + a few rare DEF/ST events".
The 40+ yard play bonuses are the ones that will move scores most; they need a PBP-derived stat key
in `player_stats_raw` before the reconstruction can match Sleeper's official points. Plan for that
in S6 (new stat keys `passing_completions_40p`, `rushing_plays_40p`, `receptions_40p`).

## 6. Transactions → `transactions`

Sleeper groups one move as one transaction with `adds{player: roster}` / `drops{player: roster}`.
Each player-team leg becomes one canonical row, as on NFL.com.

| Sleeper `type` / `status` | Canonical rows |
|---------------------------|----------------|
| `waiver`, `complete` | `waiver_add` (in) + `drop` (out) per dropped player; `extra_data.faab_bid = settings.waiver_bid` |
| `waiver`, `failed` | `waiver_add` with `status='failed'`, `extra_data.faab_bid`, `extra_data.failure_reason = metadata.notes` *(new capability)* |
| `free_agent` | `free_agent_add` / `drop` |
| `trade` | `trade` legs, `direction` in/out, `counterpart_team_id`, per player. ✅ S7-D5: plus one player-less `trade` leg (`player_id NULL`, `direction='in'`) per team that moved picks or FAAB, `extra_data = {picks_in, picks_out, faab_in, faab_out}` from `draft_picks` / `waiver_budget` |
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
| `matchups.is_playoff` | ✅ S7-D2 (legacy convention): the team-week is a game in **either** bracket that round |
| `matchups.is_consolation` | ✅ S7-D2: a losers-bracket game only (winners-bracket 3rd/5th-place games are not, as in 2010-2025). Team-weeks with no game: `extra_data.postseason_role = 'bye'` (plays later) or `'placed'` (done; the `p=5` games are round 2), `is_playoff = false` (S7-D4). ⏸ re-probe after week 15 that Sleeper pairs postseason matchups as the bracket says |
| `champion_team_id` / `runner_up_team_id` | winners bracket match with `p = 1`: `w` / `l` |
| `last_place_team_id` (the Sacko) | the highest place number (12th): the loser of the losers-bracket `p=5` game, as `t*_from = {w: m}` says winners advance. ✅ S7-D3: every decided game's `w` is checked against the week's scores; a winner who scored less withholds all places (would mean loser-advances). ⏸ re-probe after weeks 15 and 17 |
| `teams.playoff_finish` / `final_rank` | ✅ S7: place = bracket offset (0 / 6) + `p` for the winner, + 1 for the loser; both columns get it, as in legacy. `made_playoffs` = seeded into the winners bracket, once the regular season is final |

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

✅ S7: snapshots are written for league-referenced players only, one row per capture day (the
weekly Wednesday capture, after the week's games, S7-D7; per-week official game status comes from
nflverse `player_injury_reports`); text fields (injury
notes, news) are not stored.

nflverse remains the authority for `players.nfl_team`, `is_active`, `last_season` (existing
conflict policy in `03_DATA_SOURCES.md`); Sleeper values are snapshots, not overwrites.

## 9. NFL team codes

Sleeper uses `LAR`, `JAX`, `WAS`, `LV`, `LAC`; DEF player ids are team codes. Fold every code
through `nfl_teams.canonical_franchise` before matching — the same fix that resolved the DST
relocation join (`SD/OAK/STL` vs `LAC/LV/LA`).

## 10. S2 probe evidence (2026-11-09)

Re-run any time (offline, archive only; `--schedules` loads nflverse scores):
`uv run python scripts/sleeper/probe_field_mapping.py --season 2026 --schedules`. Its output has
keys, values and counts only, so it is safe for this public doc.

- **Scoring is fully reproducible from the payloads.** For all 1,749 rostered player-weeks
  (weeks 1–9), `players_points` = Σ `stats_week` stat × `scoring_settings` value, with 0 mismatches.
  S6 can therefore treat Sleeper's own stat keys as the scoring input.
- **Bonus combination (D-10 evidence, decided in S6):** yardage bonuses are tiered, long-TD bonuses
  stack, a 50+ FG = 5, points allowed excludes return TDs and safeties scored against the offense
  (§5 rows).
- **Stats payloads churn.** `stats_week` embeds live player metadata (`injury_status`,
  `news_updated`) next to the stats, and Sleeper's caches flip those values between requests. A
  re-capture can therefore add a new `stats_week` revision with no stat change (~100 KB gzip each).
  League endpoints dedupe exactly.
- **Not captured as fixtures as-is:** the league payload carries chat fields
  (`last_message_text_map`, `last_author_*`); the fixture builder drops them (D-11).
- **Sleeper's own NFL.com import:** `metadata.imported_history_provider = "nfl"`, but
  `previous_league_id = "0"`, so lineage stops at 2026. The sealed 2010–2025 DB stays the only
  source for legacy seasons; Sleeper's imported copy is ignored.
- **Not yet observable** (re-probe dates): bracket `w`/`l`/`p` outcomes (after week 15);
  `roster_id` stability across renewals (first renewal, 2027); trades with picks/FAAB, co-owners,
  `custom_points`, empty `"0"` starter slots, 200+ yard rush/rec bonuses (whenever they first occur).
