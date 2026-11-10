# S6 — Scoring (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S6. Design sources: `01_PIPELINE_ARCHITECTURE.md`
§9 (two numbers per player-week, rule derivation, verification), `04_SLEEPER_FIELD_MAPPING.md`
§5 / §5.1 / §10 (key mapping, D-10 preparation, S2 probe evidence), decision D-10 (`00`).

**Status:** PLAN, written 2026-11-09 on `feature/s6-scoring`. Nothing is built yet. **D-10 (the rule
sign-off) and the decisions at the end need the user's approval before the BUILD session.**

## Goal

Season 2026 gets a `scoring_rules` set derived from Sleeper and signed off by the user (D-10),
reconstructed points from nflverse stats (the cross-era baseline), and a reconcile report
comparing them with Sleeper's official points, with every residual divergence explained.

## Starting state (verified 2026-11-09)

- **Evidence for D-10:**
  - S2 probe: Sleeper `players_points` = Σ Sleeper stats × `scoring_settings` for 1,749/1,749
    player-weeks; yardage bonuses tiered; long-TD bonuses stacked; a 50+ FG = 5; `pts_allow`
    excludes return TDs and safeties scored against the offense.
  - The league's own settings page (user's CSV export, 2026-11-09) equals the API
    `scoring_settings` 1:1, and labels the bonuses "100–199 / 200+ yard game", which confirms the
    tiers.
- **2025 NFL.com rules** (51 rows): yardage bonuses are **tiered** (`threshold_max` 199 / 399),
  long-TD bonuses **stack** (counts of 40+ and of 50+ TDs, +1 / +3), FG 3 per range and 5 at 50+.
  Points-allowed and yards-allowed brackets are equivalent to Sleeper's.
- nflverse weekly stats for 2026 are ingested (3,007 rows, weeks 1–9). There are **no** 2026
  team-defense rows: the rollup scores only DEF players rostered that season, and live has no
  2026 rosters (S5-D1). There are no 2026 `scoring_rules` and no `player_stats_scored`.
- `scoring/verify.py` (sweep + reconcile) reads `nfl_com_points` directly.
- The engine knows `blocked_kicks` and `fumble_return_tds`, but nflverse weekly stats carry **no**
  40+ completion/rush/reception counts, defensive 2-pt returns or special-teams fumble
  recoveries.

## D-10 — the derived 2026 rule set vs 2025 (for sign-off)

**Identical to 2025** (same rows once mapped):

| Area | Rule |
|---|---|
| Passing | 1 per 25 yd, 4 / TD, −2 / INT, 2 / 2-pt; +1 at 300–399 yd, +3 at 400+ (tiered); long TD +1 (40+) and +3 (50+), stacked |
| Rushing / receiving | 1 per 10 yd, 6 / TD, 2 / 2-pt; +1 at 100–199, +3 at 200+ (tiered); long TD +1 / +3, stacked; PPR 1 per reception |
| Kicking | FG 3 at every range up to 49, 5 at 50+ (Sleeper: 3 + 2 at 50–59, 3 + 2 at 60+); PAT 1 |
| Misc | fumble lost −2; special-teams TD (player) 6 |
| Defense | TD 6, INT 2, fumble recovery 2, sack 1, safety 2, special-teams TD 6; points allowed 10/7/4/1/0/−1/−4 (0, 1–6, 7–13, 14–20, 21–27, 28–34, 35+); yards allowed 10/7/4/1/0/−1/−4 (<100, 100–199, 200–299, 300–399, 400–449, 450–499, 500+) |

**New on Sleeper in 2026** (no 2025 equivalent):

| Sleeper key | Points | Engine stat (new unless noted) | Source |
|---|---|---|---|
| `pass_cmp_40p` | +1 per 40+ yd completion | `passing_completions_40_plus` | play-by-play |
| `rush_40p` | +1 per 40+ yd rush | `rushing_attempts_40_plus` | play-by-play |
| `rec_40p` | +1 per 40+ yd reception | `receptions_40_plus` | play-by-play |
| `fum_rec_td` | 6 | `fumble_return_tds` (exists) | nflverse weekly |
| `blk_kick` (DEF) | 2 | `blocked_kicks` (exists; not derived yet) | play-by-play |
| `def_2pt` (DEF) | 2 | `defensive_2pt_returns` | play-by-play |
| `def_st_fum_rec` (DEF) | 2 | `special_teams_fumble_recoveries` (defense) | play-by-play |
| `st_fum_rec` (player) | 2 | `special_teams_fumble_recoveries` (misc) | play-by-play |

**Representation notes:**
- Sleeper's per-yard rates (0.04 / 0.1) are stored as 1 per 25 / 1 per 10, as in 2025.
- Sleeper's split brackets (`fgm_50_59` + `fgm_60p`, `yds_allow_300_349` + `_350_399`,
  `_500_549` + `_550p`) collapse into the 2025 rows, because the points are equal.
- `yds_allow_0_100` means "less than 100" (league page label), so 0–99, as in 2025.
- Any **non-zero** Sleeper key the mapping doesn't know **aborts** rule writing and is listed.

## Design

### 1. `scoring/sleeper_rules.py`

`derive_rules(scoring_settings) -> list[RuleRow]`. A table-driven mapping (keys → category /
stat_key / unit or bracket); zero-valued keys are skipped; unknown non-zero keys raise
`UnmappedScoringKeyError(keys)`. Equal-points adjacent brackets merge into one row.
`diff_rules(a, b)` gives a readable row diff. CLI: `ff-pipeline sleeper-league rules [--diff 2025]
[--write]`. It is read-only by default; `--write` needs the D-10 approval recorded (rules are
written to `scoring_rules` for the season) and is a writing command (preflight).

### 2. New stat derivations (play-by-play, the pattern of `derive_long_td_bonus_counts`)

- **Per player:** 40+ yd completions (passer), 40+ yd rushes (rusher), 40+ yd receptions
  (receiver), counted on `yards_gained >= 40` of completed passes and rushes. Special-teams
  fumble recoveries (recovering player, `play_type` in kickoff/punt).
- **Per team defense:** blocked kicks (FG / PAT / punt blocks by the defense), defensive 2-pt
  returns, special-teams fumble recoveries.
- Merged into `player_stats_raw` (source `nflverse`) like the long-TD counts. Additive keys only,
  and legacy seasons are untouched (sealed rows are not re-ingested; scoring of sealed seasons
  does not change because their rules have no such keys).

### 3. Two numbers per player-week (`01` §9)

- **Official:** `team_rosters.extra_data.platform_points` (S5).
- **Reconstructed:** `rescore --season 2026` over nflverse stats with the 2026 rules.
- **`verify.py` adapter:** `official_points(roster_row, season)` reads `platform_points` for
  `platform='sleeper'` seasons, `nfl_com_points` otherwise. `verify --sweep --reconcile --season
  2026` works unchanged otherwise.

### 4. Reconcile report → `docs/sleeper/reports/2026-reconcile.md`

Per started player-week: reconstructed vs official, with divergences classified:
- **stat source** (nflverse vs Sleeper stats differ; shown when Sleeper stats are stored, S6-D3);
- **DST definition** (points allowed: Sleeper excludes defensive/return TDs and safeties scored
  against the offense);
- **stat correction / provisional week**;
- **unexplained**.

The report holds counts, players (public NFL names) and weeks, with **no league team or owner
names** (public repo).

## Tests

- `sleeper_rules`: every mapped key incl. bracket merges; zero keys skipped; an unknown non-zero
  key aborts; derive(2026 fixture `scoring_settings`) == the D-10 table; the diff vs a 2025-shaped
  rule set shows only the new rows.
- Derivations: fixture play-by-play rows → expected 40+ counts (incl. a 40+ TD counting as both a
  40+ play and a long TD), blocked kick, def 2-pt, ST fumble recovery; idempotent merge.
- **Engine parity:** for fixture player-weeks, engine(Sleeper stats mapped to engine keys, 2026
  rules) == Sleeper `players_points` (the S2 identity, through our engine).
- `verify` adapter: `platform_points` read for Sleeper seasons, `nfl_com_points` for NFL.com.
- Reconcile classifier on synthetic divergences.

## Rollout (BUILD session, with the user)

On the **S5 copy** (live 2026 is not loaded, S5-D1): write rules → `run --source team_defense
--season 2026` → derivations → `rescore --season 2026` → `verify --sweep --reconcile` → report.
Live gets the same steps as part of the S5 go-live runbook (S6-D1).

## Done when

- [ ] D-10 signed off; rules written (copy; live with the go-live).
- [ ] 2026 scored (weeks 1..current) on the copy.
- [ ] Reconcile report committed under `docs/sleeper/reports/`; every residual class explained.
- [ ] Full gate green; RUNBOOK + `02` §S6 updated; PR → `dev`.

## Decisions (recommended defaults — need the user's approval)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| **D-10** | 2026 scoring rules | **Approve the table above**: identical to 2025 for every 2025 rule, plus the 8 new Sleeper rules. | Change a value, or score some new rule as 0 (e.g. to keep 2026 points comparable with 2025). |
| S6-D1 | Where S6 writes | Same as S5: rules, team defense, derivations and rescore run on the copy; on live they join the go-live runbook (one sequence: load → rules → team defense → rescore). | Write 2026 rules and scores to live now (the dashboard may show 2026 player points before the season is visible). |
| S6-D2 | The 8 new rules' stats | Derive all of them from play-by-play now (40+ plays are frequent, so the reconcile would be noisy otherwise; the rare ones are cheap with the same pattern). | Derive only the 40+ plays; leave blocked kicks / def 2-pt / ST fumble recoveries as a documented residual. |
| S6-D3 | Store Sleeper's own weekly stats | Yes, as `player_stats_raw` with `source='sleeper'`, non-primary (never scored as the baseline). It lets the reconcile tell "stat source differs" from "rule bug". | Off (classify from totals only). |
| S6-D4 | Reconcile tolerance | ≤ 0.01 per player-week counts as a match; provisional (in-progress) weeks are reported separately. | Exact equality. |
