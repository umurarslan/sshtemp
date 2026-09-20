import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE_NAME = "sshtemp.log"

# Allowlist: only these ``extra=`` keys reach the log line. A new field must be
# added here to appear, which also keeps unexpected session data out of logs.
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

_LOGGER_NAME = "sshtemp"
_OWNED_HANDLER_FLAG = "_sshtemp_owned"
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


def _mark_owned(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _OWNED_HANDLER_FLAG, True)
    return handler


def _remove_owned_handlers(logger: logging.Logger) -> None:
    for handler in logger.handlers[:]:
        if getattr(handler, _OWNED_HANDLER_FLAG, False):
            logger.removeHandler(handler)
            handler.close()


def configure_logging(
    *,
    verbose: bool = False,
    log_file: str | Path | None = None,
    append: bool = False,
) -> None:
    """Send sshtemp logs to stderr, plus *log_file* when a path is given.

    Handlers added by the calling application are left in place; only handlers
    created here are replaced, so repeated calls do not stack up.
    """
    with _logging_lock:
        level = logging.INFO if verbose else logging.WARNING

        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(logging.DEBUG)
        _remove_owned_handlers(logger)

        stream_handler = _mark_owned(logging.StreamHandler())
        stream_handler.setFormatter(SshTempFormatter(include_timestamp=False))
        stream_handler.setLevel(level)
        logger.addHandler(stream_handler)

        if log_file is not None:
            file_handler = _mark_owned(
                logging.FileHandler(
                    Path(log_file),
                    mode="a" if append else "w",
                    encoding="utf-8",
                )
            )
            file_handler.setFormatter(SshTempFormatter(include_timestamp=True))
            file_handler.setLevel(level)
            logger.addHandler(file_handler)

        # The stderr handler above already prints every record; propagating would
        # print each line a second time through the root logger.
        logger.propagate = False


def shutdown_logging() -> None:
    """Close the handlers sshtemp added, so a log file can be deleted or moved."""
    with _logging_lock:
        _remove_owned_handlers(logging.getLogger(_LOGGER_NAME))
