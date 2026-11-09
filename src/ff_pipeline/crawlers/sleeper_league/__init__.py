"""Sleeper league capture (S2): raw league payloads archived before parsing.

Separate from ``crawlers.sleeper`` (public projections / trending / players),
which is unchanged. Layout:

* ``endpoints`` — request types and the per-run request plan (pure).
* ``client``    — ``SleeperLeagueSource`` seam: live, fixture, archive replay.
* ``lineage``   — ``previous_league_id`` walk.
* ``capture``   — one capture run → ``archive.raw_store``.
* ``cli``       — ``ff-pipeline sleeper-league capture|lineage|archive``.
"""
