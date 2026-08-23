import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE_NAME = "sshtemp.log"

_LOG_EXTRA_FIELDS = (
    "log_id",
    "host",
    "template_line",
    "command",
    "port",
    "username",
    "pattern",
    "error",
    "attempt",
    "max_attempts",
    "wait_seconds",
    "output_preview",
    "capture_name",
    "capture_bytes",
    "value",
)

_SENSITIVE_EXTRA_FIELDS = frozenset({"password", "then_response", "secret"})

_log_file_append = False
_logging_lock = threading.Lock()


def _format_timestamp(record: logging.LogRecord) -> str:
    """UTC ISO 8601 timestamp for log file lines (e.g. 2026-08-22T19:17:03.456Z)."""
    instant = datetime.fromtimestamp(record.created, tz=timezone.utc)
    text = instant.isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


class SshTempFormatter(logging.Formatter):
    def __init__(self, *, include_timestamp: bool = False) -> None:
        super().__init__()
        self._include_timestamp = include_timestamp

    def format(self, record: logging.LogRecord) -> str:
        body = f"{record.name}:{record.getMessage()}"
        extras = [
            f"{key}={getattr(record, key)}"
            for key in _LOG_EXTRA_FIELDS
            if key not in _SENSITIVE_EXTRA_FIELDS
            and hasattr(record, key)
            and getattr(record, key) is not None
        ]
        if extras:
            body = f"{body} {' '.join(extras)}"

        log_id = getattr(record, "log_id", None)
        if log_id:
            line = f"{record.levelname}:{log_id}:{body}"
        else:
            line = f"{record.levelname}:{body}"

        if self._include_timestamp:
            return f"{_format_timestamp(record)} {line}"
        return line


def configure_logging(
    *,
    verbose: bool = False,
    log_file: str | Path | None = None,
    append: bool | None = None,
) -> Path:
    """Configure sshtemp logging to console and a log file."""
    global _log_file_append

    with _logging_lock:
        log_path = Path(log_file or LOG_FILE_NAME)
        if append is None:
            append = _log_file_append

        console_level = logging.INFO if verbose else logging.WARNING
        file_level = logging.INFO if verbose else logging.WARNING

        file_handler = logging.FileHandler(
            log_path,
            mode="a" if append else "w",
            encoding="utf-8",
        )
        file_handler.setFormatter(SshTempFormatter(include_timestamp=True))
        file_handler.setLevel(file_level)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(SshTempFormatter(include_timestamp=False))
        stream_handler.setLevel(console_level)

        root_logger = logging.getLogger("sshtemp")
        root_logger.setLevel(logging.DEBUG)
        for handler in root_logger.handlers[:]:
            handler.close()
            root_logger.removeHandler(handler)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(stream_handler)
        root_logger.propagate = False

        _log_file_append = True
        return log_path


def shutdown_logging() -> None:
    """Close sshtemp log handlers so log files can be deleted (e.g. in tests)."""
    with _logging_lock:
        root_logger = logging.getLogger("sshtemp")
        for handler in root_logger.handlers[:]:
            handler.close()
            root_logger.removeHandler(handler)
