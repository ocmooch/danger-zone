# Pre-2016 Scoring & Era-Nuance Distillation Plan

**Status:** proposed · **Owner:** ocmooch · **Drafted:** 2026-06-04 · **Branch:** TBD (`feature/pre-2016-distillation`)

Closes the largest remaining Phase 1 data gap — **§P1-V1, "2010–2015 seasons
are unscored (no period rules)"** — and, while the same labeled data is in hand,
distills the other era nuances that drifted over the league's history: roster
construction, playoff structure/timing, conferences/divisions, tiebreakers, and
team/manager ownership rights.

The guiding principle (per the kickoff brief): **prefer automated, high-confidence
distillation of truth from the data over bothering the user.** Bother the user
exactly once, for the irreducible minimum (Phase C), and only for things the data
genuinely cannot reveal.

---

## 0. Headline finding (already established, 2026-06-04)

A feasibility probe run before drafting this plan changes the shape of the work
and is the empirical foundation for everything below.

**The ground truth needed to *solve* (not guess) the rules already exists.** Every
starter-week in `team_rosters.extra_data` carries `nfl_com_points` — the actual
league score NFL.com recorded — for **all 16 seasons 2010–2025 at 100% coverage**
(~1,650 starters/season). `player_stats_raw` already maps nflverse stats to the
engine's canonical keys for those same years. So for any season we hold a labeled
dataset of `(stat_vector → league_points)`.

**The probe.** Scoring every clean skill starter (excludes DST + K) with the
**current** 51-rule set (2016) and comparing to `nfl_com_points`:

| Seasons | Exact-to-cent | Median Δ | Negative tail | Positive tail | Reading |
|---|---|---|---|---|---|
| **2011–2015** | **91–92%** | **0.00** | ~8% | <1% | Statistically identical to the verified 2016–2025 seasons. The ~8% miss is the **known long-TD-bonus pbp gap** (§P1-V2: clean −1/−4/−8 underscores), *not* a rule difference. |
| **2010** | **9.5%** | **+1.50** | 15% | 75% | A genuinely different ruleset — the engine *over*scores by a median +1.5. |

**2010 diagnosis** (delta regressed against stat drivers on 1,172 starter-weeks):

| Driver | corr(Δ, stat) | Interpretation |
|---|---|---|
| `receptions` | **+0.72** | Engine overscores high-catch players → 2010 per-reception value was **lower** than current 1.0 PPR (likely 0 or 0.5). 0-reception mean Δ = −2.9; ≥5-reception mean Δ = +3.2. |
| `passing_yards` | **−0.79** | Engine *under*scores passing → 2010 passing was **more** generous. |
| `passing_tds` | **−0.84** | Same; QB mean Δ = **−4.3** → likely 6-pt pass TDs and/or 1pt/20yd. |
| `rushing_*` | ~0.0 | Rushing essentially unchanged. |

Two clean, opposing signals on independent stat axes — exactly the structure a
linear solve disentangles cleanly.

**Implication.** "2010–2015 deferred" is really **"2011–2015 = already solved
(current rules hold), 2010 = one distinct era to recover."** Most of the perceived
risk collapses. The plan is sized accordingly.

---

## 1. Method: solve the rules, don't guess them

For each season build the clean labeled matrix `X · β = y`, where:

- one **row** per clean starter-week (skill positions; exclude DST and K in the
  first pass — they carry the known DST-rollup and FG-bracket complications and
  are fit separately in a second pass),
- **columns** `X` = the canonical stat keys already stored in
  `player_stats_raw.stats` (passing/rushing/receiving yards, TDs, receptions,
  INTs, fumbles, 2pt, the yardage bonuses, …),
- **target** `y` = `nfl_com_points` from `team_rosters.extra_data`,
- **unknowns** `β` = points-per-unit per stat key (~15 free skill params).

With ~1,170 equations and ~15 unknowns the system is **massively overdetermined**.
NFL.com coefficients are exact rationals (0.04 = 1/25, 0.1 = 1/10, 4, 6, 1.0, …),
so we solve by least squares and **snap to the nearest exact rational**, then
verify the snapped ruleset reproduces the data to the cent.

**What the solve resolves vs. doesn't:**

- **Resolves** (high confidence): every per-unit coefficient whose stat varies in
  the data — the entire skill-scoring core, INTs, fumbles, 2pt, the 100/200/300/400
  yardage bonuses, and the FG-distance brackets + XP for kickers (second pass).
- **Cannot resolve from this data** (carry the existing gaps, do not re-litigate):
  - **Long-TD-length bonuses** — needs per-TD distance from pbp; the weekly
    aggregates lack it. This is the *same* §P1-V2 gap that caps modern verify at
    ~92%, so a recovered pre-2016 ruleset is "correct" at the same ceiling.
  - **DST internals** for the pre-fix era — but DST starters *do* carry
    `nfl_com_points`, so the bracket/­event coefficients can be fit in the second
    pass where the team-defense rollup exists; flag where it doesn't.
  - Any rule whose stat simply never occurred that season (rare; flag as "unobserved").

This **reuses the existing scoring engine and the same clean-skill-player isolation
that `verify --sweep` already applies** — no parallel scoring path, no redundant
scaffolding.

---

## 2. Workstreams

Dependency order: **A → B → C → D**. A and B are fully automated; C is the single
user touch; D wires results back.

### A. Scoring-rule distillation (automated)

- **A1.** `scripts/distill_scoring_rules.py` — assembles the per-season labeled
  matrix, runs the constrained solve (§1), snaps to exact rationals, and emits a
  per-season candidate ruleset + a residual/confidence report (exact%, median Δ,
  per-stat recovered coefficient, and a flag for unobserved/ambiguous keys).
- **A2. 2011–2015** — confirm the current 51-rule set reproduces the data (the
  probe already says it does at the §P1-V2 ceiling). Then `scoring load --csv
  .project-src/dz-rules.csv --season <YR>` (or the recovered set if any season
  surprises us), `rescore --season <YR>`, `verify --sweep --season <YR>`. **This
  closes §P1-V1 for 2011–2015 immediately** — they get real `player_stats_scored`
  rows at parity with 2016+.
- **A3. 2010** — recover the distinct coefficients (expected: lower/zero PPR +
  more-generous passing). Load as a *separate* season ruleset, `rescore`, `verify
  --sweep`. Confirm the residual collapses to the long-TD/DST gaps only.
- **A4. Kicker + DST second pass** — fit the FG-bracket / XP coefficients and the
  DST event/bracket coefficients per era against their `nfl_com_points`, flagging
  any season/week with no nflverse rollup as a known gap (consistent with §P1-V1's
  existing DST handling).

### B. Structural-nuance distillation (automated)

Each is inferable from already-populated tables; the harness emits a report with a
confidence flag per finding.

- **B1. Roster construction** — derive the starting-lineup template per season from
  `team_rosters.roster_slot` counts. Already visible in the data:
  - **2010**: `1 QB / 2 RB / 3 WR / 1 TE / 1 K / 1 DEF` — **no flex**.
  - **2013**: adds a **`W/R`** flex (RB/WR-eligible).
  - **2016**: widens the flex to **`R/W/T`** (adds TE eligibility); `RES` (IR)
    slots appear.
  - Confirm exact counts and the precise switch weeks; emit a per-season template.
- **B2. Playoff structure & timing** — from `matchups.is_playoff` +
  `seasons.regular_season_weeks` / `playoff_weeks`. The reconstruction already
  encodes: **2010 = 14 regular weeks** with a smaller playoff bracket (14 playoff
  matchup-rows vs 28 in later years), **2011–2020 = 13 regular weeks**, **2021–2025
  = 14**. This is the answer to the dashboard's open **"roadmap input #1"** (the
  13↔14 season-length switch). **To be confirmed, not asked cold** — the 2010
  14-week value may be a reconstruction artifact (boundary derived from the
  champion's game count), so it is a confirm-this-inference item, not an
  open question.
- **B3. Conferences / divisions** — detect from the **matchup graph**: cluster
  teams by intra- vs inter-group play frequency per season; a divisional era shows
  as denser intra-group scheduling. Cross-check against standings grouping. Emit a
  proposed division map per era **with a confidence score**; low-confidence eras
  become a Phase C confirm item.
- **B4. Tiebreakers** — **already handled, do not redo.** The dashboard's Q5
  resolution prefers Phase 1's reconstructed `teams.final_rank` (which *bakes in*
  the historical tiebreak NFL.com applied) and flags a `tiebreak_caveat` only for
  *computed* pre-2019 ranks. This plan **documents** that rather than re-deriving
  the old best-of-3 rule. No work beyond a cross-reference.

### C. One consolidated user questionnaire (the irreducible minimum)

Batched into a single structured pass so the user is bothered exactly once. Only
things the data cannot reveal:

- **C1. Team/manager ownership rights** — who controlled each team per year across
  renames, any **co-ownership**, and ownership **transfers**. Motivation: the
  `owners` table has 12 names but **no tenure or alias data** — `joined_year`,
  `left_year`, and `aliases` are all empty. Owner-keyed analytics in the dashboard
  (careers, rivalries, the managers page) silently assume a clean
  owner↔team mapping that the data does not actually pin down before renames.
- **C2. Keeper / draft rights** — keeper rules and when they changed (undiscoverable
  from box scores).
- **C3. Confirm data-inferred change-points** the solve/structure pass flags as
  ambiguous — e.g. whether 2010 PPR was exactly 0 vs 0.5 (if the residual can't
  separate them), the proposed conference map (B3), and the 2010 14-regular-week
  anomaly (B2). Each is presented as "the data says X (confidence Y) — confirm or
  correct," never an open-ended question.

### D. Wire results back

- **D1.** Load recovered rulesets; `rescore` 2010–2015; re-run `verify --sweep`.
  Target: 2011–2015 at 2016+ parity; 2010 residual reduced to the long-TD/DST
  ceiling.
- **D2.** Populate confirmed structure: `seasons` week boundaries, the
  `owners.joined_year/left_year/aliases` from C1, and a roster-template /
  division-map reference artifact for the dashboard.
- **D3.** Set `_CONFIRMED` in **dz-dashboard** `analytics/season_schedule.py` with
  the now-confirmed 13↔14 switch (closes its "roadmap input #1").
- **D4.** Update `10_OPEN_QUESTIONS.md` §P1-V1: split into **resolved**
  (2011–2015 scored; 2010 ruleset recovered) and **remaining** (long-TD/pbp and any
  unobserved-stat flags), and record the recovered 2010 ruleset alongside
  `.project-src/dz-rules.csv`.

---

## 3. Avoiding redundant work (what already exists — reuse, don't rebuild)

- **Scoring engine + `verify --sweep` + `rescore`** (`src/ff_pipeline/scoring/`):
  the solve scores *through* the existing engine and validates with the existing
  verify path. No new scoring logic.
- **`nfl_com_points` ground truth**: already persisted per starter-week in
  `team_rosters.extra_data` by the Phase 1 reconstruction — the labels are free.
- **Canonical stat mapping**: `player_stats_raw.stats` already uses the engine's
  keys for 2010–2025 — no re-ingest, no re-mapping.
- **Dashboard `season_schedule.py`**: built config-driven precisely to accept the
  switch year as `_CONFIRMED` — we supply the input, not the mechanism.
- **Dashboard Q5 tiebreaker + era split** (`scored_era` vs `team_record_era`):
  already models the 2016-scored / 2010-record boundary and the tiebreak caveat —
  we extend the scored era backward, we don't rebuild the affordance.
- **Identity/merge work** (§P1-V3/V4): already done; the clean-player matrix
  inherits the resolved `nfl_com_player_id` ↔ nflverse joins.

---

## 4. Known limitations after this plan

- **Long-TD-length bonuses** remain unscored for *all* eras until pbp ingestion
  (§P1-V2) — recovered pre-2016 rulesets verify only to that same ~92% ceiling for
  long-TD games; this is correctness-equivalent to the modern seasons.
- **Pre-rename ownership** before C1 is confirmed is an assumption, not data;
  owner-keyed history is only as good as the user's questionnaire answers.
- **Conference/division** inference is probabilistic; low-confidence eras stay
  flagged until C3 confirmation.
- Any **unobserved stat** (a rule whose stat never occurred in a season) cannot be
  fit and stays at its current-rules default with a flag.

---

## 5. Decision log

- 2026-06-04 — Probe established 2011–2015 already match current rules at the
  §P1-V2 ceiling; only 2010 is a distinct era. Plan re-scoped from "recover six
  seasons of unknown rules" to "confirm five, solve one."
- 2026-06-04 — Chose a **constrained linear solve against `nfl_com_points`** over
  scraping a historical settings page (NFL.com history pages expose final points,
  not the era's rule table — §P1-V1). The data we already hold is a stronger,
  more complete source of truth than any page we could scrape.
- 2026-06-04 — Tiebreakers explicitly **not** re-derived (defer to reconstructed
  `final_rank`, per dashboard Q5).

## 6. Resumable checklist

- [ ] A1 `scripts/distill_scoring_rules.py` (solve + snap + report)
- [ ] A2 2011–2015 confirmed, loaded, rescored, verified (closes §P1-V1 for these)
- [ ] A3 2010 ruleset recovered, loaded, rescored, verified
- [ ] A4 kicker + DST second-pass fit
- [ ] B1 roster-construction templates per season
- [ ] B2 playoff structure/timing + 13↔14 switch confirmed-from-data
- [ ] B3 conference/division map per era (with confidence)
- [ ] B4 tiebreaker handling cross-referenced (no re-derivation)
- [ ] C1–C3 single consolidated questionnaire to the user
- [ ] D1 rescore + verify 2010–2015
- [ ] D2 populate seasons/owners/templates
- [ ] D3 dz-dashboard `_CONFIRMED` switch year set
- [ ] D4 `10_OPEN_QUESTIONS.md` §P1-V1 updated (resolved/remaining split)
