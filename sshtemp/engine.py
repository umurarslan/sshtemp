import logging
import os
import re
import threading
import time
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
KEEPALIVE_INTERVAL = 60
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


def _poll_command_output(
    session,
    *,
    host: str | None,
    poll_expect_interval: float,
    polls_left: int,
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
) -> tuple[str, bool]:
    """Poll shell output. Returns (recv_text, error_seen_without_fail)."""
    recv_buffer = b""
    recv_text = ""
    had_error = False

    while polls_left > 0:
        time.sleep(poll_expect_interval)
        polls_left -= 1
        if not session.recv_ready():
            continue

        recv_buffer += session.recv(recv_buffer_size)
        recv_text = recv_buffer.decode(recv_encoding, errors="ignore")

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
            break

    return recv_text, had_error


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

    poll_expect_interval = 0.5
    default_wait_seconds = DEFAULT_WAIT_SECONDS
    default_connect_timeout = 10
    default_retry_count = 1

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
            elif line.startswith(directive_prefix + "#"):
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
            elif line.startswith(directive_prefix + "ENTER"):
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
            elif line.startswith(directive_prefix + "CAPTURE"):
                if pending_wait is not None:
                    capture_block_wait = pending_wait
                    pending_wait = None
                if pending_retry is not None:
                    capture_block_retry = pending_retry
                    pending_retry = None
                capturing = True
                capture_leading_enter = True
                capture_name = ""
                run_context.active_capture_name = None
                if line.startswith(directive_prefix + "CAPTURE:"):
                    capture_name = line.removeprefix(directive_prefix + "CAPTURE:").strip()
                    run_context.active_capture_name = capture_name
                continue
            elif line.startswith(directive_prefix + "END"):
                if capturing:
                    capture_buffer_copy = capture_buffer
                    capturing = False
                    capture_buffer = ""
                    run_context.active_capture_name = None
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

            if line.startswith(directive_prefix):
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
                polls_left = max(1, int(wait_seconds / poll_expect_interval))

                recv_text, _had_error = _poll_command_output(
                    session,
                    host=host,
                    poll_expect_interval=poll_expect_interval,
                    polls_left=polls_left,
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

                if expect_pattern and re.search(expect_pattern, recv_text):
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

            if capturing:
                capture_buffer += recv_text

        return None

    except SshTempError as exc:
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


def _is_inline_template(text: str) -> bool:
    return "\n" in text or "\r" in text


def _resolve_template_text(
    command_temp_text: str | Path,
) -> tuple[str | None, str | None]:
    """Return (template_text, error_message).

    Multi-line strings are inline templates. A single-line string is a file path.
    ``Path`` is accepted for tests and internal callers.
    """
    if isinstance(command_temp_text, Path):
        if not command_temp_text.is_file():
            return None, f"template file not found: {command_temp_text}"
        return command_temp_text.read_text(encoding="utf-8"), None

    text = command_temp_text
    if _is_inline_template(text):
        return text, None

    path = Path(text.strip())
    if not path.is_file():
        return None, f"template file not found: {text.strip()}"
    return path.read_text(encoding="utf-8"), None


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
            out_dir = _prepare_output_dir(output_dir)
            if manage_logging:
                configure_logging(
                    verbose=verbose,
                    log_file=out_dir / LOG_FILE_NAME,
                    append=False,
                )
            logger.error(
                "sshtemp.template_not_found",
                extra=_log_extra(log_id=log_id, error=load_error),
            )
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
        logger.error(
            "sshtemp.template_not_found",
            extra=_log_extra(log_id=log_id, error=load_error),
        )
        return SshTempResult(success=False, log_id=log_id, error=load_error)

    out_dir: Path | None = None
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

    try:
        for item in _ssh_temp(
            template_text,
            directive_prefix=directive_prefix,
            context=context,
            skip_validation=True,
        ):
            name = item.name.strip() or f"capture_{len(captures)}.txt"
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
        result = SshTempResult(
            success=False,
            output_dir=out_dir,
            host=context.host,
            log_id=context.log_id,
            captures=captures,
            warnings=list(context.warnings),
            error=str(exc),
        )

    if manage_logging and save_to_disk:
        shutdown_logging()
    return result
