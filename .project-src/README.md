# Phase 1 Handoff Package — Fantasy Football Data Platform

This is a complete planning and starter package for **Phase 1: Data Aggregation Infrastructure** of your personal fantasy football management system. Everything here is designed to be handed directly to Claude Code with minimal further research or design work needed.

## What's in this package

```
phase1_handoff/
├── README.md                          ← you are here
├── CLAUDE_CODE_KICKOFF.md             ← copy/paste this into Claude Code as the first message
├── docs/
│   ├── 01_SPEC.md                     ← functional & non-functional requirements
│   ├── 02_ARCHITECTURE.md             ← system design, module boundaries, data flow
│   ├── 03_DATA_SOURCES.md             ← which APIs/sites to use and why (with fallbacks)
│   ├── 04_DATA_MODEL.md               ← database schema, entity definitions, conventions
│   ├── 05_SCORING_ENGINE.md           ← how league scoring rules translate raw stats → points
│   ├── 06_API_CONTRACT.md             ← endpoints Phase 2 & 3 will consume
│   ├── 07_TESTING_STRATEGY.md         ← unit, integration, data-quality, regression tests
│   ├── 08_OPERATIONS.md               ← scheduling, secrets, logging, backup
│   ├── 09_ROADMAP.md                  ← milestone-by-milestone implementation order
│   └── 10_OPEN_QUESTIONS.md           ← decisions deferred, with my recommendations
└── starter/
    ├── .env.example                   ← all required environment variables
    ├── pyproject.toml                 ← exact dependency pins
    ├── README.md                      ← repo-level README the project will use
    ├── .gitignore
    └── prerequisites.md               ← what YOU must do before kicking off Claude Code
```

## Read in this order

1. **`starter/prerequisites.md`** — there are a handful of things only you can do (get cookies, find your league ID, etc.). Do these first.
2. **`docs/01_SPEC.md`** through **`docs/10_OPEN_QUESTIONS.md`** — skim or read end-to-end depending on appetite.
3. **`CLAUDE_CODE_KICKOFF.md`** — when ready, copy this into a new Claude Code session.

## Why this design

The single most important insight from the research phase: **NFL.com's fantasy platform has no public, supported API for private league data.** Every working approach uses session cookies copied from a logged-in browser, and the league owner (you) is the only person who can produce them. This shapes everything downstream:

- The crawler must authenticate via a one-time cookie capture (no programmatic login — that risks captcha and ToS issues).
- The official NFL Players/Stats portion of `api.fantasy.nfl.com` is **public, no-auth** and remains usable for player-level data and projections.
- League-private data (rosters, matchups, history) is scraped from `fantasy.nfl.com` HTML pages with a user cookie.
- Authoritative NFL statistical data should come from **nflverse** (via `nflreadpy`) — not scraped — because they maintain it nightly, license it CC-BY 4.0, and have years of stability behind them.
- **Sleeper API** is the best supplementary source for projections and trending players (free, no auth, rate-limited).

This architecture splits cleanly into three crawlers (NFL.com private, NFL.com public, nflverse) plus a normalization layer, making each one independently testable and replaceable when (not if) NFL.com changes its DOM.

## What "done" looks like for Phase 1

A single command — `ff-pipeline run` — that:
1. Pulls fresh league data from NFL.com (with your cookie) — including the league-wide players page for waiver/availability state
2. Pulls fresh player stats from nflverse
3. Pulls latest projections from Sleeper
4. Normalizes, deduplicates, and joins them into a unified SQLite database
5. Recomputes every fantasy point total using **your** league's scoring rules
6. **Captures game-time state**: who was on each team's roster, who was a free agent, who was on waivers, and when each player was added/dropped
7. Exposes a FastAPI service Phase 2 will consume

Plus: backfill of every historical season (10+ years), test coverage on the scoring engine, scheduled (cron) runs during the season, schema extensibility for new sections, and clear documentation of what to do when NFL.com changes its HTML.

## Confirmed user requirements

The user has explicitly confirmed:
- League is **currently active on NFL.com (ongoing)**
- **10+ years of history**, all on NFL.com
- Scoring features needed: **standard + PPR variants**, **custom bonuses** (long TDs, yardage tiers)
- Must capture **player waiver status at game time** (Owned / Free Agent / On Waivers)
- Must capture **player roster spot at game time** (Starter / Bench / IR)
- Must capture **date added to / dropped from team**
- Schema must be **extensible to new sections** as available data is discovered
