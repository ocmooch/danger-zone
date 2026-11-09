"""Legacy-vault archive retention (S0).

The fingerprint serializer tests moved to ``test_legacy_fingerprint.py``, which
runs them against both this script and ``ff_pipeline.legacy.fingerprint``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

VAULT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "vault"


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(VAULT_DIR))
    spec = importlib.util.spec_from_file_location(name, VAULT_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync = _load("vault_sync")


def test_retention_pins_seal_v1_and_keeps_four_newest() -> None:
    seal = "dz-vault_seal-v1_20261107_aaaaaaaa.tar.gz.age"
    snaps = [f"dz-vault_snap_202611{d:02d}_bbbbbbbb.tar.gz.age" for d in range(8, 15)]
    keep, delete = sync.plan_retention([seal, *snaps, "README.md", ".x.partial"])
    assert seal in keep
    assert len(keep) == 5
    assert keep == sorted([seal, *snaps[-4:]])
    assert delete == sorted(snaps[:3])


def test_retention_never_deletes_when_under_limit() -> None:
    names = [
        "dz-vault_seal-v1_20261107_aaaaaaaa.tar.gz.age",
        "dz-vault_snap_20261108_bbbbbbbb.tar.gz.age",
    ]
    assert sync.plan_retention(names) == (sorted(names), [])


# ── evergreen fallback copy (05 §10) ─────────────────────────────────────────

EVERGREEN_OK = {
    sync.EVERGREEN_FILE: sync.EVERGREEN_SIZE,
    **dict.fromkeys(sync.EVERGREEN_SIDECARS, 1),
}


def test_evergreen_listing_ok() -> None:
    assert sync.check_listing(EVERGREEN_OK) == []


def test_evergreen_listing_flags_missing_extra_and_resized() -> None:
    names = {**EVERGREEN_OK, "dz-vault_snap_20261109_cccccccc.tar.gz.age": 5}
    del names["README.txt"]
    names[sync.EVERGREEN_FILE] = 1
    problems = sync.check_listing(names)
    assert any(p.startswith("MISSING README.txt") for p in problems)
    assert any(p.startswith("UNEXPECTED") for p in problems)
    assert any(p.startswith("SIZE CHANGED") for p in problems)


def test_evergreen_folder_detects_changed_and_unlocked(tmp_path: Path) -> None:
    for name in sync.EVERGREEN_SIDECARS:
        (tmp_path / name).write_text("x")
    archive = tmp_path / sync.EVERGREEN_FILE
    archive.write_bytes(b"\0" * sync.EVERGREEN_SIZE)  # right size, wrong content, writable
    problems = sync.check_folder(tmp_path, check_locks=False)
    assert any(p.startswith("CONTENT CHANGED") for p in problems)
    assert any(p.startswith("READ-ONLY attribute removed") for p in problems)
    assert sync.check_folder(tmp_path / "gone", check_locks=False)[0].startswith("FOLDER MISSING")


def test_evergreen_folders_are_never_sync_destinations() -> None:
    assert sync.EVERGREEN_DRIVE != sync.DRIVE_REMOTE
    assert sync.EVERGREEN_ICLOUD != sync.ICLOUD_DIR
    assert sync.EVERGREEN_LOCAL.resolve() != sync.VAULT.resolve()
    assert "evergreen" not in str(sync.OUTBOX)
