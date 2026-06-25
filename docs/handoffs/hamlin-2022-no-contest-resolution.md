# Handoff → danger-zone (ff-pipeline): 2022 championship "Damar Hamlin" no-contest resolution

**Repo:** `/home/mainuser/danger-zone`  ·  **DB:** `data/fantasy.db` (SQLite)
**Paired dashboard handoff:** `dz-dashboard/docs/handoffs/hamlin-2022-championship-context.md`
(do the upstream work here **first**; the dashboard work consumes the provenance contract below).

## Context

The 2022 fantasy championship is stored with the **wrong champion** because the NFL Week 17
Bills@Bengals game (Jan 2, 2023) was suspended after Damar Hamlin's cardiac arrest and officially
ruled a **no-contest — never resumed, never rescheduled** (NFL.com / ESPN / FOX Sports). The
affected BUF/CIN starters have **no nflverse Week-17 stat line**; nfl.com fantasy stamped them
`0.0`, which leaves the title game reading CMC 89.0 def. Smokin Doubs 74.9 and records CMC as champion.

The league resolved the no-contest by, for each affected player, taking **the stats they accrued in
the suspended Week-17 game before play stopped, PLUS their NFL Week-19 (Wild Card, Jan 14–15 2023)
game** — i.e. `final = wk17_partial + wk19`. **Week 18 was deliberately skipped** (for fairness:
some players rested/were injured/ineligible in Wk18). The cancelled game's partial play **is
included** — it is not discarded.

**Source of truth = public data, not any private note.** A recovered league note ("keeping track of
all the points… Allen 23.08, Davis 24.3, Bass 10.0, Burrow 21.26, Boyd 5.6, Chase 23.4, Higgins
9.7") matches the pipeline's **Week-19** `player_stats_scored.total_points` exactly — which is the
tell that **the note records the Wk19 *add-on* component**, not the final figure (the finals are
higher by each player's Wk17 partial). The note is therefore corroboration only, and it is
**incomplete** — a derivation against public data finds more affected players than it lists (Stefon
Diggs, Joe Mixon, Buffalo Bills DEF, Dawson Knox, Devin Singletary, James Cook, Hayden Hurst, Samaje
Perine). Anchor the fix on **derived, verifiable public stat lines** (Wk17 partial + Wk19).

**Sourcing the Wk17 partial (the hard part).** nflverse *voided* the no-contest from its weekly
player-stats rollup, so `player_stats_raw`/`player_stats_scored` have **no 2022 wk17 rows** for these
players. But the ~9 minutes of live plays are retained in nflverse **play-by-play** (game
`2022_17_BUF_CIN`). Aggregate the partial per player from pbp (passing/rushing/receiving/kicking),
league-score it, and add it to Wk19. Cross-check against the contemporaneous box score
(Burrow 3/3 + 14-yd TD to Boyd + 13-yd to Higgins; Bass 25-yd FG; Allen 3/6, 36 yds — Bengals 7-3).
If pbp coverage for this game is missing, fall back to the box-score reconstruction and document it.

This handoff asks you to encode that resolution as a **deterministic override that re-applies on
every ingest/score** (so a re-scrape never reverts it), mirroring the existing relocation/DST
override precedent (`franchises.py` root-fix + re-score).

## Verified facts (reproduce read-only against `data/fantasy.db`)

Season 2022 = `season_id 14`. Title game = matchup `2635/2636`, week 17:
`CMC Rules Everything Around Me (team 160) 89.0` vs `Smokin Doubs (team 165) 74.9`;
`seasons.champion_team_id=160`, `runner_up_team_id=165`; `teams.final_rank/playoff_finish` CMC=1, Doubs=2.

**Derive the affected set from public data — do not use the ledger as the list.** The authoritative
rule: any player **rostered** in 2022 wk17 who has **no `player_stats_raw` row for 2022 wk17** but
**does** have a 2022 wk19 row on **BUF or CIN** (i.e. their wk17 game was the cancelled no-contest):

```sql
with affected as (
  select distinct tr.player_id from team_rosters tr
  where tr.season_year=2022 and tr.week=17
    and not exists (select 1 from player_stats_raw r
                    where r.player_id=tr.player_id and r.season_year=2022 and r.week=17)
    and exists (select 1 from player_stats_raw r19
                where r19.player_id=tr.player_id and r19.season_year=2022 and r19.week=19
                  and r19.nfl_team in ('BUF','CIN'))
)
select * from affected;
```
This correctly **excludes Zack Moss** (16993 — he has a real wk17 Colts row) and **includes** the
players the ledger omitted. The ledger's 7 (below) are a verified subset, not the whole:

The **Wk19 add-on** component (= the league note, to the penny) — the **final** for each player is
this *plus* their Wk17 partial:

| player_id | Player | Pos | Wk19 game | Wk19 add-on | Wk17 partial (from box score) |
|-----------|--------|-----|-----------|-------------|-------------------------------|
| 4236 | Joe Burrow | QB | CIN vs BAL | 21.26 | 3/3, 1 pass TD (Boyd) + 13-yd to Higgins |
| 6328 | Gabe Davis | WR | BUF vs MIA | 24.30 | (TBD from pbp — not individually reported) |
| 2331 | Tyler Bass | K | BUF vs MIA | 10.00 | 25-yd FG (≈3.0) |
| 10930 | Tee Higgins | WR | CIN vs BAL | 9.70 | 1 rec, 13 yds (≈2.3) |
| 1413 | Josh Allen | QB | BUF vs MIA | 23.08 | 3/6, 36 pass yds (≈1.4) |
| 3291 | Tyler Boyd | WR | CIN vs BAL | 5.60 | 1 rec, 14 yds, 1 TD (≈8.4) |
| 4933 | Ja'Marr Chase | WR | CIN vs BAL | 23.40 | (TBD from pbp) |

> The 7 above are only the ledger-listed subset. The query also returns **Stefon Diggs, Joe Mixon,
> Buffalo Bills DEF** (affected starters on other teams) and **Dawson Knox, Devin Singletary, James
> Cook, Hayden Hurst, Samaje Perine** (bench). Use the **query result**, not this table. Get exact
> Wk17 partials per player from nflverse pbp (`2022_17_BUF_CIN`).

Approx title-game effect (Wk19 add-on + Wk17 partial; partials pending exact pbp):
Doubs 74.9 + Burrow(~5.5+21.26) + Bass(3.0+10.0) + Gabe Davis(?+24.3) ≈ **~139**;
CMC 89.0 + Tee Higgins(2.3+9.7) ≈ **~101** → **Smokin Doubs champion by ~38** (margin only grows vs
the Wk19-only lower bound of 130.46–98.7). Outcome is not sensitive to the small partials.

### All affected week-17 matchups (substitute in every one, regardless of importance)

Apply Week-19 substitution to **every** affected slot league-wide. Starters (`S`) move the team
total; bench (`b`) is display-only. Derived map:

Placement labels are the dashboard's own (`analytics/bracket.py` `season_bracket()`); **every**
final-round placement game has ≥1 affected player. **Bold** = the meaningful games the user flagged.

| Placement | matchup | teams (current scores) | affected players | outcome |
|-----------|---------|------------------------|------------------|---------|
| **Championship** | 2635/2636 | CMC 89.0 / **Smokin Doubs 74.9** | CMC: Higgins `S` · Doubs: Bass/Burrow/Gabe Davis `S` | **FLIPS → Doubs** |
| **3rd Place** | 2643/2644 | Ice Station Zebra 101.7 / Smokin' AJ 70.86 | Zebra: Chase `S` · AJ: Allen `S`, Boyd `S`, Knox `b` | no flip (both rise) |
| 5th Place | 2645/2646 | House of the Droggenburg 114.32 / Smokin Dabolls 159.38 | Droggenburg: Mixon `S`, Perine `b` · Dabolls: Cook `b`, Hurst `b` | no flip |
| **7th Place** (consolation winner) | 2637 | Younghoe Kooloo 119.56 / King Henry 105.42 | Younghoe: Singletary `b` | no flip (bench only) |
| 9th Place | 2639 | So What-No Fuckin Ziti 81.2 / Grim SiLLeeper 79.2 | Ziti: Diggs `S` | no flip (leader rises) |
| **11th Place** | 2641 | Robra Kai 118.88 / Feast of FIES 147.68 | Robra Kai: Buffalo Bills DEF `S` | no flip |

**Per current data only the championship outcome flips** — every other affected starter sits on the
side already winning, and the rest are bench-only. So `final_rank`/`playoff_finish` should change
**only for CMC (1→2) and Doubs (2→1)**. **Recompute all starter sums (now Wk17-partial + Wk19) to
confirm**; the small partials don't threaten any of the non-championship margins, but verify and
enumerate any surprise flip before finalizing.

## The resolution rule

For `season_year=2022, week=17`, for every roster slot holding an affected player, the substitute
score is **`wk17_partial + wk19`**: the stats accrued in the suspended Week-17 game before play
stopped (sourced from nflverse pbp `2022_17_BUF_CIN`) **plus** the player's Week-19 2022 stat line,
summed and league-scored. **Week 18 is skipped.** The Week-17 partial is **included, not discarded.**
Apply **league-wide** (not just the title game).

## Required changes

1. **New override module** (e.g. `ff_pipeline/.../overrides/hamlin_2022_wk17.py`) invoked after
   normalize+score, declaratively encoding the rule. Keep it idempotent and reproducible across
   re-ingest (precedent: `franchises.py`). For each affected player, build the combined stat line
   = (Wk17 partial aggregated from pbp `2022_17_BUF_CIN`) + (their Wk19 raw line), then translate
   raw→points through the pure `scoring` engine (`docs/05_SCORING_ENGINE.md`) — do not copy
   nflverse weekly scored values (the Wk19-only match is the *add-on*, not the final).
2. **Keep `player_stats_raw` / `player_stats_scored` for 2022 wk17 honest re: the official record** —
   nflverse voids the no-contest, so don't fabricate a "real" wk17 weekly row. The substitution
   lives at the roster/matchup/season layer, flagged with provenance (below). (The pbp partial is
   raw input to the override, not a claim that the weekly game counted.)
3. **`team_rosters.extra_data`** for each affected wk17 slot — write the provenance contract the
   dashboard will read (coordinated shape — do not diverge):
   ```json
   "hamlin_substitute": {
     "basis": "no_contest_wk17partial_plus_wk19",
     "league_points": 26.8,
     "wk17_partial": { "raw_stats": {...}, "points": 5.54 },
     "wk19":         { "raw_stats": {...}, "points": 21.26 },
     "points_breakdown": { ...combined stat→points breakdown... }
   }
   ```
   `league_points` = wk17_partial + wk19 (the final). Also set `extra_data.nfl_com_points` =
   `league_points` so existing readers (dashboard `_authoritative_points`) sum to the corrected
   team total without special-casing.
4. **`matchups.team_score` / `opponent_score`** — recompute for every affected week-17 matchup from
   the corrected **starter** sums.
5. **Standings** — re-derive `teams.final_rank` / `playoff_finish` for every team whose bracket
   result changes; then `seasons.champion_team_id` (160→**165**), `runner_up_team_id` (165→**160**),
   `last_place_team_id` only if affected.
6. **Bump `pipeline_runs`** (re-score) so the dashboard `AnalyticsCache` (keyed on run id) invalidates.

## Scope — league-wide substitution, championship-only flip (verify)

Substitute in **every** affected week-17 slot (see the matchup map above), but per the current
read-only analysis the only **outcome** that changes is the championship; all other affected
starters sit on the already-winning side. So the standings blast radius should be just CMC (1→2)
and Doubs (2→1). **Recompute every affected starter sum to confirm this still holds** after the
override, and if any non-championship game flips, record it in the PR before finalizing — nothing
should flip silently.

## Done when

- Title game recomputes to **Doubs (165) > CMC (160)** (≈139 vs ≈101 once partials are added;
  exact pending pbp); `champion_team_id=165`, `runner_up_team_id=160`; affected
  `final_rank`/`playoff_finish` swapped (and any league-wide rank shifts enumerated).
- Each affected wk17 slot carries `hamlin_substitute` provenance with **both** `wk17_partial` and
  `wk19` components + corrected `nfl_com_points` (= their sum).
- No fabricated "official" 2022-wk17 weekly stat rows in `player_stats_raw`/`player_stats_scored`
  (the partial is pbp-derived override input only).
- The override re-applies cleanly on a fresh ingest+score (idempotent; verified by re-running).

## Verification queries (read-only)

```sql
-- champion + runner-up now Doubs/CMC
SELECT champion_team_id, runner_up_team_id FROM seasons WHERE season_id=14;   -- 165, 160
-- title-game scores corrected (Doubs now > CMC)
SELECT matchup_id, team_id, team_score, opponent_team_id, opponent_score
FROM matchups WHERE matchup_id IN (2635,2636);
-- provenance present (both components) on the four title-game slots
SELECT team_id, player_id,
       json_extract(extra_data,'$.hamlin_substitute.wk17_partial.points') AS partial,
       json_extract(extra_data,'$.hamlin_substitute.wk19.points')         AS wk19,
       json_extract(extra_data,'$.hamlin_substitute.league_points')       AS final
FROM team_rosters WHERE season_year=2022 AND week=17 AND player_id IN (4236,6328,2331,10930);
```

Deliver as `feature/hamlin-2022-no-contest-resolution` → `dev`. Commit trailers
`AI-Model` / `Prompted-By` / `Reviewed-By`; never `Co-Authored-By: Claude`.
