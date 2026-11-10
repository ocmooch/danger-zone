# S8 — Read surface (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S8 (gate **G3**: the dashboard can consume both
eras through supported helpers). Design sources: `00_SLEEPER_ERA_MASTER_PLAN.md` §4.1-4.2 (read
seam, capability matrix), `01_PIPELINE_ARCHITECTURE.md` §8.3 and §9.1, `docs/06_API_CONTRACT.md`.

**Status:** PLAN approved 2026-11-09 with changes (see "Approved decisions" at the end, which
override the table and the design text where they differ). BUILD on `feature/s8-read-surface`.

## Goal

Additive, read-only helpers in `repository/queries.py` (the dashboard's only allowed Phase 1
import surface) that answer every 2026 question source-neutrally, a capability matrix per season,
and the Phase 1 API additions. No DB writes, no migration; legacy outputs provably unchanged.

## Starting state (verified 2026-11-09, read-only)

- `queries.py` has 50+ helpers; none knows `platform`, `platform_points`, brackets, traded picks,
  status snapshots or failed claims.
- The dashboard's league score is `analytics/scoring.authoritative_week_points` =
  `coalesce(nfl_com_points, total_points)`; a few dashboard modules also read `nfl_com_points`
  directly (`players.py`, `matchups.py`, `records.py`). Moving those onto the new helper is
  dashboard work (D2), not S8.
- **Two latent read bugs for Sleeper seasons** (legacy data can't trigger them):
  - `standings_for_season` counts a row with `is_win NULL` and both scores as a **tie**, which is
    what a provisional (in-progress) week looks like (S5-D2), and adds idle postseason rows
    (S7-D4, no opponent) to points-for.
  - `list_transactions` / `transactions_for_team` return failed waiver claims (S5-D7,
    `status='failed'`) mixed with moves that happened. Legacy rows all have `status NULL`.
- Phase 1 API: `SeasonOut` has no platform or capabilities; `TransactionOut` has no `status`.
- Mixed-era test fixture: the S5 sealed fixture DB (2015 NFL.com sealed, 2016 NFL.com open) plus
  the 2026 Sleeper load — what S5/S6/S7 tests already build.

## Design

### 1. Points (source-neutral)

- `official_slot_points_expr(roster=TeamRoster)` → `coalesce(platform_points, nfl_com_points)`
  as a Float SQL expression (from `team_rosters.extra_data`).
- `authoritative_week_points_expr(roster=TeamRoster, scored=PlayerStatsScored)` →
  `coalesce(official_slot_points_expr, scored.total_points)`: the dashboard's current definition
  moved upstream, so both eras share one.

### 2. Season platform + capability matrix

- `season_platform(session, season_id) -> PlatformInfo(platform, label)`:
  `nfl_com` → "NFL.com", `sleeper` → "Sleeper".
- `season_capabilities(session, season_id) -> dict[str, Capability]`,
  `Capability(state, note)`, for every key of master plan §4.2. A static platform table gives the
  default; **data-presence checks win** (rows present upgrade the state; missing core data
  downgrades it):

| Key | NFL.com default | Sleeper default | Presence check |
|---|---|---|---|
| `standings`, `matchups`, `rosters_weekly`, `draft` | available | available | core: rows exist (else `not_yet`) |
| `official_player_points` | available | available | core: roster rows carry official points; some weeks without → `partial` |
| `reconstructed_player_points` | available | available | core: `player_stats_scored` rows |
| `transactions_moves` | available | available | core |
| `faab_winning_bids` | partial | available | waiver rows with `faab_bid` |
| `faab_failed_bids` | not_on_platform | available | event (S8-D2) |
| `lineup_change_log`, `setting_change_log` | available | not_on_platform | legacy: rows of that type |
| `official_brackets` | derived | available | Sleeper: `playoff_brackets` rows |
| `official_divisions` | derived (2010-2019) / not_used | available / not_used | `teams.division` set |
| `potential_points` | derived | available | Sleeper: `teams.extra_data.ppts` |
| `trades_with_picks_faab`, `traded_future_picks`, `co_owners`, `commissioner_score_overrides` | not_on_platform | available | event |
| `player_status_daily` | partial (nflverse weekly reports) | available | snapshot / report rows |
| `availability_weekly` | partial | derived | `player_availability` rows |
| `avatars` | available | available | linked asset ids; some teams without → `partial` |

  States: `available`, `partial`, `derived` (not stored; the dashboard computes it), `not_yet`
  (in-progress season, not produced yet), `not_on_platform`, and `not_used` (S8-D1).

### 3. New-feature reads

- `brackets_for_season(session, season_id) -> list[PlayoffBracket]` (bracket, round, match).
- `traded_picks_for_league(session, league_id, *, pick_season=None) -> list[TradedPick]`.
- `player_status_asof(session, player_ids, as_of, *, max_age_days=7) -> dict[int, PlayerStatusSnapshot]`
  — newest snapshot on or before `as_of`, none if older than `max_age_days` (S8-D5).
- `failed_waiver_claims(session, season_id, *, team_id=None, week=None) -> list[Transaction]`.
- `trade_assets(session, season_id) -> list[Transaction]` — the player-less pick/FAAB legs (S7-D5).

### 4. Read fixes (S8-D3, S8-D4)

- `standings_for_season`: skip rows with no opponent and rows whose `extra_data.finalized` is
  false. Legacy rows have neither, so their output is unchanged (proven by a test over every
  legacy season of the fixture and a read-only check on live).
- `list_transactions`, `transactions_for_team`: `include_failed=False` by default.

### 5. Phase 1 API (additive) + `docs/06_API_CONTRACT.md`

- `SeasonOut`: `platform`, `platform_label`, `capabilities` (`{key: {state, note}}`).
- `TransactionOut`: `status`; `GET /transactions` gains `include_failed` (default false).
- No new Phase 1 endpoints for brackets / picks / statuses (S8-D6): the dashboard reads them
  through the helpers above.

## Tests

- Helpers on the mixed-era fixture: platform per season; capabilities for 2015 / 2016 (NFL.com)
  vs 2026 (Sleeper) incl. a presence upgrade and a `not_yet`; brackets, traded picks, status as-of
  (incl. the staleness cutoff), failed claims, trade assets.
- **Legacy parity:** `authoritative_week_points_expr` equals the dashboard's current expression
  on every legacy fixture row; `standings_for_season` output for legacy seasons byte-identical
  before/after; plus a read-only check of both on the live DB (all 2010-2025 rows).
- Sleeper: a provisional week is not a tie; idle postseason rows add nothing; failed claims hidden
  by default and returned by `failed_waiver_claims`.
- API contract tests for the new fields.

## Rollout

Code only: nothing to apply to live. The dashboard's BFF imports danger-zone editable, so after
the merge it sees the helpers (additive; existing outputs unchanged).

## Done when

- [ ] Helpers merged with tests; legacy outputs of the moved expression and of standings proven
  identical (fixture + live read-only).
- [ ] Phase 1 API + `06_API_CONTRACT.md` updated; full gate green in both repos.
- [ ] `02` §S8 marked done; dashboard D1/D2 can start.

## Decisions (recommended defaults — need the user's approval)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| S8-D1 | A feature the league simply didn't use that season (e.g. no divisions in 2026) | A sixth state, `not_used`, so the dashboard can say "no divisions this season" instead of a gap. | Report it as `not_on_platform` (wrong for Sleeper, which supports divisions). |
| S8-D2 | Event features with zero events (no trades yet, no co-owners, no overrides) | `available` whenever the platform captures them: an empty list is a real answer ("no pick trades yet"). Core data (points, rosters, matchups) uses presence: missing in an in-progress season → `not_yet`, some weeks missing → `partial`. | `not_yet` until the first event (the dashboard can't tell "none happened" from "not loaded"). |
| S8-D3 | `standings_for_season` on Sleeper data | Skip provisional weeks and opponent-less rows (no change for 2010-2025, proven). | Leave it (the in-progress week counts as a tie for every team). |
| S8-D4 | Failed waiver claims in the existing transaction readers and `GET /transactions` | Hidden by default (`include_failed=False`) so "transactions" keeps meaning moves that happened; `failed_waiver_claims()` returns them. | Return them and let every caller filter on `status`. |
| S8-D5 | How old a status snapshot may be and still count as "status as of" a date | 7 days (one weekly capture); older → no status (an honest gap). | No limit (a 3-week-old injury status would show as current). |
| S8-D6 | Phase 1 API scope | `SeasonOut` platform + capabilities and `TransactionOut.status` only; brackets / picks / statuses stay helper reads for the dashboard. | Also add Phase 1 endpoints for brackets, traded picks and statuses now. |

## Approved decisions (2026-11-09, the user's words → what is built)

- **S8-D1 approved, "resolve silently":** the `not_used` state exists, carries **no note**, and the
  contract says consumers render nothing for it (no "no divisions this season" message).
- **S8-D2 "trades available, co-owners not":** two groups.
  - *Activity* (`trades_with_picks_faab`, `traded_future_picks`, `faab_failed_bids`): `available`
    whenever the platform captures them, even with zero events.
  - *League structure* (`co_owners`, and by the same logic `commissioner_score_overrides` and
    `official_divisions`): `available` only when present, otherwise `not_used` (silent, D1).
- **S8-D3 "current state while the season is active, static at season end"** (for standings and
  every other in-season representation):
  - Helpers that summarize a season report freshness: `season_freshness(session, season_id)` →
    `{is_final, last_finalized_week, provisional_weeks}`; in-season outputs carry
    `provisional = not is_final`.
  - Standings: W-L-T and PF/PA from finalized games (a game in progress has no result, so never a
    tie), plus the in-progress week as `live_points_for` / `live_week`; idle postseason rows are
    not games. At season end nothing is provisional and the output is static.
  - Capabilities are computed from current data each call; `not_yet` exists only while the
    season is active.
- **S8-D4 "include, for richer data" (and other similar sources):** existing readers return
  everything, with the fields to tell it apart: failed waiver claims and the player-less pick/FAAB
  legs stay in `list_transactions` / `transactions_for_team` (with `status`, `extra_data`);
  `player_raw_stats` keeps every source incl. `sleeper` (non-primary). Focused helpers
  (`failed_waiver_claims`, `trade_assets`) remain for convenience. *Dashboard note:* its team page
  uses `transactions_for_team`; 2026 views must read `status` (D-milestone work; legacy rows are
  unaffected).
- **S8-D5 approved:** 7-day staleness cutoff.
- **S8-D6 approved, at the agent's discretion given D4:** besides `SeasonOut` platform /
  capabilities / freshness and `TransactionOut.status`, small read endpoints so the richer data is
  reachable over HTTP too: `GET /seasons/{id}/brackets`, `GET /leagues/{id}/traded-picks`,
  `GET /players/{id}/status?as_of=`.
