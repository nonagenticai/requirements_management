"""
Structured logging with OpenTelemetry trace correlation.
"""

import contextvars
import logging
import os
import sys
import types

import structlog

# Check if OTEL is available
OTEL_AVAILABLE = False
trace: types.ModuleType | None = None
try:
    from opentelemetry import trace

    OTEL_AVAILABLE = True
except ImportError:
    pass


# Per-request context — set by middleware, read by structlog processors.
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
_tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_id", default=None
)
_subject_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "subject_id", default=None
)
_principal_source_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "principal_source", default=None
)


def bind_request_context(
    *,
    request_id: str | None = None,
    tenant_id: str | None = None,
    subject_id: str | None = None,
    principal_source: str | None = None,
) -> None:
    """Bind per-request fields into contextvars; picked up by add_principal_context."""
    if request_id is not None:
        _request_id_var.set(request_id)
    if tenant_id is not None:
        _tenant_id_var.set(tenant_id)
    if subject_id is not None:
        _subject_id_var.set(subject_id)
    if principal_source is not None:
        _principal_source_var.set(principal_source)


def add_trace_context(logger, method_name, event_dict):
    """Inject OpenTelemetry trace context into structured logs."""
    if OTEL_AVAILABLE and trace:
        span = trace.get_current_span()
        if span and span.is_recording():
            ctx = span.get_span_context()
            if ctx.is_valid:
                event_dict["trace_id"] = format(ctx.trace_id, "032x")
                event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def add_service_context(logger, method_name, event_dict):
    """Add service metadata to logs."""
    from settings import get_settings

    event_dict["service"] = os.getenv("OTEL_SERVICE_NAME", "requirements-api")
    # ⚠️ Read through `settings` so the label and the production fail-fast can
    # never disagree about which environment this is. Measured 2026-09-04:
    # `ENVIRONMENT` is absent from the live Deployment, so every production log
    # line has been emitted with `environment=development` — verbatim, from
    # Loki. That is not cosmetic: it is the field an operator filters on, so
    # production traffic has been hiding inside the development bucket, and the
    # `_REQUIRED_IN_PRODUCTION` guard that keys off the same value has never run.
    # Sourcing both from one place means arming one arms the other.
    # ⛔ NEVER LET THIS RAISE. A structlog processor that throws takes out EVERY
    # log line in the process, so this cannot be a bare `get_settings().environment`
    # — which is exactly what the first version of this change was. It broke 6
    # existing tests immediately (a `SimpleNamespace` settings fake has no
    # `environment`, so `AttributeError` fired inside structlog for every line),
    # and the same shape would break production logging outright if `Settings()`
    # ever failed to construct — including via the production fail-fast this very
    # variable arms, which would then be unable to report why it fired.
    #
    # The fallback is the RAW variable, not a literal: it degrades to the old
    # behaviour rather than to a guess.
    try:
        environment = get_settings().environment
    except Exception:  # noqa: BLE001 - a log processor must never break logging
        environment = os.getenv("ENVIRONMENT", "development")
    event_dict["environment"] = environment
    return event_dict


def add_principal_context(logger, method_name, event_dict):
    """Inject per-request principal fields (request_id, tenant_id, subject_id, source)."""
    rid = _request_id_var.get()
    if rid is not None:
        event_dict.setdefault("request_id", rid)
    tid = _tenant_id_var.get()
    if tid is not None:
        event_dict.setdefault("tenant_id", tid)
    sid = _subject_id_var.get()
    if sid is not None:
        event_dict.setdefault("subject_id", sid)
    src = _principal_source_var.get()
    if src is not None:
        event_dict.setdefault("principal_source", src)
    return event_dict


_configured: bool = False


def configure_logging(level: int | None = None, log_file: str | None = None) -> None:
    """Configure structured JSON logging with trace context.

    Args:
        level: Logging level (defaults to INFO or LOG_LEVEL env var)
        log_file: Optional file path for logging (ignored in structlog setup)
    """
    global _configured

    if _configured:
        return

    log_format = os.getenv("LOG_FORMAT", "json").lower()
    log_level = level or getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper())

    # Determine processors based on format
    if log_format == "json":
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            add_service_context,
            add_trace_context,
            add_principal_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable console format for development
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            add_service_context,
            add_trace_context,
            add_principal_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    # Suppress noisy loggers
    for logger_name in ["httpx", "httpcore", "uvicorn.access", "urllib3"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a module-level structured logger.

    Args:
        name: Logger name (defaults to "requirements_api")

    Returns:
        Configured structlog logger
    """
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name or "requirements_api")
    return logger
