"""Legacy guard for the sealed NFL.com era (S1).

The 2010-2025 seasons are frozen: SQLite triggers refuse changes to their
irreplaceable columns, ``legacy verify`` compares the live DB with the stored
seal baseline, and ``legacy amend`` is the one audited way to change them.
Design: ``docs/sleeper/03_LEGACY_PRESERVATION.md`` and
``docs/plans/S1-legacy-guard.md``.
"""
