"""Verify structlog cookie redaction."""

from __future__ import annotations

from ff_pipeline.logging_config import (
    REDACTED_PLACEHOLDER,
    _redact_secrets,
)


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
