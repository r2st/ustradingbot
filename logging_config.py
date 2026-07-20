"""
Structured logging configuration using *structlog*.

Call :func:`setup_logging` once at application startup.  In production the
output is newline-delimited JSON; during development it uses a coloured
console renderer for readability.

**Log rotation (audit B-9).**  Logs are written to *stderr*, not a file sink.
Under the shipped systemd units (``deploy/systemd/``) that stream goes to
journald, which rotates and vacuums by size/age on its own — so a long-running
bot never grows logs unbounded and there is nothing extra to configure.  If you
redirect this stderr to a *file* instead (``… > bot.log``), that file will grow
without bound: put it under ``logrotate`` (a ``copytruncate`` daily rule) or
have the process manager cap it.  Do **not** add an in-process
``RotatingFileHandler`` here — the stderr→journald model is deliberate so the
app owns no log files.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

# All user-facing timestamps in this app are Eastern (market) time — see
# ``config.settings.EASTERN``.  structlog's built-in ``TimeStamper`` emits UTC,
# which made the engine-log viewer show times offset from market hours.  We
# stamp Eastern instead so the logs line up with everything else.
_EASTERN = ZoneInfo("America/New_York")


def _eastern_timestamper(_: object, __: str, event_dict: dict) -> dict:
    """structlog processor: add an Eastern-time ISO ``timestamp`` to each event."""
    event_dict["timestamp"] = datetime.now(_EASTERN).isoformat(timespec="milliseconds")
    return event_dict


def setup_logging(log_level: str = "INFO") -> None:
    """Initialise structured logging for the entire application.

    Args:
        log_level: Root log level as a string (e.g. ``"DEBUG"``, ``"INFO"``).
            Parsed case-insensitively.

    The function configures both :mod:`structlog` and the stdlib
    :mod:`logging` module so that third-party libraries (``ib_insync``,
    ``httpx``, etc.) also route through the same pipeline.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    is_development = sys.stderr.isatty()

    # Shared processors applied to every log event.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _eastern_timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if is_development:
        # Pretty, coloured output for local development.
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(
            colors=True,
        )
    else:
        # Machine-readable JSON for production / log aggregation.
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)

    # Quieten noisy third-party loggers.
    for noisy in ("ib_insync", "asyncio", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))
