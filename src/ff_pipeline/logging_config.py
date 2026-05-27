"""structlog configuration with JSON output and cookie redaction.

Single entry point: ``configure_logging(settings)``. Idempotent — safe to
call multiple times (it replaces the structlog config wholesale each
time rather than appending).

Cookie redaction is implemented as a processor that walks the event
dict and replaces values for known-sensitive keys. The set of keys is
defined in ``REDACTED_KEYS`` and is conservative — we'd rather log
``[REDACTED]`` for a benign field named ``cookie`` than accidentally
leak the NFL_COOKIE into a log file someone might share.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from structlog.types import EventDict, Processor

    from ff_pipeline.settings import Settings

REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "cookie",
        "cookies",
        "nfl_cookie",
        "set-cookie",
        "set_cookie",
        "authorization",
        "auth",
        "token",
        "session",
    }
)
REDACTED_PLACEHOLDER = "[REDACTED]"


def _redact_secrets(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    """Replace values whose key matches REDACTED_KEYS (case-insensitive)
    with the redaction placeholder. Walks nested dicts one level deep,
    which is enough for typical structured log payloads."""
    for key in list(event_dict.keys()):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = REDACTED_PLACEHOLDER
            continue
        value = event_dict[key]
        if isinstance(value, Mapping):
            event_dict[key] = _redact_mapping(value)
    return event_dict


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {k: REDACTED_PLACEHOLDER if k.lower() in REDACTED_KEYS else v for k, v in value.items()}


def _build_processor_chain(log_format: str) -> list[Processor]:
    common: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if log_format == "json":
        common.append(structlog.processors.JSONRenderer())
    else:
        common.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))
    return common


def configure_logging(settings: Settings) -> None:
    """Apply the settings to structlog and the stdlib logging root.

    stdlib logs (uvicorn, alembic, sqlalchemy) are routed through
    structlog so a single config controls every log line the pipeline
    emits.
    """
    level = logging.getLevelName(settings.log_level)

    settings.log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
        force=True,
    )

    structlog.configure(
        processors=_build_processor_chain(settings.log_format),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Convenience accessor so callers don't need to import structlog
    directly. Returns a logger bound with ``logger=name`` if provided."""
    log = structlog.get_logger()
    if name:
        log = log.bind(logger=name)
    return log  # type: ignore[no-any-return]
