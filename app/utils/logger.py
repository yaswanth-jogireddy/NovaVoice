import logging
import sys

import structlog


_configured = False
_explicitly_configured = False


def configure_logging(
    log_level: str = "INFO", log_format: str = "json", _implicit: bool = False
) -> None:
    """Configure structlog.

    Module-level `logger = get_logger(...)` calls run at import time, before main()
    can read .env — those configure implicitly with defaults. The first explicit call
    (from main.py / VoiceAgent) must still be able to apply LOG_LEVEL / LOG_FORMAT,
    so only implicit calls are short-circuited once configured.
    """
    global _configured, _explicitly_configured
    if _implicit and _configured:
        return
    if _explicitly_configured:
        return
    if not _implicit:
        _explicitly_configured = True

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if log_format == "console":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
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
            *([structlog.processors.format_exc_info] if log_format != "console" else []),
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "websockets", "grpc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging(_implicit=True)
    return structlog.get_logger(component=component)
