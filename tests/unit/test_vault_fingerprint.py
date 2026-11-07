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
