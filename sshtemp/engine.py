import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import paramiko

from sshtemp.exceptions import (
    SshTempConnectError,
    SshTempError,
    SshTempErrorPattern,
    SshTempTemplateError,
    SshTempTimeoutError,
    SshTempValidationError,
)
from sshtemp.logging_config import LOG_FILE_NAME, configure_logging, shutdown_logging
from sshtemp.models import SshTempCapture, SshTempResult, SshTempRunContext
from sshtemp.validator import (
    DEFAULT_WAIT_SECONDS,
    is_directive_line,
    parse_directive,
    parse_encoding,
    parse_shell_size,
    validate_directive_prefix,
    validate_template,
)

logger = logging.getLogger(__name__)

_OUTPUT_PREVIEW_LIMIT = 500
DEFAULT_SHELL_WIDTH = 240
DEFAULT_SHELL_HEIGHT = 72
DEFAULT_RECV_BUFFER = 65535
DEFAULT_RECV_ENCODING = "utf-8"
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_RETRY_COUNT = 1
KEEPALIVE_INTERVAL = 60
# After invoke_shell, wait this long for the first login byte (and again for
# silence after MOTD). Quiet devices proceed; they do not fail the run.
LOGIN_SETTLE_SECONDS = 2.0
DEFAULT_HOST_KEY_POLICY = "auto"
HOST_KEY_POLICIES = frozenset({"auto", "reject"})
_capture_file_lock = threading.Lock()


def _preview_output(text: str, limit: int = _OUTPUT_PREVIEW_LIMIT) -> str:
    cleaned = text.replace("\r", "")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def _display_command(command: str) -> str:
    stripped = command.lstrip("\n")
    return stripped if stripped else command


def _format_validation_error(issues: list[str]) -> str:
    return "template validation failed:\n" + "\n".join(issues)


def _format_write_error(exc: OSError) -> str:
    return f"failed to write output: {exc}"


def _normalize_host_key_policy(value: str) -> str:
    policy = value.strip().lower()
    if policy not in HOST_KEY_POLICIES:
        policies = ", ".join(sorted(HOST_KEY_POLICIES))
        raise ValueError(
            f"host_key_policy must be one of {policies!r}, got {value!r}"
        )
    return policy


def _missing_host_key_policy(policy: str):
    if policy == "reject":
        return paramiko.RejectPolicy()
    return paramiko.AutoAddPolicy()


def _configure_ssh_client(client: paramiko.SSHClient, host_key_policy: str) -> None:
    client.load_system_host_keys()
    client.set_missing_host_key_policy(_missing_host_key_policy(host_key_policy))


def _connect_auth_kwargs(password: str | None) -> dict[str, bool]:
    if password:
        return {"allow_agent": False, "look_for_keys": False}
    return {}


def _log_extra(
    host: str | None = None,
    template_line: int | None = None,
    command: str | None = None,
    log_id: str | None = None,
    **fields: object,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if log_id is not None:
        payload["log_id"] = log_id
    if host is not None:
        payload["host"] = host
    if template_line is not None:
        payload["template_line"] = template_line
    if command is not None:
        payload["command"] = _display_command(command)
    for key, value in fields.items():
        if key not in ("password", "then_response", "secret"):
            payload[key] = value
    return payload


def _record_warning(
    context: SshTempRunContext,
    message: str,
    *,
    host: str | None = None,
    template_line: int | None = None,
) -> None:
    """Append a non-fatal warning to the run result and log it immediately."""
    if message in context.warnings:
        return
    context.warnings.append(message)
    logger.warning(
        "sshtemp.warning",
        extra=_log_extra(
            host,
            log_id=context.log_id,
            template_line=template_line,
            error=message,
        ),
    )


@dataclass
class _PollOutcome:
    recv_text: str
    had_error: bool = False
    channel_closed: bool = False


def _session_closed(session) -> bool:
    """True when the SSH channel is actually closed (not a mock/placeholder object)."""
    if getattr(session, "closed", False) is True:
        return True
    checker = getattr(session, "exit_status_ready", None)
    if not callable(checker):
        return False
    return checker() is True


def _channel_timeout(session) -> float | None:
    getter = getattr(session, "gettimeout", None)
    if not callable(getter):
        return None
    return getter()


def _set_channel_timeout(session, timeout: float | None) -> None:
    setter = getattr(session, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _drain_ready(
    session, recv_buffer: bytes, recv_buffer_size: int
) -> tuple[bytes, bool]:
    """Read already-buffered bytes without waiting. Returns (buffer, channel_closed)."""
    while session.recv_ready():
        extra = session.recv(recv_buffer_size)
        if not extra:
            return recv_buffer, True
        recv_buffer += extra
    return recv_buffer, False


def _stash_open_capture(
    context: SshTempRunContext,
    *,
    capturing: bool,
    capture_name: str,
    capture_buffer: str,
) -> None:
    if not capturing:
        return
    output = "\n".join((capture_buffer + context.last_recv_text).splitlines())
    if not output:
        return
    context.partial_capture = SshTempCapture(name=capture_name, output=output)
    context.last_recv_text = ""


def _poll_command_output(
    session,
    *,
    host: str | None,
    wait_seconds: float,
    current_if_rules: list[tuple[str, str]],
    responded: list[bool],
    expect_pattern: str,
    error_pattern: str,
    error_fail: str,
    line_number: int,
    command: str,
    capture_name: str | None = None,
    context: SshTempRunContext,
    recv_buffer_size: int,
    recv_encoding: str,
) -> _PollOutcome:
    """Read shell output until expect matches, the channel closes, or *wait_seconds* elapse."""
    recv_buffer = b""
    recv_text = ""
    had_error = False
    channel_closed = False
    deadline = time.monotonic() + wait_seconds

    def _apply_output_hooks() -> bool:
        nonlocal had_error
        for i, (if_pat, then_resp) in enumerate(current_if_rules):
            if not responded[i] and re.search(if_pat, recv_text):
                session.send(then_resp + "\n")
                responded[i] = True
                if context.verbose:
                    logger.info(
                        "sshtemp.if_then_matched",
                        extra=_log_extra(
                            host,
                            log_id=context.log_id,
                            template_line=line_number,
                            command=command,
                            pattern=if_pat,
                        ),
                    )

        if error_pattern and re.search(error_pattern, recv_text):
            display_command = _display_command(command)
            capture_part = (
                f", capture {capture_name!r}" if capture_name else ""
            )
            message = (
                f'error pattern "{error_pattern}" matched at line {line_number}'
                f"{capture_part}, command {display_command!r}"
            )
            if error_fail == "on":
                context.last_recv_text = recv_text
                raise SshTempErrorPattern(
                    message,
                    host=host,
                    template_line=line_number,
                    command=display_command,
                    capture_name=capture_name,
                )
            _record_warning(
                context,
                message,
                host=host,
                template_line=line_number,
            )
            had_error = True

        if expect_pattern and re.search(expect_pattern, recv_text):
            if context.verbose:
                logger.info(
                    "sshtemp.expect_matched",
                    extra=_log_extra(
                        host,
                        log_id=context.log_id,
                        template_line=line_number,
                        command=command,
                        pattern=expect_pattern,
                    ),
                )
            return True
        return False

    while True:
        recv_buffer, drained_closed = _drain_ready(
            session, recv_buffer, recv_buffer_size
        )
        if drained_closed:
            channel_closed = True
        if recv_buffer:
            recv_text = recv_buffer.decode(recv_encoding, errors="ignore")
            if _apply_output_hooks():
                return _PollOutcome(
                    recv_text,
                    had_error=had_error,
                    channel_closed=channel_closed,
                )
        if channel_closed or _session_closed(session):
            return _PollOutcome(
                recv_text,
                had_error=had_error,
                channel_closed=True,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        previous_timeout = _channel_timeout(session)
        try:
            _set_channel_timeout(session, remaining)
            chunk = session.recv(recv_buffer_size)
        except socket.timeout:
            break
        finally:
            _set_channel_timeout(session, previous_timeout)

        if not chunk:
            channel_closed = True
            continue

        recv_buffer += chunk

    return _PollOutcome(
        recv_text,
        had_error=had_error,
        channel_closed=channel_closed,
    )


def _settle_login_output(
    session,
    *,
    host: str | None,
    wait_seconds: float,
    idle_seconds: float,
    expect_pattern: str,
    line_number: int,
    context: SshTempRunContext,
    recv_buffer_size: int,
    recv_encoding: str,
) -> None:
    """Read post-connect MOTD/prompt without sending a command. Never fails the run.

    Stops when *_EXPECT* matches received text, the channel goes idle, the
    channel closes, or *wait_seconds* elapse. *_ERROR* and *_IF* are not
    applied, and the text is not captured. A device that prints nothing until
    the first command hits the idle timeout and execution continues.
    """
    recv_buffer = b""
    recv_text = ""
    saw_data = False
    deadline = time.monotonic() + wait_seconds
    last_data_at = time.monotonic()

    def _matched_expect() -> bool:
        if not expect_pattern or not recv_text:
            return False
        if not re.search(expect_pattern, recv_text):
            return False
        if context.verbose:
            logger.info(
                "sshtemp.login_settled",
                extra=_log_extra(
                    host,
                    log_id=context.log_id,
                    template_line=line_number,
                    pattern=expect_pattern,
                    output_preview=_preview_output(recv_text),
                ),
            )
        return True

    while True:
        previous_len = len(recv_buffer)
        recv_buffer, drained_closed = _drain_ready(
            session, recv_buffer, recv_buffer_size
        )
        if len(recv_buffer) > previous_len:
            recv_text = recv_buffer.decode(recv_encoding, errors="ignore")
            saw_data = True
            last_data_at = time.monotonic()
            if _matched_expect():
                return
        if drained_closed or _session_closed(session):
            return

        now = time.monotonic()
        remaining_deadline = deadline - now
        if remaining_deadline <= 0:
            return

        if saw_data:
            idle_left = idle_seconds - (now - last_data_at)
            if idle_left <= 0:
                return
            recv_timeout = min(remaining_deadline, idle_left)
        else:
            recv_timeout = min(remaining_deadline, idle_seconds)

        if recv_timeout <= 0:
            return

        previous_timeout = _channel_timeout(session)
        try:
            _set_channel_timeout(session, recv_timeout)
            chunk = session.recv(recv_buffer_size)
        except socket.timeout:
            return
        finally:
            _set_channel_timeout(session, previous_timeout)

        if not chunk:
            return
        recv_buffer += chunk
        recv_text = recv_buffer.decode(recv_encoding, errors="ignore")
        saw_data = True
        last_data_at = time.monotonic()
        if _matched_expect():
            return


def _validate_or_raise(
    command_temp_text: str,
    directive_prefix: str,
    *,
    host: str | None = None,
    log_id: str | None = None,
) -> None:
    validation = validate_template(
        command_temp_text, directive_prefix=directive_prefix
    )
    if validation.valid:
        return

    for issue in validation.issues:
        logger.error(
            "sshtemp.template_validation_error",
            extra=_log_extra(host, log_id=log_id, error=issue),
        )
    raise SshTempValidationError(
        "template validation failed",
        validation.issues,
        host=host,
    )


def _ssh_temp(
    command_temp_text: str,
    directive_prefix: str = "_",
    context: SshTempRunContext | None = None,
    *,
    skip_validation: bool = False,
):
    """Execute an SSH template and yield SshTempCapture objects."""

    default_wait_seconds = DEFAULT_WAIT_SECONDS
    default_connect_timeout = DEFAULT_CONNECT_TIMEOUT
    default_retry_count = DEFAULT_RETRY_COUNT

    run_context = context or SshTempRunContext()

    prefix_issue = validate_directive_prefix(directive_prefix)
    if prefix_issue:
        raise SshTempValidationError(
            "template validation failed",
            [prefix_issue],
        )

    if not skip_validation:
        _validate_or_raise(
            command_temp_text,
            directive_prefix,
            host=run_context.host,
            log_id=run_context.log_id,
        )

    client = None
    session = None

    host = None
    username = None
    password = None
    port = "22"
    timeout = str(default_connect_timeout)

    capture_buffer = ""
    capturing = False
    pending_wait: int | None = None
    pending_retry: int | None = None
    capture_block_wait: int | None = None
    capture_block_retry: int | None = None
    expect_pattern = ""
    error_pattern = ""
    error_fail = "off"
    capture_name = ""
    capture_leading_enter = False
    shell_width = DEFAULT_SHELL_WIDTH
    shell_height = DEFAULT_SHELL_HEIGHT
    login_settle_seconds = LOGIN_SETTLE_SECONDS
    recv_buffer_size = DEFAULT_RECV_BUFFER
    recv_encoding = DEFAULT_RECV_ENCODING
    template_lines = command_temp_text.splitlines()

    if_rules: list[tuple[str, str]] = []
    pending_if = None

    def _consume_command_timing() -> tuple[int, int]:
        nonlocal pending_wait, pending_retry
        wait = default_wait_seconds
        retry = default_retry_count
        if capturing and capture_block_wait is not None:
            wait = capture_block_wait
        if capturing and capture_block_retry is not None:
            retry = capture_block_retry
        if pending_wait is not None:
            wait = pending_wait
            pending_wait = None
        if pending_retry is not None:
            retry = pending_retry
            pending_retry = None
        return wait, retry

    try:
        for line_number, line in enumerate(template_lines, start=1):
            if not line.strip():
                continue

            parsed = parse_directive(line, directive_prefix)
            name = parsed[0] if parsed is not None else None
            raw = parsed[1] if parsed is not None else ""

            if name == "#":
                continue

            elif line.startswith(directive_prefix + "HOST:"):
                if session is not None:
                    session.close()
                    session = None
                if client is not None:
                    client.close()
                    client = None
                host = line.removeprefix(directive_prefix + "HOST:").strip()
                run_context.host = host
                continue
            elif line.startswith(directive_prefix + "USERNAME:"):
                username = line.removeprefix(directive_prefix + "USERNAME:").strip()
                continue
            elif line.startswith(directive_prefix + "PASSWORD:"):
                password_value = line.removeprefix(directive_prefix + "PASSWORD:").strip()
                password = password_value if password_value else None
                continue
            elif line.startswith(directive_prefix + "PORT:"):
                port = line.removeprefix(directive_prefix + "PORT:").strip()
                continue
            elif line.startswith(directive_prefix + "TIMEOUT:"):
                timeout = line.removeprefix(directive_prefix + "TIMEOUT:").strip()
                continue
            elif line.startswith(directive_prefix + "LOGID:"):
                log_id_value = line.removeprefix(directive_prefix + "LOGID:").strip()
                if not log_id_value:
                    raise SshTempTemplateError(
                        f"{directive_prefix}LOGID value is empty at line {line_number}",
                        host=host,
                        template_line=line_number,
                    )
                run_context.log_id = log_id_value
                continue
            elif line.startswith(directive_prefix + "SHELL:"):
                raw = line.removeprefix(directive_prefix + "SHELL:").strip()
                try:
                    shell_width, shell_height = parse_shell_size(raw)
                except ValueError as exc:
                    raise SshTempTemplateError(
                        f"invalid {directive_prefix}SHELL at line {line_number}: {exc}",
                        host=host,
                        template_line=line_number,
                    ) from exc
                continue
            elif line.startswith(directive_prefix + "SETTLE:"):
                raw = line.removeprefix(directive_prefix + "SETTLE:").strip()
                try:
                    login_settle_seconds = max(0, int(raw))
                except ValueError as exc:
                    raise SshTempTemplateError(
                        f"invalid {directive_prefix}SETTLE at line {line_number}: "
                        f"must be an integer, got {raw!r}",
                        host=host,
                        template_line=line_number,
                    ) from exc
                continue
            elif line.startswith(directive_prefix + "RECV_BUFFER:"):
                raw = line.removeprefix(directive_prefix + "RECV_BUFFER:").strip()
                try:
                    recv_buffer_size = max(1, int(raw))
                except ValueError as exc:
                    raise SshTempTemplateError(
                        f"invalid {directive_prefix}RECV_BUFFER at line {line_number}: "
                        f"must be an integer, got {raw!r}",
                        host=host,
                        template_line=line_number,
                    ) from exc
                continue
            elif line.startswith(directive_prefix + "ENCODING:"):
                raw = line.removeprefix(directive_prefix + "ENCODING:").strip()
                try:
                    recv_encoding = parse_encoding(raw)
                except ValueError as exc:
                    raise SshTempTemplateError(
                        f"invalid {directive_prefix}ENCODING at line {line_number}: {exc}",
                        host=host,
                        template_line=line_number,
                    ) from exc
                continue
            elif line.startswith(directive_prefix + "IF:"):
                if pending_if is not None:
                    raise SshTempTemplateError(
                        f"missing {directive_prefix}THEN for {directive_prefix}IF at line {line_number}",
                        host=host,
                        template_line=line_number,
                    )
                pending_if = line.removeprefix(directive_prefix + "IF:").lstrip()
                continue
            elif line.startswith(directive_prefix + "THEN:"):
                if pending_if is None:
                    raise SshTempTemplateError(
                        f"{directive_prefix}THEN without {directive_prefix}IF at line {line_number}",
                        host=host,
                        template_line=line_number,
                    )
                then_text = line.removeprefix(directive_prefix + "THEN:").strip()
                if_rules.append((pending_if, then_text))
                pending_if = None
                continue
            elif name == "ENTER":
                line = ""
            elif line.startswith(directive_prefix + "EXPECT:"):
                expect_pattern = line.removeprefix(directive_prefix + "EXPECT:").lstrip()
                continue
            elif line.startswith(directive_prefix + "ERROR:"):
                error_pattern = line.removeprefix(directive_prefix + "ERROR:").lstrip()
                if error_pattern:
                    error_fail = "on"
                else:
                    error_fail = "off"
                continue
            elif line.startswith(directive_prefix + "ERRORFAIL:"):
                value = line.removeprefix(directive_prefix + "ERRORFAIL:").strip().lower()
                if value == "on":
                    error_fail = "on"
                elif value == "off":
                    error_fail = "off"
                else:
                    logger.warning(
                        "sshtemp.invalid_errorfail",
                        extra=_log_extra(
                            host,
                            log_id=run_context.log_id,
                            template_line=line_number,
                            value=value,
                        ),
                    )
                continue
            elif line.startswith(directive_prefix + "WAIT:"):
                pending_wait = max(
                    1, int(line.removeprefix(directive_prefix + "WAIT:").strip())
                )
                continue
            elif line.startswith(directive_prefix + "RETRY:"):
                pending_retry = max(
                    1, int(line.removeprefix(directive_prefix + "RETRY:").strip())
                )
                continue
            elif line.startswith(directive_prefix + "SLEEP:"):
                time.sleep(int(line.removeprefix(directive_prefix + "SLEEP:").strip()))
                continue
            elif name == "CAPTURE":
                if pending_wait is not None:
                    capture_block_wait = pending_wait
                    pending_wait = None
                if pending_retry is not None:
                    capture_block_retry = pending_retry
                    pending_retry = None
                capturing = True
                capture_leading_enter = True
                capture_name = raw.strip()
                continue
            elif name == "END":
                if capturing:
                    capture_buffer_copy = capture_buffer
                    capturing = False
                    capture_buffer = ""
                    capture_block_wait = None
                    capture_block_retry = None
                    output = "\n".join(capture_buffer_copy.splitlines())
                    if run_context.verbose:
                        logger.info(
                            "sshtemp.capture_completed",
                            extra=_log_extra(
                                host,
                                log_id=run_context.log_id,
                                template_line=line_number,
                                capture_name=capture_name,
                                capture_bytes=len(output.encode("utf-8")),
                            ),
                        )
                    yield SshTempCapture(name=capture_name, output=output)
                continue

            if parsed is not None and not is_directive_line(line, directive_prefix):
                raise SshTempTemplateError(
                    f"unknown directive at line {line_number}: {line!r}",
                    host=host,
                    template_line=line_number,
                )

            if session is None:
                if not host or not username:
                    raise SshTempTemplateError(
                        f"missing {directive_prefix}HOST or {directive_prefix}USERNAME at line {line_number}",
                        host=host,
                        template_line=line_number,
                    )

                client = paramiko.SSHClient()
                _configure_ssh_client(client, run_context.host_key_policy)
                try:
                    client.connect(
                        host,
                        port=int(port),
                        username=username,
                        password=password,
                        timeout=int(timeout),
                        **_connect_auth_kwargs(password),
                    )
                    transport = client.get_transport()
                    if transport is not None:
                        transport.set_keepalive(KEEPALIVE_INTERVAL)
                    session = client.invoke_shell(width=shell_width, height=shell_height)
                except Exception as exc:
                    raise SshTempConnectError(
                        f"failed to connect to {host}:{port}: {exc}",
                        host=host,
                        template_line=line_number,
                    ) from exc

                logger.info(
                    "sshtemp.session_opened",
                    extra=_log_extra(
                        host,
                        log_id=run_context.log_id,
                        port=port,
                        username=username,
                    ),
                )
                _settle_login_output(
                    session,
                    host=host,
                    wait_seconds=max(
                        float(default_wait_seconds),
                        float(login_settle_seconds),
                    ),
                    idle_seconds=float(login_settle_seconds),
                    expect_pattern=expect_pattern,
                    line_number=line_number,
                    context=run_context,
                    recv_buffer_size=recv_buffer_size,
                    recv_encoding=recv_encoding,
                )

            wait_seconds, retry_count = _consume_command_timing()

            display_command = _display_command(line)
            active_capture = capture_name if capturing else None

            if run_context.verbose:
                logger.info(
                    "sshtemp.command_sent",
                    extra=_log_extra(
                        host,
                        log_id=run_context.log_id,
                        template_line=line_number,
                        command=display_command,
                        capture_name=active_capture,
                        pattern=expect_pattern or None,
                        wait_seconds=wait_seconds,
                        max_attempts=retry_count,
                    ),
                )

            if capture_leading_enter:
                line = "\n" + line
                capture_leading_enter = False

            if pending_if is not None:
                raise SshTempTemplateError(
                    f"missing {directive_prefix}THEN for {directive_prefix}IF at line {line_number}",
                    host=host,
                    template_line=line_number,
                    command=line,
                )

            current_if_rules = list(if_rules)
            if_rules = []
            responded = [False] * len(current_if_rules)

            recv_text = ""
            attempts_left = retry_count
            attempt_number = 0
            while attempts_left > 0:
                attempt_number += 1
                attempts_left -= 1
                session.send(line + "\n")
                outcome = _poll_command_output(
                    session,
                    host=host,
                    wait_seconds=wait_seconds,
                    current_if_rules=current_if_rules,
                    responded=responded,
                    expect_pattern=expect_pattern,
                    error_pattern=error_pattern,
                    error_fail=error_fail,
                    line_number=line_number,
                    command=line,
                    capture_name=active_capture,
                    context=run_context,
                    recv_buffer_size=recv_buffer_size,
                    recv_encoding=recv_encoding,
                )
                recv_text = outcome.recv_text
                if capturing:
                    capture_buffer += recv_text

                if expect_pattern and re.search(expect_pattern, recv_text):
                    break

                if outcome.channel_closed:
                    if expect_pattern:
                        capture_part = (
                            f", capture {active_capture!r}" if active_capture else ""
                        )
                        raise SshTempTimeoutError(
                            f'channel closed before expect pattern "{expect_pattern}" '
                            f"matched at line {line_number}{capture_part}, "
                            f"command {display_command!r}",
                            host=host,
                            template_line=line_number,
                            command=display_command,
                            capture_name=active_capture,
                        )
                    break

                if expect_pattern and attempts_left > 0:
                    logger.warning(
                        "sshtemp.command_retry",
                        extra=_log_extra(
                            host,
                            log_id=run_context.log_id,
                            template_line=line_number,
                            command=display_command,
                            capture_name=active_capture,
                            attempt=attempt_number,
                            max_attempts=retry_count,
                            wait_seconds=wait_seconds,
                            pattern=expect_pattern,
                            output_preview=_preview_output(recv_text),
                        ),
                    )
                    responded = [False] * len(current_if_rules)
                    continue

                if expect_pattern:
                    if run_context.verbose:
                        logger.info(
                            "sshtemp.expect_not_matched",
                            extra=_log_extra(
                                host,
                                log_id=run_context.log_id,
                                template_line=line_number,
                                command=display_command,
                                capture_name=active_capture,
                                attempt=attempt_number,
                                max_attempts=retry_count,
                                wait_seconds=wait_seconds,
                                pattern=expect_pattern,
                                output_preview=_preview_output(recv_text),
                            ),
                        )
                    capture_part = (
                        f", capture {active_capture!r}" if active_capture else ""
                    )
                    raise SshTempTimeoutError(
                        f'expect pattern "{expect_pattern}" not matched within '
                        f"{wait_seconds}s after {retry_count} attempt(s) at "
                        f"line {line_number}{capture_part}, command {display_command!r}",
                        host=host,
                        template_line=line_number,
                        command=display_command,
                        capture_name=active_capture,
                    )
                break

        return None

    except SshTempError as exc:
        _stash_open_capture(
            run_context,
            capturing=capturing,
            capture_name=capture_name,
            capture_buffer=capture_buffer,
        )
        logger.error(
            "sshtemp.run_failed",
            extra=_log_extra(
                host,
                log_id=run_context.log_id,
                template_line=exc.template_line,
                command=exc.command,
                capture_name=exc.capture_name,
                error=str(exc),
            ),
        )
        raise

    except Exception as exc:
        _stash_open_capture(
            run_context,
            capturing=capturing,
            capture_name=capture_name,
            capture_buffer=capture_buffer,
        )
        logger.exception(
            "sshtemp.unexpected_error",
            extra=_log_extra(host, log_id=run_context.log_id),
        )
        raise SshTempConnectError(
            f"unexpected error: {exc}",
            host=host,
        ) from exc

    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                logger.debug(
                    "sshtemp.session_close_failed",
                    extra=_log_extra(host, log_id=run_context.log_id),
                )
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug(
                    "sshtemp.client_close_failed",
                    extra=_log_extra(host, log_id=run_context.log_id),
                )


def _default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(f"output_{stamp}")


def _prepare_output_dir(output_dir: str | Path | None) -> Path:
    out_dir = Path(output_dir) if output_dir else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _resolve_capture_path(out_dir: Path, name: str) -> Path:
    out_root = out_dir.resolve()
    relative = Path(name.strip().replace(os.sep, "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"invalid capture path: {name!r}")
    file_path = (out_root / relative).resolve()
    if not file_path.is_relative_to(out_root):
        raise ValueError(f"invalid capture path: {name!r}")
    return file_path


def _append_capture_file(path: Path, output: str) -> None:
    with _capture_file_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = output if output else ""
        if path.exists() and path.stat().st_size > 0:
            existing = path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
            path.write_text(existing + text, encoding="utf-8")
        else:
            path.write_text(text, encoding="utf-8")


def _merge_capture(captures: dict[str, str], name: str, output: str) -> None:
    if name in captures:
        captures[name] = captures[name] + "\n" + output
    else:
        captures[name] = output


def _assign_capture_name(raw_name: str, unnamed_index: int) -> tuple[str, int]:
    name = raw_name.strip()
    if not name:
        name = f"capture_{unnamed_index}.txt"
        unnamed_index += 1
    return name, unnamed_index


def _commit_partial_capture(
    context: SshTempRunContext,
    captures: dict[str, str],
    unnamed_index: int,
    *,
    save_to_disk: bool,
    out_dir: Path | None,
) -> int:
    item = context.partial_capture
    if item is None:
        return unnamed_index
    name, unnamed_index = _assign_capture_name(item.name, unnamed_index)
    _merge_capture(captures, name, item.output)
    if save_to_disk and out_dir is not None:
        try:
            _append_capture_file(_resolve_capture_path(out_dir, name), item.output)
        except (OSError, ValueError) as exc:
            _record_warning(
                context,
                f"failed to write partial capture {name!r}: {exc}",
                host=context.host,
            )
    _record_warning(
        context,
        f"capture {name!r} saved partially (run failed before _END)",
        host=context.host,
    )
    return unnamed_index


def _is_inline_template(text: str) -> bool:
    return "\n" in text or "\r" in text


def _read_template_file(path: Path) -> tuple[str | None, str | None]:
    try:
        if not path.is_file():
            return None, f"template file not found: {path}"
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"failed to read template: {exc}"


def _log_template_load_error(load_error: str, log_id: str | None) -> None:
    event = (
        "sshtemp.template_not_found"
        if load_error.startswith("template file not found")
        else "sshtemp.template_read_error"
    )
    logger.error(
        event,
        extra=_log_extra(log_id=log_id, error=load_error),
    )


def _resolve_template_text(
    command_temp_text: str | Path,
) -> tuple[str | None, str | None]:
    """Return (template_text, error_message).

    Multi-line strings are inline templates. A single-line string is a file path.
    ``Path`` is accepted for tests and internal callers.
    """
    if isinstance(command_temp_text, Path):
        return _read_template_file(command_temp_text)

    text = command_temp_text
    if _is_inline_template(text):
        return text, None

    return _read_template_file(Path(text.strip()))


def ssht(
    command_temp_text: str | Path,
    directive_prefix: str = "_",
    *,
    log_id: str | None = None,
    verbose: bool = False,
    save_to_disk: bool = False,
    output_dir: str | Path | None = None,
    host_key_policy: str = DEFAULT_HOST_KEY_POLICY,
    manage_logging: bool = True,
) -> SshTempResult:
    """Run a template and return an aggregated SshTempResult."""
    try:
        host_key_policy = _normalize_host_key_policy(host_key_policy)
    except ValueError as exc:
        if manage_logging:
            configure_logging(verbose=verbose)
        logger.error(
            "sshtemp.invalid_host_key_policy",
            extra=_log_extra(log_id=log_id, error=str(exc)),
        )
        return SshTempResult(success=False, log_id=log_id, error=str(exc))

    template_text, load_error = _resolve_template_text(command_temp_text)
    if load_error:
        if save_to_disk:
            try:
                out_dir = _prepare_output_dir(output_dir)
                if manage_logging:
                    configure_logging(
                        verbose=verbose,
                        log_file=out_dir / LOG_FILE_NAME,
                        append=False,
                    )
            except OSError as exc:
                logger.error(
                    "sshtemp.output_write_error",
                    extra=_log_extra(log_id=log_id, error=str(exc)),
                )
                if manage_logging:
                    shutdown_logging()
                return SshTempResult(
                    success=False,
                    log_id=log_id,
                    error=_format_write_error(exc),
                )
            _log_template_load_error(load_error, log_id)
            result = SshTempResult(
                success=False,
                output_dir=out_dir,
                log_id=log_id,
                error=load_error,
            )
            if manage_logging:
                shutdown_logging()
            return result
        if manage_logging:
            configure_logging(verbose=verbose)
        _log_template_load_error(load_error, log_id)
        return SshTempResult(success=False, log_id=log_id, error=load_error)

    out_dir: Path | None = None
    try:
        if manage_logging:
            if save_to_disk:
                out_dir = _prepare_output_dir(output_dir)
                configure_logging(
                    verbose=verbose,
                    log_file=out_dir / LOG_FILE_NAME,
                    append=False,
                )
            else:
                configure_logging(verbose=verbose)
        elif save_to_disk:
            out_dir = _prepare_output_dir(output_dir)
    except OSError as exc:
        logger.error(
            "sshtemp.output_write_error",
            extra=_log_extra(log_id=log_id, error=str(exc)),
        )
        if manage_logging:
            shutdown_logging()
        return SshTempResult(
            success=False,
            output_dir=out_dir,
            log_id=log_id,
            error=_format_write_error(exc),
        )

    validation = validate_template(template_text, directive_prefix=directive_prefix)
    if not validation.valid:
        for issue in validation.issues:
            logger.error(
                "sshtemp.template_validation_error",
                extra=_log_extra(log_id=log_id, error=issue),
            )
        result = SshTempResult(
            success=False,
            output_dir=out_dir,
            log_id=log_id,
            error=_format_validation_error(validation.issues),
        )
        if manage_logging and save_to_disk:
            shutdown_logging()
        return result

    context = SshTempRunContext(
        log_id=log_id,
        verbose=verbose,
        host_key_policy=host_key_policy,
    )
    context.warnings.extend(validation.warnings)
    for warning in validation.warnings:
        logger.warning(
            "sshtemp.warning",
            extra=_log_extra(log_id=context.log_id, error=warning),
        )
    captures: dict[str, str] = {}
    unnamed_capture_index = 0

    try:
        for item in _ssh_temp(
            template_text,
            directive_prefix=directive_prefix,
            context=context,
            skip_validation=True,
        ):
            name, unnamed_capture_index = _assign_capture_name(
                item.name, unnamed_capture_index
            )
            if save_to_disk:
                file_path = _resolve_capture_path(out_dir, name)
                _append_capture_file(file_path, item.output)
            _merge_capture(captures, name, item.output)

        result = SshTempResult(
            success=True,
            output_dir=out_dir,
            host=context.host,
            log_id=context.log_id,
            captures=captures,
            warnings=list(context.warnings),
        )
    except SshTempError as exc:
        unnamed_capture_index = _commit_partial_capture(
            context,
            captures,
            unnamed_capture_index,
            save_to_disk=save_to_disk,
            out_dir=out_dir,
        )
        result = SshTempResult(
            success=False,
            output_dir=out_dir,
            host=context.host or exc.host,
            log_id=context.log_id,
            captures=captures,
            warnings=list(context.warnings),
            error=str(exc),
            failed_line=exc.template_line,
            failed_command=exc.command,
            failed_capture_name=exc.capture_name,
        )
    except ValueError as exc:
        unnamed_capture_index = _commit_partial_capture(
            context,
            captures,
            unnamed_capture_index,
            save_to_disk=save_to_disk,
            out_dir=out_dir,
        )
        result = SshTempResult(
            success=False,
            output_dir=out_dir,
            host=context.host,
            log_id=context.log_id,
            captures=captures,
            warnings=list(context.warnings),
            error=str(exc),
        )
    except OSError as exc:
        unnamed_capture_index = _commit_partial_capture(
            context,
            captures,
            unnamed_capture_index,
            save_to_disk=save_to_disk,
            out_dir=out_dir,
        )
        logger.error(
            "sshtemp.output_write_error",
            extra=_log_extra(
                context.host,
                log_id=context.log_id,
                error=str(exc),
            ),
        )
        result = SshTempResult(
            success=False,
            output_dir=out_dir,
            host=context.host,
            log_id=context.log_id,
            captures=captures,
            warnings=list(context.warnings),
            error=_format_write_error(exc),
        )

    if manage_logging and save_to_disk:
        shutdown_logging()
    return result
