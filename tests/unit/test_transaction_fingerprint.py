"""Unit tests for the transaction upsert fingerprint's ``extra_data`` signature.

The dedup fingerprint keys on (type, team, player, direction, executed_at) plus
``_extra_sig`` — the latter exists because two row families carry their only
distinguishing content in ``extra_data``, not those columns:

* lineup moves differ only by slot;
* setting/commish rows have null team/player/direction, so the change
  description is what makes same-minute actions distinct (league setup fires
  dozens in one minute — without this, ~83% of the commish diary collapses).
"""

from __future__ import annotations

from ff_pipeline.crawlers.nfl_com.league import _extra_sig


def test_setting_rows_differ_by_description() -> None:
    # Two commish actions in the same minute (same null columns, same
    # timestamp) are distinguished only by their description.
    a = _extra_sig({"raw_type": "lm", "description": "harry updated playoff teams"})
    b = _extra_sig({"raw_type": "lm", "description": "harry DOOKS Player Adds Count"})
    assert a != b
    assert a is not None and a[0] == "setting"


def test_identical_setting_rows_share_signature() -> None:
    # Genuine duplicates (e.g. a sweep page-boundary overlap) must still
    # collapse, so the signature is stable for identical content.
    payload = {"raw_type": "lm", "description": "harry updated playoff teams"}
    assert _extra_sig(dict(payload)) == _extra_sig(dict(payload))


def test_setting_rows_differ_by_from_to() -> None:
    base = {"raw_type": "lm", "description": "changed scoring"}
    assert _extra_sig({**base, "from": "6", "to": "4"}) != _extra_sig(
        {**base, "from": "6", "to": "0"}
    )


def test_lineup_rows_keyed_by_slot_move() -> None:
    sig = _extra_sig({"from_slot": "R/W/T", "to_slot": "BN"})
    assert sig == ("slot", "R/W/T", "BN")
    # A different slot move is a different signature; slot wins over any
    # description key that might also be present.
    assert sig != _extra_sig({"from_slot": "BN", "to_slot": "R/W/T"})


def test_no_extra_data_has_no_signature() -> None:
    # Add/drop/trade rows carry their identity in the real columns, so they
    # contribute nothing here.
    assert _extra_sig(None) is None
    assert _extra_sig({}) is None
