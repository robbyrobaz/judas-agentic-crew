"""JSON structured logging setup.

Call configure_logging() once at process startup. All subsequent
logging.getLogger() calls will emit JSON to stdout (and optionally to a
rotating file under logs/).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge any extra fields passed via `extra=`
        skip = {
            "name", "msg", "args", "created", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs",
            "pathname", "process", "processName", "relativeCreated",
            "thread", "threadName", "exc_info", "exc_text", "stack_info",
            "message",
        }
        for k, v in record.__dict__.items():
            if k not in skip:
                payload[k] = v
        return json.dumps(payload, default=str)


def configure_logging(
    level: str = "INFO",
    log_dir: str | Path | None = None,
    log_file: str = "judas_crew.log",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure root logger with JSON output.

    Args:
        level: Root log level string (DEBUG, INFO, WARNING, ERROR).
        log_dir: If given, also write rotating JSON log to this directory.
        log_file: Filename within log_dir.
        max_bytes: Rotate after this many bytes.
        backup_count: Keep this many rotated files.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers to avoid duplicate output.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = _JSONFormatter()

    # stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Optional file handler
    if log_dir is not None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            Path(log_dir) / log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Silence overly verbose third-party loggers
    for noisy in ("ib_async", "asyncio", "urllib3", "httpx", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def setup_logging(name: str, level: str = "INFO") -> logging.Logger:
    """Convenience wrapper: configure root logging and return a named logger.

    This is the simple interface described in the spec. For full control,
    use configure_logging() directly.

    Args:
        name: Logger name (typically __name__ of the calling module).
        level: Log level string.

    Returns:
        logging.Logger instance for `name`.
    """
    configure_logging(level=level)
    return logging.getLogger(name)
