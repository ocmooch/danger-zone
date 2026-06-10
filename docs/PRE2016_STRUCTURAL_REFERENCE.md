# Pre-2016 Structural Reference (data-distilled)

**Status:** confirmed-from-data · **Produced:** 2026-06-05 · Workstream **B** of
`docs/archive/PHASE_PRE2016_PLAN.md`.

Every finding below was distilled from already-populated tables (`team_rosters`,
`matchups`, `seasons`, `teams`) — no user input required. Confidence is **high**
for all four: each is a direct count over the reconstructed data, cross-checked
against a second source where one exists. This artifact is the reference the
dz-dashboard consumes (roster templates, season-length switch, division map).

---

## B1 — Starting-lineup template per era

Derived from `team_rosters.roster_slot` counts (week 1, per team). Three eras:

| Era | QB | RB | WR | TE | Flex | K | DEF | Starters |
|---|---|---|---|---|---|---|---|---|
| **2010** | 1 | 2 | **3** | 1 | — (no flex) | 1 | 1 | 8 |
| **2011–2015** | 1 | 2 | 2 | 1 | **1 × W/R** (RB/WR) | 1 | 1 | 8 |
| **2016–2025** | 1 | 2 | 2 | 1 | **1 × R/W/T** (RB/WR/TE) | 1 | 1 | 8 |

**Correction to the plan's assumption:** the plan expected the W/R flex to
appear in **2013**. The data shows it league-wide from **2011** — every team's
week-1 lineup carries a `W/R` slot (and drops from 3 WR to 2 WR) starting 2011.
The flex *widens* to `R/W/T` (TE-eligible) in **2016**; `RES`/`Bench` slots are
non-starters and unchanged.

Switch points: **2011** (add W/R flex, 3WR→2WR) · **2016** (W/R → R/W/T).

## B2 — Playoff structure & season length (the 13↔14 switch)

From `seasons.regular_season_weeks` / `playoff_weeks`, cross-checked against the
`matchups` graph (last non-playoff week, first playoff week, playoff row count):

| Era | Regular weeks | Playoff weeks | First PO week | PO matchup-rows |
|---|---|---|---|---|
| **2010** | **14** | 3 (W15–17) | 15 | **14** (smaller bracket) |
| **2011–2020** | **13** | 3 (W14–16) | 14 | 28 |
| **2021–2025** | **14** | 3 (W15–17) | 15 | 28 |

**The 2010 = 14-regular-week value is confirmed, not a reconstruction
artifact.** The plan flagged it as possibly an artifact of deriving the boundary
from the champion's game count; the independent `matchups` graph resolves it —
2010's last non-playoff week is genuinely 14, with playoffs in weeks 15–17. 2010
*does* carry a smaller playoff bracket (14 playoff team-rows vs 28 in every other
season), consistent with a reduced/no-consolation format that year.

**13↔14 switch years (dashboard "roadmap input #1"):** **2011** (14→13) and
**2021** (13→14). Confirmed from two independent sources. Set `_CONFIRMED` in the
dashboard's `analytics/season_schedule.py`.

## B3 — Conferences / divisions

**None, in any era.** There is no `division`/`conference` column on `teams`, and
the matchup graph is a **full round-robin every season**: each team faces a
minimum of 11 distinct opponents (all other 11 teams) in 2010–2025, with no
intra-group clustering that a divisional schedule would produce. This matches the
NFL.com setting `Divisions: No`. High confidence; **not a Phase C question.**

## B4 — Tiebreakers (cross-reference only — not re-derived)

Per the plan, tiebreakers are **not** re-derived here. Phase 1's reconstructed
`teams.final_rank` is populated for all 192 team-seasons (16 × 12) and already
*bakes in* the historical tiebreak NFL.com applied. The league's standings
tiebreaker setting is **Points For**. The dashboard's Q5 resolution prefers
`final_rank` and flags a `tiebreak_caveat` only for *computed* pre-2019 ranks;
that affordance stands unchanged. No work beyond this cross-reference.

---

## What this leaves for the user (Phase C)

B1–B4 are fully resolved from data. The only structural item that the data
**cannot** reveal is **team/manager ownership** across renames (the `owners`
table has names but empty `joined_year`/`left_year`/`aliases`) and **keeper/draft
rights**. Those — and only those — go to the Phase C questionnaire.
