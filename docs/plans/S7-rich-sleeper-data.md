# S7 — Rich Sleeper data (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S7. Design sources: `04_SLEEPER_FIELD_MAPPING.md`
§2 (league), §3 (rosters → teams), §6 (transactions), §7 (brackets), §8 (players directory); the
S3 schema (`playoff_brackets`, `traded_picks`, `player_status_snapshots`, `assets`, the new
`teams` / `seasons` columns).

**Status:** PLAN approved 2026-11-09: S7-D1..D8 as recommended, **except S7-D7 = game-day
capture** (a Sunday-morning players-directory capture, so statuses are as of kickoff). BUILD on
`feature/s7-rich-sleeper-data`.

## Goal

The Sleeper facts the dashboard will surface beyond S5's core tables: brackets and season results,
pick trading and FAAB in trades, team extras, injury/depth snapshots, avatars and the league's
settings, all loaded idempotently by `sleeper-league load`.

## Starting state (verified 2026-11-09, archive + live DB, read-only)

- **Brackets:** both `winners_bracket` and `losers_bracket` are archived, pre-generated for 6 + 6
  teams with current seeds filled in; **no game decided** (`w`/`l` all null). Shape (both
  brackets): r1 = 2 games (seeds 3-6; seeds 1-2 bye), r2 = 2 games + a `p=5` game between the r1
  losers, r3 = `p=1` final + `p=3` game. `t1_from`/`t2_from` say `{w: m}` (winner advances) in
  both. `playoff_week_start = 15`, one week per round → weeks 15-17. `loser_bracket_type` is
  absent from settings.
- **Legacy convention** (2025 NFL.com, checked): every bracket game is `is_playoff`; only the
  lower bracket (places 7-12) is also `is_consolation` (the 3rd- and 5th-place games are not);
  every team has `final_rank` = `playoff_finish` 1-12; `last_place_team_id` = the 12th-place team
  (the Sacko); byes have **no** matchup row. The dashboard's `postseason_classification` reads
  these.
- **Trades:** none yet in 2026 (transactions: 264 waivers incl. 162 failed, 158 free agent).
  `traded_picks` is `[]`. S5 already loads failed claims (S5-D7) and commissioner moves.
- **Rosters payload** (current state only): `settings.ppts`/`ppts_decimal`, `waiver_position`,
  `waiver_budget_used`, `total_moves`; `metadata.streak`, `metadata.record`. No `division` (no
  divisions in 2026).
- **Players directory:** captured whenever a capture runs and the last copy is > 24 h old (so
  weekly today, Wed 06:00). 3,266 DB players carry a `sleeper_id`.
- **Avatars:** league avatar set; 11/12 users have a user avatar, 10/12 a team avatar. The
  existing `crawlers/nfl_com/media.py::download_and_store` stores assets deduped by sha256.
- No capability matrix exists yet (it is S8).

## Design

### 1. Brackets → `playoff_brackets`, postseason flags, season results (`normalizer/sleeper_brackets.py`)

- One `playoff_brackets` row per (bracket, round, match), synced each load (update / insert /
  delete), `team1/2_id`, `winner/loser_team_id`, `placement` = `p`, `*_from` raw, `source='sleeper'`.
  Pre-playoff rows show current seeds and are rewritten as standings move.
- Round → week: `week = playoff_week_start + r - 1` (`playoff_round_type = 0`; another value
  aborts the derivation with a message, not a guess).
- **Places** (S7-D2, S7-D3): winners bracket places 1-6, losers bracket 7-12. A decided match
  with `p` gives its winner place `offset + p` and its loser `offset + p + 1` (offset 0 / 6).
  `final_rank` = `playoff_finish` = place; `made_playoffs` = in the winners bracket. Champion /
  runner-up = winners `p=1` w / l; `last_place_team_id` = place 12.
- **Direction check (S7-D3):** before writing places, every decided match's `w` must have the
  higher score in that week's matchups. A failure means the bracket runs "loser advances" (toilet
  bowl) or the data is inconsistent → places are **not** written, the load reports it.
- `matchups` for weeks 15-17: a bracket game sets `is_playoff = true`, and `is_consolation = true`
  when it is a losers-bracket game. A team-week in no bracket game per S7-D4: 4 teams in week 15 (the top two seeds of each
  bracket) and 4 in week 17 (the two `p=5` games are in round 2, week 16).
- Seasons with results decided are only those whose final (`p=1`) is decided; partial places are
  written as they are decided.

### 2. Trades: picks and FAAB (S7-D5) → `transactions`

Per completed trade, per team involved: one extra `trade` leg with `player_id = NULL`,
`direction = 'in'`, `extra_data = {picks_in, picks_out, faab_in, faab_out}` (picks as
`{season, round, original_team_key}`), built from the transaction's `draft_picks[]` and
`waiver_budget[]`. Player legs are unchanged. The S3 partial unique index already admits a
player-less leg (`coalesce(player_id, -1)`). A pick-only or FAAB-only trade therefore still has
rows.

### 3. `traded_picks` (S7-D6)

The current owner of every traded future pick, from the newest archived `/traded_picks`, synced
per load: (`league_id = '36271'`, `pick_season`, `round`, `original_owner_id`) →
`current_owner_id`, `previous_owner_id` (owner ids via the 2026 roster → owner crosswalk),
`observed_at` = capture time, `source_transaction_id` = the latest completed trade that moved it.
A pick back with its original owner is deleted (Sleeper drops it from the list too). History
comes from the trade legs.

### 4. Team extras → `teams.extra_data`, `teams.division`

`extra_data = {ppts, streak, record_string, waiver_position, waiver_budget_used, total_moves,
co_owner_user_ids, as_of_week}`. These are Sleeper's **current** values (the rosters payload has
no history), stamped with the last finalized week. `division` = `settings.division` when present
(NULL in 2026; the name lookup is built once a season has divisions).

### 5. `seasons.settings_snapshot`

`{settings, roster_positions, scoring_settings, draft: {type, rounds, draft_order,
slot_to_roster_id}}` from the archived league and draft. The league name, avatar id and
`metadata` (incl. the imported-history ids) are left out.

### 6. Player status snapshots (S7-D7) → `player_status_snapshots`

From each archived players-directory revision: one row per (player, capture date UTC,
`source='sleeper'`) with injury status / body part / start date, practice participation, depth
chart position / order, team, status; `extra_data.news_updated` (timestamp only, no text). Scope:
players referenced by the league that season (rostered, transacted or drafted). Idempotent per
day (`uq_player_status_snapshots_day`).

### 7. Avatars (S7-D8) → `assets`, `teams.team_avatar_asset_id` / `owner_avatar_asset_id`

`sleeper-league capture` downloads the league avatar, user avatars
(`sleepercdn.com/avatars/<id>`) and team avatars (`metadata.avatar`) when their URL is new, via
`download_and_store` (sha256 dedupe; kinds `team_avatar`, `owner_avatar`, `league_avatar`). The
load links them to `teams`. Fixtures carry no avatars and no avatar ids (pseudonymization).

### Wiring

All of it runs inside `sleeper-league load` (offline except S7-D8, which is capture), using the
S5 sync helpers so a reload is a no-op. New counts appear in the load summary.

## Tests

- **Brackets** on a synthetic decided 6 + 6 fixture with 3rd/5th-place and lower-bracket games:
  places 1-12, champion / runner-up / last place, `is_playoff` / `is_consolation` per the legacy
  convention, idle team-weeks per S7-D4; partial (week-15-only) decisions; the direction check refuses an
  inverted bracket; the live-shaped undecided fixture writes rows but no places.
- **Pick ownership over two trades** (synthetic): A→B then B→C gives `current = C`,
  `previous = B`, `source_transaction_id` = the second trade; returning a pick deletes the row;
  the per-team player-less legs carry `picks_in/out` and `faab_in/out`.
- **Status snapshots:** idempotent per day; a second directory revision on a later day adds rows.
- **Avatars:** two teams with the same image → one asset row (sha256), both teams linked.
- Team extras + settings snapshot written; full reload no-op (S5 invariant); sealed data untouched
  and `legacy verify` passes.

## Rollout

On a copy (as S5/S6; live joins the go-live, S7-D1): capture (avatars) → `sleeper-league load` →
check counts. After week 15 and week 17 the re-probe (`04` §7 ⏸) confirms the bracket semantics
on real decided games, before the places are trusted on live.

## Done when

- [x] Each facet populated for 2026 to date on the copy: brackets rows (undecided), team extras,
  settings snapshot, status snapshots, avatars; picks/FAAB and places fixture-tested (no trades
  or decided games exist yet).
- [x] Reload is a no-op; `legacy verify` passes; full gate green.
- [x] RUNBOOK + `02` §S7 + `04` §6/§7 updated; PR → `dev`.

Capability presence checks (`available` where data exists) move to S8, which builds the
capability matrix.

## Decisions (recommended defaults — need the user's approval)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| S7-D1 | Where S7 writes | Same as S5/S6: build and verify on a DB copy; live gets it in the one go-live sequence. | Write live now (needs the 2026 live load first, which waits on the dashboard). |
| S7-D2 | Postseason flags and places | **Follow the legacy convention**: every bracket game `is_playoff`; losers-bracket games also `is_consolation`; 3rd/5th-place games are not consolation; `final_rank` = `playoff_finish` = bracket place 1-12; Sacko = 12th place. The dashboard keeps working unchanged. | `04` §7's first idea: winners-bracket placement games (`p > 1`) count as consolation too (breaks parity with 2010-2025). |
| S7-D3 | Trusting Sleeper's `w` / `l` | Cross-check every decided game against the matchup scores; if a winner scored less, write no places and report it (catches a "loser advances" toilet bowl). Re-probe after weeks 15 and 17. | Trust `w` / `l` as given. |
| S7-D4 | Idle postseason team-weeks (4 teams in week 15 on a bye, 4 in week 17 already placed; Sleeper still scores them) | Keep the row (the points are real), `opponent NULL`, `is_win NULL`, `is_playoff = false`, `extra_data.postseason_role = 'bye'` / `'placed'`. The dashboard's D-milestones treat opponent-less rows as non-games. | Delete those rows so 2026 looks exactly like legacy (their points are lost from the DB). |
| S7-D5 | Picks and FAAB in trades | One extra player-less `trade` leg per team per trade, carrying `picks_in/out` and `faab_in/out`; player legs unchanged. Pick-only trades still produce rows. | Copy the trade's full asset lists onto every player leg (no player-less rows, but a pick-only trade produces nothing). |
| S7-D6 | `traded_picks` | Current-ownership snapshot, synced each load (picks back with their original owner are deleted); history comes from the trade legs. | Append-only rows per observation (a history table; more rows, harder to query "who owns it now"). |
| S7-D7 | Status snapshot cadence and scope | Use the existing capture (weekly, Wednesday morning) and store league-referenced players only (a few hundred rows per capture). | (a) Add a Sunday-morning directory capture for pre-kickoff statuses (second scheduled task); (b) store all 3,266 Sleeper-linked players. |
| S7-D8 | Avatars | Downloaded during `sleeper-league capture` (the only network step), deduped by sha256, linked to teams at load; never committed. | A separate `sleeper-league avatars` command run by hand. |

## Build notes (2026-11-09)

On a copy of the S6 copy: capture stored 20 avatars (1 league, 9 owner, 10 team; two owners share
one image URL); the load wrote 14 bracket rows (undecided), extras on 12 teams, a 4.6 KB settings
snapshot, 289 status rows; the reload changed nothing; `legacy verify` passed before and after.

- **Game-day capture (S7-D7 as chosen):** `sleeper-league capture --directory-only` +
  `scripts/sleeper/gameday_capture.sh`; Windows task `dz-sleeper-gameday` registered for Sunday
  12:15 PM ET (first run 2026-11-15). Thursday / Monday / late-window inactives are not covered;
  a missed run fires at the next start, so its statuses may be post-kickoff (its date shows it).
- **Status rows only for days not yet snapshotted**, so a load doesn't re-read every ~5 MB
  directory revision; a player who joins the league later has no rows for earlier days.
- **Trade pick/FAAB legs** are written only for teams that moved picks or FAAB (a team that only
  swapped players has none).
- **Avatar dedupe limit:** identical bytes under two different URLs keep one asset row under the
  first URL, so only the first team is linked (the capture reports the count). Sleeper avatar ids
  are content hashes, so the same image normally has the same URL.
- `crawlers/nfl_com/media.download_and_store` gained a `fetch_errors` parameter (default
  unchanged) so the Sleeper downloader reuses it.
- **Places before the final:** a place is written as soon as its game is final, so the Sacko
  (decided in week 16) is known before the champion.
- Capability presence checks move to S8 (the capability matrix).
