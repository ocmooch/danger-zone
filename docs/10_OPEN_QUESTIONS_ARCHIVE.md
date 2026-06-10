# 10 (Archive) — Resolved Questions & Settled Decisions

Items moved out of `10_OPEN_QUESTIONS.md` once they were no longer open.
Archived **2026-05-29** at the **v1.0.0** Phase 1 release. Kept here as a
decision record so follow-up sessions don't re-litigate settled ground.

Still-open items live in `10_OPEN_QUESTIONS.md`.

---

## Confirmed decisions (user-validated, now realized in v1.0.0)

### League is currently active on NFL.com ✓
The crawler targets the current season **and** historical seasons; in-season
scheduled runs and the cookie-refresh workflow are built. Realized in M5 +
the reconstruction path.

### 10+ years of NFL.com history ✓
Resolved by the Phase 1 reconstruction run: standings, matchups, and per-week
lineups are populated for **2010–2025**. The open worry attached to this
decision — "verify NFL.com still serves history pages that far back" — is
now answered: every season 2010–2025 was reachable and reconstructed.

### Scoring + game-time state requirements ✓
Delivered: `scoring_rules` with full bonus support, `player_availability`,
extended `team_rosters` (`acquisition_date`, `drop_date`,
`was_locked_at_kickoff`), and `extra_data` JSON columns. See
`05_SCORING_ENGINE.md` and `04_DATA_MODEL.md`.

## Settled architecture defaults (shipped in v1.0.0)

These were "defaults pending confirmation"; v1.0.0 is built on them, so they
are now decisions, not open questions. Each remains reversible later via the
documented path, but none is an active question.

- **Local-first, cloud-ready** — SQLite + cron + `.env`. Cloud path
  (PostgreSQL via `DATABASE_URL`, vault secrets, hosted scheduler) stays
  documented but untriggered.
- **Python 3.11+** — locked; modern type syntax, Python-native NFL ecosystem.
- **SQLite with PostgreSQL migration path** — `data/fantasy.db`; SQLAlchemy +
  alembic keep the swap to Postgres a connection-string change. Re-evaluation
  of "is SQLite still adequate" moves to the Phase 2 entry review.

## Deferred questions resolved during Phase 1

### Q1. Handling "deleted from NFL.com" data — RESOLVED
Policy implemented (scrape what's available, warn on empty historical pages,
don't fail the run) **and** the "memory dump / run backfill ASAP" decision
point is satisfied: the reconstruction run captured 2010–2025 while the pages
are still served.

### Q2. ID for unknown players — RESOLVED (residuals tracked elsewhere)
The M7 `PlayerResolver` (direct + fuzzy match) and the `player_id_overrides`
table answer the design question; unknowns insert with NULL IDs and score 0
unless raw stats exist. Remaining concrete residuals are tracked as open
items: ambiguous abbreviated names (P1-V3), the end-to-end ID audit (M7-V1),
and the override CLI command (M7-V2).

### Q3. Verifier aggressiveness — RESOLVED
Tolerance is `SCORING_VERIFY_TOLERANCE` (default 0.1), differences logged
without failing the run. `verify --sweep` exercised this across 2016–2025.
A tuning knob now, not an open question.

### Q4. Save raw HTML long-term — RESOLVED
Default **off**; opt in with `SAVE_RAW_HTML=true`. Test-fixture HTML serves as
the small-scale audit trail.

### Q6. Cookie storage (`.env` vs keychain) — RESOLVED
Default `.env`, implemented. A `keyring`-based upgrade in `settings.py`
remains an optional, undriven enhancement.

### Q7. Availability tracking granularity — RESOLVED (as default)
One pre-kickoff row per (player, week); intra-week moves come from the
transaction log. The deeper "capture OWNED / ON_WAIVERS slices" question is
its own open item, M5-V1 (deferred to Phase 2).

### Q8. Off-season sync cadence — RESOLVED
Weekly Sunday sync, wired in cron (`08_OPERATIONS.md`). Adjustable there.

## Phase 1 data gaps resolved after release

### P1-V1. 2010–2015 scoring rules distilled — RESOLVED (2026-06-05)

Rules were *solved from data*, not guessed: every starter-week in
`team_rosters.extra_data` carries NFL.com's actual `nfl_com_points`, so for each
season a labeled `(stat_vector → league_points)` matrix recovers the per-unit
coefficients by least squares, snapped to NFL.com's value vocabulary and
verified back through the real engine. See `scripts/distill_scoring_rules.py`
and `archive/PHASE_PRE2016_PLAN.md`.

Findings:

* **2011–2015 match the current 51-rule set** — confirmed (the solve recovers
  the 2016+ coefficients exactly). Rules loaded (`scoring load --season`),
  rescored, and offline-verified at **89–92% exact-to-cent**, the same long-TD
  ceiling (§P1-V2) as the verified 2016–2025 seasons.
* **2010 is the one distinct era**: **6-point passing TDs + 0.5 PPR (half-PPR)**,
  standardizing to 4-pt / full-PPR from 2011 on. Conclusive (PPR 0 or 1.0 →
  ~19%; pass-TD 4 or 5 → ~82%; the recovered values → 91.5%) and
  **user-confirmed**. Recovered ruleset: `.project-src/dz-rules-2010.csv` (a
  minimal two-line patch of the canonical export). Loaded, rescored, verified.
* **Kicking** is unchanged across all eras (FG-bracket/XP reproduce
  `nfl_com_points` at 100% for 2010–2025).
* **DST**: the real pre-2016 gap was *missing team-defense ingestion*, now
  backfilled (`team-defense --season 2010..2015`) and scored. DST reproduces at
  ~69% (2010–15) vs ~83% (2016–25) — the *same* known DST data-quality tail,
  not a rules difference.

The two residual scoring limits this surfaced remain open in
`10_OPEN_QUESTIONS.md`: the long-TD-length bonuses (§P1-V2) and the
reconstruction team-total trust-check (§P1-V5).
