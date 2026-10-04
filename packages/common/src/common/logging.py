"""Structured JSON logging (structlog) with trace correlation and PII redaction.

Usage:
    log = get_logger(__name__)
    log.info("job_fetched", job_id=job.id, stage="fetch", message="optional human text")

The first positional argument is the `event` name. IDs only: never log resume text, email
bodies, tokens or full email addresses. As a safety net, sensitive keys are dropped and
anything that looks like an email address is masked (`j***@acme.com`).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog
from opentelemetry import trace

REQUIRED_FIELDS = (
    "timestamp",
    "level",
    "service",
    "env",
    "version",
    "trace_id",
    "span_id",
    "user_id",
    "job_id",
    "application_id",
    "stage",
    "event",
    "duration_ms",
    "error_type",
    "message",
)

# Keys whose values must never reach a log line, whatever their content.
REDACTED_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "body",
        "completion",
        "cookie",
        "email_body",
        "id_token",
        "password",
        "prompt",
        "refresh_token",
        "resume",
        "resume_text",
        "secret",
        "token",
    }
)
REDACTED = "[REDACTED]"

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def mask_email(value: str) -> str:
    """`jane.doe@acme.com` -> `j***@acme.com`, for every substring that looks like an email."""
    return _EMAIL_RE.sub(r"\1***@\2", value)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return mask_email(value)
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if str(k).lower() in REDACTED_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact(v) for v in value]
    return value


def _redact_processor(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return redact(event_dict)  # type: ignore[no-any-return]


def _add_trace_context(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def _fill_required(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for field in REQUIRED_FIELDS:
        event_dict.setdefault(field, None)
    if event_dict["message"] is None:
        event_dict["message"] = event_dict["event"]
    return event_dict


def _shared_processors(service: str, env: str, version: str) -> list[Any]:
    def add_static(
        _: Any, __: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict.setdefault("service", service)
        event_dict.setdefault("env", env)
        event_dict.setdefault("version", version)
        return event_dict

    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_static,
        _add_trace_context,
        structlog.processors.format_exc_info,
        _redact_processor,
        _fill_required,
    ]


def configure_logging(
    *, service: str, env: str, version: str, level: str = "INFO", stream: TextIO | None = None
) -> None:
    """Configure structlog, and route stdlib logging (uvicorn, sqlalchemy, ...) through it."""
    out = stream or sys.stdout
    processors = _shared_processors(service, env, version)
    level_no = logging.getLevelNamesMapping()[level.upper()]

    structlog.configure(
        processors=[*processors, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,  # so set_log_level() takes effect on hot reload
    )

    handler = logging.StreamHandler(out)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level_no)
    # uvicorn installs its own plain-text handlers; send its records through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True


def set_log_level(level: str) -> None:
    level_no = logging.getLevelNamesMapping()[level.upper()]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level_no))
    logging.getLogger().setLevel(level_no)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


bind_context = structlog.contextvars.bind_contextvars
clear_context = structlog.contextvars.clear_contextvars
