"""Unit tests for the player-identity merge helpers in ``scripts/``.

The merge scripts live outside the ``ff_pipeline`` package, so load the
module by path. We only exercise the pure ``_normalize`` helper here — the
DB-mutating paths are covered by the dry-run/--apply operational flow.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "merge_split_player_identities.py"
_spec = importlib.util.spec_from_file_location("_merge_split", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_normalize = _mod._normalize


def test_leading_single_letter_initial_is_preserved() -> None:
    # Regression: a lone "v"/"i"/"x" leading token is an abbreviated first
    # name, not a generational suffix. Eating it collapsed "V. Cruz" to a
    # single token and defeated the initial+last match path.
    assert _normalize("V. Cruz") == "v cruz"
    assert _normalize("V. Davis") == "v davis"
    assert _normalize("I. Smith") == "i smith"
    assert _normalize("X. Worthy") == "x worthy"


def test_trailing_generational_suffix_is_stripped() -> None:
    assert _normalize("Robert Griffin III") == "robert griffin"
    assert _normalize("Marvin Mims Jr.") == "marvin mims"
    assert _normalize("Frank Gore Sr.") == "frank gore"
    # A "V" suffix mid/trailing is still dropped (e.g. a hypothetical fifth).
    assert _normalize("Pat Henry V") == "pat henry"


def test_plain_two_token_name_unchanged() -> None:
    assert _normalize("Victor Cruz") == "victor cruz"
    assert _normalize("Vernon Davis") == "vernon davis"


def test_diacritics_and_punctuation_folded() -> None:
    assert _normalize("D.J. Moore") == "dj moore"
    assert _normalize("Eddy Piñeiro") == "eddy pineiro"


def test_empty_and_none() -> None:
    assert _normalize(None) == ""
    assert _normalize("") == ""
