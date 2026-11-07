"""Guard trigger DDL: the frozen migration copy must equal the generator."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from ff_pipeline.legacy.spec import GUARDED_TABLES, GuardKind
from ff_pipeline.legacy.triggers import trigger_ddl, trigger_names

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "e2a4c6e8f0b1_s1_legacy_guard.py"
SEAL_V1 = ROOT / "docs" / "sleeper" / "seals" / "seal-v1.manifest.json"


def _migration_triggers() -> list[tuple[str, str]]:
    spec = importlib.util.spec_from_file_location("s1_legacy_guard", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.TRIGGERS)


def test_migration_ddl_equals_generator() -> None:
    assert _migration_triggers() == trigger_ddl()


def test_every_guarded_table_has_its_triggers() -> None:
    names = set(trigger_names())
    assert len(names) == len(trigger_names()), "duplicate trigger names"
    events = {
        GuardKind.SEASON: ("insert", "update", "delete"),
        GuardKind.SEASON_ROW: ("update", "delete"),
        GuardKind.REGISTERED: ("update", "delete"),
    }
    for table in GUARDED_TABLES:
        for event in events[table.kind]:
            assert f"legacy_guard_{table.name}_{event}" in names
    for table in ("legacy_amendments", "legacy_seals"):
        assert {f"legacy_guard_{table}_update", f"legacy_guard_{table}_delete"} <= names
    for event in ("insert", "update", "delete"):
        assert f"legacy_guard_legacy_sealed_rows_{event}" in names


def test_spec_matches_seal_v1_manifest() -> None:
    manifest = json.loads(SEAL_V1.read_text())
    assert {t.name for t in GUARDED_TABLES} == set(manifest["tables"])
    for table in GUARDED_TABLES:
        assert list(table.legacy_columns) == manifest["tables"][table.name]["legacy_columns"]


def test_update_guards_compare_every_legacy_column() -> None:
    ddl = dict(trigger_ddl())
    for table in GUARDED_TABLES:
        sql = ddl[f"legacy_guard_{table.name}_update"]
        for col in table.legacy_columns:
            assert f"NEW.{col} IS NOT OLD.{col}" in sql
    for col in ("is_sealed", "sealed_at", "seal_id"):
        assert f"NEW.{col} IS NOT OLD.{col}" in ddl["legacy_guard_seasons_update"]
