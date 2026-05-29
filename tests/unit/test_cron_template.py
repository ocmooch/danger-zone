"""Cron example sanity check.

The schedule is documented in docs/08_OPERATIONS.md; this test catches
the regression where someone deletes the file or breaks a key entry.
"""

from __future__ import annotations

import re
from pathlib import Path

CRON_PATH = Path(__file__).resolve().parents[2] / "scripts" / "cron.example"

EXPECTED_COMMANDS = ("run", "run --verify", "backup")
PLACEHOLDERS = ("<PROJECT_ROOT>", "<FF_PIPELINE>")
# Plain cron line: m h dom mon dow followed by content.
CRON_LINE_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+")


def test_cron_template_exists() -> None:
    assert CRON_PATH.exists(), f"missing cron template at {CRON_PATH}"


def test_cron_template_lists_run_and_backup() -> None:
    text = CRON_PATH.read_text(encoding="utf-8")
    for cmd in EXPECTED_COMMANDS:
        assert f"<FF_PIPELINE> {cmd}" in text, (
            f"cron template missing scheduled `ff-pipeline {cmd}` entry"
        )


def test_cron_template_uses_documented_placeholders() -> None:
    text = CRON_PATH.read_text(encoding="utf-8")
    for placeholder in PLACEHOLDERS:
        assert placeholder in text, f"cron template missing placeholder {placeholder}"


def test_cron_template_has_at_least_four_schedule_lines() -> None:
    """One backup + at least three sync entries — guards against accidental
    deletion of one of the in-season run rows."""
    lines = [
        ln
        for ln in CRON_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#") and CRON_LINE_RE.match(ln.lstrip())
    ]
    assert len(lines) >= 4, f"expected ≥4 schedule lines, found {len(lines)}: {lines}"


__all__ = [
    "test_cron_template_exists",
    "test_cron_template_has_at_least_four_schedule_lines",
    "test_cron_template_lists_run_and_backup",
    "test_cron_template_uses_documented_placeholders",
]
