"""S9 guard v2 migration on a sealed DB: additive, reversible, seal-v1 untouched."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from ff_pipeline.legacy.triggers import trigger_names, trigger_names_v2
from tests.integration.test_legacy_guard import _build as build_sealed_db
from tests.integration.test_migration_s3 import _downgrade, _triggers, _upgrade, _verified

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def test_guard_v2_round_trip_keeps_seal_v1(tmp_path: Path) -> None:
    db = build_sealed_db(tmp_path)  # at head: v1 + v2 triggers
    assert set(trigger_names()) | set(trigger_names_v2()) <= _triggers(db)
    assert _verified(db)
    _downgrade(db, "b8d0f2a4c6e3")
    assert not set(trigger_names_v2()) & _triggers(db)
    assert set(trigger_names()) <= _triggers(db)  # the S1 guard survives
    assert _verified(db)  # a seal-v1 baseline does not need v2
    _upgrade(db)
    assert set(trigger_names_v2()) <= _triggers(db)
    assert _verified(db)


def test_update_is_a_no_op_until_auto_load_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ff_pipeline.cli import app
    from ff_pipeline.settings import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("SLEEPER_AUTO_LOAD", "false")
    get_settings.cache_clear()
    try:
        out = CliRunner().invoke(app, ["sleeper-league", "update", "--if-enabled"])
    finally:
        get_settings.cache_clear()
    assert out.exit_code == 0, out.output
    assert "off (SLEEPER_AUTO_LOAD is false)" in out.output
