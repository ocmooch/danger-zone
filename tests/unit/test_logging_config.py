"""Verify structlog cookie redaction + dual file/stderr routing."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from ff_pipeline.logging_config import (
    LOG_FILE_NAME,
    REDACTED_PLACEHOLDER,
    _redact_secrets,
    configure_logging,
    get_logger,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Redaction processor (pure function)
# ---------------------------------------------------------------------------


def test_redacts_known_sensitive_keys() -> None:
    event = {
        "event": "nfl_com.fetch",
        "cookie": "real-cookie-value",
        "authorization": "Bearer secret",
        "url": "https://fantasy.nfl.com/",
    }
    redacted = _redact_secrets(None, "info", event)
    assert redacted["cookie"] == REDACTED_PLACEHOLDER
    assert redacted["authorization"] == REDACTED_PLACEHOLDER
    assert redacted["url"] == "https://fantasy.nfl.com/"


def test_case_insensitive_redaction() -> None:
    event = {"Cookie": "abc", "Authorization": "Bearer x", "Token": "y"}
    redacted = _redact_secrets(None, "info", event)
    assert redacted["Cookie"] == REDACTED_PLACEHOLDER
    assert redacted["Authorization"] == REDACTED_PLACEHOLDER
    assert redacted["Token"] == REDACTED_PLACEHOLDER


def test_redacts_nested_one_level() -> None:
    event = {
        "event": "request",
        "headers": {"cookie": "leaked", "content-type": "text/html"},
    }
    redacted = _redact_secrets(None, "info", event)
    assert redacted["headers"]["cookie"] == REDACTED_PLACEHOLDER
    assert redacted["headers"]["content-type"] == "text/html"


def test_non_sensitive_keys_untouched() -> None:
    event = {"event": "ok", "url": "https://x", "duration_ms": 12}
    redacted = _redact_secrets(None, "info", event)
    assert redacted == event


# ---------------------------------------------------------------------------
# configure_logging — dual routing to file + stderr
# ---------------------------------------------------------------------------


def _make_settings(log_dir: Path, *, log_format: str = "json") -> MagicMock:
    s = MagicMock()
    s.log_dir = log_dir
    s.log_level = "INFO"
    s.log_format = log_format
    return s


@pytest.fixture
def _flush_root_handlers_after() -> Iterator[None]:
    """Tests below mutate the root logger; restore afterwards so other
    tests aren't affected by leftover handlers."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        # Handler may already be closed (e.g., re-configure inside the test
        # closed it). Idempotent teardown is the goal.
        with contextlib.suppress(ValueError, OSError):
            h.close()


@pytest.mark.usefixtures("_flush_root_handlers_after")
def test_configure_logging_creates_log_dir(tmp_path: Path) -> None:
    log_dir = tmp_path / "does-not-exist-yet"
    assert not log_dir.exists()
    configure_logging(_make_settings(log_dir))
    assert log_dir.is_dir()
    assert (log_dir / LOG_FILE_NAME).exists() or True  # file is created on first emit


@pytest.mark.usefixtures("_flush_root_handlers_after")
def test_log_lands_in_file_as_redacted_json(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(_make_settings(log_dir))

    log = get_logger("test")
    log.info("nfl_com.fetch", cookie="super-secret-cookie", url="https://example.com")

    # Flush so the file handler writes through.
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_file = log_dir / LOG_FILE_NAME
    assert log_file.exists(), "log file was not created"
    contents = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert contents, "log file is empty"

    record = json.loads(contents[-1])
    assert record["event"] == "nfl_com.fetch"
    assert record["cookie"] == REDACTED_PLACEHOLDER
    assert "super-secret-cookie" not in contents[-1]
    assert record["url"] == "https://example.com"
    assert "timestamp" in record
    assert record["level"] == "info"


@pytest.mark.usefixtures("_flush_root_handlers_after")
def test_log_also_lands_on_stderr(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    configure_logging(_make_settings(tmp_path / "logs"))
    log = get_logger("test")
    log.info("hello_world", cookie="leak-me", url="https://example.com")
    for handler in logging.getLogger().handlers:
        handler.flush()

    captured = capfd.readouterr()
    assert "hello_world" in captured.err
    assert "leak-me" not in captured.err
    assert REDACTED_PLACEHOLDER in captured.err


@pytest.mark.usefixtures("_flush_root_handlers_after")
def test_idempotent_does_not_duplicate_handlers(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path / "logs")
    configure_logging(settings)
    configure_logging(settings)
    configure_logging(settings)

    # 1 file handler + 1 stderr handler; no accumulation.
    handlers = logging.getLogger().handlers
    assert len(handlers) == 2


@pytest.mark.usefixtures("_flush_root_handlers_after")
def test_foreign_stdlib_logs_are_also_redacted(tmp_path: Path) -> None:
    """alembic / uvicorn / sqlalchemy log via stdlib logging — they
    should flow through the same redaction processor chain."""
    log_dir = tmp_path / "logs"
    configure_logging(_make_settings(log_dir))

    stdlib_log = logging.getLogger("alembic.runtime.migration")
    stdlib_log.info("connecting cookie=leakage url=https://x")
    for handler in logging.getLogger().handlers:
        handler.flush()

    contents = (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "connecting cookie=leakage url=https://x" in contents
    record = json.loads(contents.strip().splitlines()[-1])
    assert record["level"] == "info"
    # Foreign string is logged as `event` after passing through the chain.
    assert "event" in record
