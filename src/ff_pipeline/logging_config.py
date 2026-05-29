"""structlog configuration with JSON output and cookie redaction.

Single entry point: ``configure_logging(settings)``. Idempotent — safe to
call multiple times.

Output is dual-routed through stdlib ``logging``:

* ``data/logs/pipeline.log`` via ``TimedRotatingFileHandler`` (rolls at
  midnight UTC, keeps 14 days) — always JSON for log-aggregation tools.
* ``stderr`` via ``StreamHandler`` — JSON or pretty console depending on
  ``LOG_FORMAT``.

Both handlers share the same processor chain (timestamp, log-level,
secret redaction, stack/exception rendering) via structlog's
``ProcessorFormatter``. Non-structlog stdlib logs (uvicorn, alembic,
sqlalchemy) pass through the same chain via ``foreign_pre_chain`` so
NFL.com cookies in third-party logs get redacted too.

Cookie redaction lives in ``REDACTED_KEYS``; values are replaced with
``[REDACTED]`` case-insensitively, including one level of nested
mappings (header dicts).
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
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

LOG_FILE_NAME = "pipeline.log"
LOG_FILE_RETENTION_DAYS = 14


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


def _shared_processors() -> list[Processor]:
    """Processors that run on EVERY log line — structlog-native and
    foreign (stdlib) alike — before the final renderer."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def _make_handler_formatter(renderer: Processor) -> structlog.stdlib.ProcessorFormatter:
    """Build a ProcessorFormatter that finishes with the given renderer.

    ``foreign_pre_chain`` ensures stdlib log records (alembic, uvicorn,
    sqlalchemy) go through the same redaction + timestamp pipeline.
    """
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )


def configure_logging(settings: Settings) -> None:
    """Apply settings to stdlib logging + structlog.

    Idempotent — clears existing handlers on the root logger before
    attaching ours, so a re-call (e.g., in tests) doesn't accumulate
    duplicate output.
    """
    level = logging.getLevelName(settings.log_level)

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_dir / LOG_FILE_NAME

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=LOG_FILE_RETENTION_DAYS,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(_make_handler_formatter(structlog.processors.JSONRenderer()))

    stderr_renderer: Processor
    if settings.log_format == "json":
        stderr_renderer = structlog.processors.JSONRenderer()
    else:
        stderr_renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(_make_handler_formatter(stderr_renderer))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        # Defensive: closing a re-closed handler shouldn't crash idempotent re-config.
        with contextlib.suppress(ValueError, OSError):
            existing.close()
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)
    root.setLevel(level)

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Convenience accessor so callers don't need to import structlog
    directly. Returns a logger bound with ``logger=name`` if provided."""
    log = structlog.get_logger()
    if name:
        log = log.bind(logger=name)
    return log  # type: ignore[no-any-return]
