"""Committed Sleeper league fixtures must stay pseudonymized (D-11; the repo is public)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "sleeper_league"
FILES = sorted(ROOT.rglob("*.json"))
REAL_LEAGUE_ID = "1393640392104099840"  # D-1, public in docs; must not appear in fixtures
FORBIDDEN_KEYS = re.compile(
    r"^(last_message.*|last_author.*|last_read_id|imported_history.*|p_nick_.*|player_map"
    r"|reactions|group_id|company_id|bracket_id|loser_bracket_id|.*_overrides_id)$"
)


def _walk(node: Any, path: str = "") -> Any:
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def test_fixtures_exist() -> None:
    assert FILES, "tests/fixtures/sleeper_league has no fixtures"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_fixture_is_pseudonymized(path: Path) -> None:
    text = path.read_text()
    assert REAL_LEAGUE_ID not in text
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), f"email-like text in {path.name}"
    assert not re.search(r"https?://", text), f"URL in {path.name}"
    for long_id in re.findall(r"\d{15,}", text):
        assert long_id.startswith("9000"), f"non-fake 15+ digit id in {path.name}"
    for where, key, value in _walk(json.loads(text)):
        assert not FORBIDDEN_KEYS.match(str(key)), f"forbidden key {where}.{key}"
        if key == "display_name":
            assert re.fullmatch(r"Manager \d{2}", str(value)), f"{where}.display_name"
        if key == "team_name":
            assert value == "" or re.fullmatch(r"Team \d{2}", str(value)), f"{where}.team_name"
        if key == "avatar":
            assert value is None, f"{where}.avatar"
        if key == "name" and where == "":
            assert value == "Fixture League"
