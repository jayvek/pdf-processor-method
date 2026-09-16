"""Reusable request-scoped logger and configuration."""

import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Optional, Union

DEFAULT_STATUS_LOG = "status.log"
DEFAULT_ERROR_LOG = "error.log"
LOGGER_NAME = "pdf_processor"


class RequestFormatter(logging.Formatter):
    """Formats log records with timestamp, level, request_id, file, and line numbers on errors."""

    def format(self, record: logging.LogRecord) -> str:
        req_id = getattr(record, "request_id", "N/A")
        asctime = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        loc = f"{record.filename}:{record.lineno}" if record.levelno >= logging.ERROR else record.filename
        msg = f"[{asctime}] [{record.levelname:<5}] [{req_id}] [{loc}] {record.getMessage()}"
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            msg = f"{msg}\n{record.exc_text}"
        return msg


# Message-text markers that signal criticality regardless of the record's level.
# Checked case-insensitively against the rendered message text.
CRITICALITY_MARKERS = (
    "ConflictError:",
    "FileNotFoundError:",
    "IsADirectoryError:",
    "ValueError:",
    "RequestValidationError:",
    "Critical:",
    "Fatal:",
    "Unrecoverable",
    "Failed to",
)


def record_is_critical(record: logging.LogRecord) -> bool:
    """True when a record signals a serious error

    A record is considered critical when:
    - it carries exception/traceback info, or
    - an explicit `critical=True` marker was passed via `extra`, or
    - its message text contains a known criticality marker (e.g. an
      error-class prefix logged at INFO level like "ConflictError: ...").
    """
    if getattr(record, "critical", False):
        return True
    if record.exc_info or record.exc_text:
        return True
    try:
        message = record.getMessage()
    except Exception:
        return False
    lowered = message.lower()
    return any(marker.lower() in lowered for marker in CRITICALITY_MARKERS)


class StatusLogFilter(logging.Filter):
    """Allows only normal/informational logs into status.log.

    INFO-level messages that carry criticality signals (serious errors logged
    at info level) are rejected here so they are appended to error.log instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR and not record_is_critical(record)


class ErrorLogFilter(logging.Filter):
    """Allows ERROR+ records, plus INFO-level records that signal serious errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR or record_is_critical(record)


class RequestLogger:
    """Request-scoped logger instance."""

    @staticmethod
    def generate_request_id() -> str:
        """Generate a unique request ID as a standard uuid4 string."""
        return str(uuid.uuid4())

    def __init__(self, request_id: Optional[str] = None, logger: Optional[logging.Logger] = None):
        if request_id is None or not str(request_id).strip():
            self.request_id = self.generate_request_id()
        else:
            self.request_id = str(request_id)
        self._logger = logger or get_base_logger()

    def _log(self, level: int, msg: str, exc_info: Any = None) -> None:
        self._logger.log(
            level, str(msg), extra={"request_id": self.request_id}, exc_info=exc_info, stacklevel=3
        )

    def info(self, message: str) -> None:
        self._log(logging.INFO, message)

    def error(self, message: str, exc_info: Union[bool, BaseException, None] = None) -> None:
        if exc_info is None and sys.exc_info()[0] is not None:
            exc_info = True
        self._log(logging.ERROR, message, exc_info=exc_info)

    def warning(self, message: str) -> None:
        self._log(logging.WARNING, message)

    def debug(self, message: str) -> None:
        self._log(logging.DEBUG, message)

    def log(self, message: str) -> None:
        self.info(message)

    def __call__(self, message: str) -> None:
        self.info(message)


_CONFIGURED = False


def configure_logging(
    status_log_path: Union[str, Path] = DEFAULT_STATUS_LOG,
    error_log_path: Union[str, Path] = DEFAULT_ERROR_LOG,
    enable_console: bool = True,
    force_reconfigure: bool = False,
) -> logging.Logger:
    """Configure status.log and error.log file handlers."""
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)

    if _CONFIGURED and not force_reconfigure:
        return logger

    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    formatter = RequestFormatter()

    # status.log handler (normal / info logs only; criticality-signalled
    # info messages are excluded so they land in error.log instead)
    sh = logging.FileHandler(Path(status_log_path).resolve(), mode="a", encoding="utf-8")
    sh.setLevel(logging.INFO)
    sh.addFilter(StatusLogFilter())
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    # error.log handler (ERROR+ records, plus criticality-signalled INFO records)
    eh = logging.FileHandler(Path(error_log_path).resolve(), mode="a", encoding="utf-8")
    eh.setLevel(logging.DEBUG)
    eh.addFilter(ErrorLogFilter())
    eh.setFormatter(formatter)
    logger.addHandler(eh)

    if enable_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_base_logger() -> logging.Logger:
    """Return configured base logger."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(LOGGER_NAME)
