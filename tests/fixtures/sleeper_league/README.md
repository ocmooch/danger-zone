# Sleeper league fixtures (S2, D-11)

Built by `scripts/sleeper/build_fixtures.py` from the archived 2026 capture. Pseudonymized:
fake ids (`9000…`), `Manager NN` / `Team NN`, no avatars, chat, nicknames or import metadata.
File names = `PayloadRequest.fixture_name` (read with `FixtureSleeperLeagueSource`).

- `season_2026/` — the real season to date (league id `900000000000000001`), trimmed:
  3 transactions per (type, status) per week; stats only for players in that week's
  matchups and only scoring-relevant keys; players directory only for referenced players.
  Covers: regular weeks, bye weeks, failed + complete waivers, free-agent adds, a blank team
  name, the DEF slot, IR (reserve) players, pre-generated brackets, a snake draft.
- `synthetic/` — files that **replace** their `season_2026/` namesake for scenarios the league
  has not produced yet; every changed object carries `"_synthetic": true`:
  a trade with a draft pick + FAAB (week 1 transactions), commissioner `custom_points`
  (week 1 matchups), a co-owner (rosters).
- Not built yet: a playoff week (captured for real after week 15; bracket semantics are
  unverified until games are decided, so no synthetic version is made).
