"""Smoke test — verifies the package imports cleanly."""

from __future__ import annotations

import importlib


def test_package_imports() -> None:
    import ff_pipeline

    assert ff_pipeline.__version__


def test_subpackages_import() -> None:
    for mod in (
        "ff_pipeline.api",
        "ff_pipeline.crawlers",
        "ff_pipeline.crawlers.nfl_com",
        "ff_pipeline.crawlers.nflverse",
        "ff_pipeline.crawlers.sleeper",
        "ff_pipeline.normalizer",
        "ff_pipeline.repository",
        "ff_pipeline.scoring",
    ):
        importlib.import_module(mod)
