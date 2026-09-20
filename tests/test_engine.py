import logging
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sshtemp import ssht
from sshtemp.models import SshTempCapture
from sshtemp.exceptions import SshTempErrorPattern
from sshtemp.engine import _poll_command_output, _settle_login_output
from sshtemp.logging_config import SshTempFormatter, configure_logging, shutdown_logging
from sshtemp.models import SshTempRunContext


def _as_chunks(data: bytes | list[bytes] | None) -> list[bytes]:
    if data is None:
        return []
    if isinstance(data, bytes):
        return [data]
    return list(data)


def _scripted_session(
    *responses: bytes | list[bytes],
    initial: bytes | list[bytes] | None = None,
    eof: bool = False,
) -> MagicMock:
    """Session whose next queued response appears after a send() that finds the recv queue empty.

    An empty recv queue raises ``socket.timeout`` after ``settimeout`` elapses.
    Data staged in *responses* is not visible until that send, so a recv that
    finds both the live queue and the send-staged queue empty-of-live-data
    times out immediately (login settle must not wait for the first command).
    Empty bytes (EOF) are returned only when *eof* is set or the channel is closed.
    """
    session = MagicMock()
    remaining = [_as_chunks(item) for item in responses]
    queue = _as_chunks(initial)
    state = {"closed": False, "timeout": None}
    data_event = threading.Event()

    def _close_if_exhausted() -> None:
        if eof and not queue and not remaining:
            state["closed"] = True

    def _notify() -> None:
        if queue or state["closed"]:
            data_event.set()
        else:
            data_event.clear()

    def send(_data):
        if not queue and remaining:
            queue.extend(remaining.pop(0))
        _close_if_exhausted()
        _notify()

    def recv_ready():
        return bool(queue)

    def recv(_size=65535):
        if queue:
            chunk = queue.pop(0)
            _close_if_exhausted()
            _notify()
            return chunk
        if state["closed"] or (eof and not remaining):
            state["closed"] = True
            return b""
        if remaining:
            # Next command's payload is not on the wire until send().
            raise socket.timeout()
        timeout = state["timeout"]
        if not isinstance(timeout, (int, float)):
            raise RuntimeError("recv() with no data requires settimeout(seconds)")
        if timeout > 0 and data_event.wait(timeout):
            if queue:
                chunk = queue.pop(0)
                _close_if_exhausted()
                _notify()
                return chunk
            if state["closed"]:
                return b""
        raise socket.timeout()

    def exit_status_ready():
        return state["closed"]

    def gettimeout():
        return state["timeout"]

    def settimeout(timeout):
        state["timeout"] = timeout

    def deliver(data: bytes) -> None:
        queue.append(data)
        _notify()

    _close_if_exhausted()
    _notify()

    session.send.side_effect = send
    session.recv_ready.side_effect = recv_ready
    session.recv.side_effect = recv
    session.exit_status_ready.side_effect = exit_status_ready
    session.gettimeout.side_effect = gettimeout
    session.settimeout.side_effect = settimeout
    session.deliver = deliver
    return session


def _wired_ssh(mock_ssh_client_cls, session: MagicMock) -> MagicMock:
    client = MagicMock()
    mock_ssh_client_cls.return_value = client
    client.connect.return_value = None
    client.invoke_shell.return_value = session
    client.get_transport.return_value = MagicMock()
    return client


def _poll(session, **overrides):
    kwargs = dict(
        host="10.0.0.1",
        wait_seconds=0,
        current_if_rules=[],
        responded=[],
        expect_pattern="",
        error_pattern="",
        error_fail="off",
        line_number=1,
        command="bad",
        context=SshTempRunContext(),
        capture_name=None,
        recv_buffer_size=65535,
        recv_encoding="ascii",
    )
    kwargs.update(overrides)
    return _poll_command_output(session, **kwargs)


class LoggingFormatterTests(unittest.TestCase):
    def _make_record(self) -> logging.LogRecord:
        record = logging.LogRecord(
            name="sshtemp.engine",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="sshtemp.run_failed",
            args=(),
            exc_info=None,
        )
        record.log_id = "myhost2"
        record.error = "timeout"
        return record

    def test_log_id_in_console_log_format(self):
        formatter = SshTempFormatter()
        formatted = formatter.format(self._make_record())
        self.assertTrue(
            formatted.startswith("ERROR:myhost2:sshtemp.engine:sshtemp.run_failed")
        )
        self.assertNotRegex(formatted, r"^\d{4}-\d{2}-\d{2}T")

    def test_file_log_format_includes_utc_timestamp(self):
        formatter = SshTempFormatter(include_timestamp=True)
        formatted = formatter.format(self._make_record())
        self.assertRegex(
            formatted,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z "
            r"ERROR:myhost2:sshtemp.engine:sshtemp.run_failed",
        )


class LoggingHygieneTests(unittest.TestCase):
    """save_to_disk=False must not write files or disturb the caller's handlers."""

    def setUp(self) -> None:
        self.logger = logging.getLogger("sshtemp")
        self.records: list[logging.LogRecord] = []

        collector = logging.Handler()
        collector.emit = self.records.append
        self.collector = collector

    def tearDown(self) -> None:
        self.logger.removeHandler(self.collector)
        shutdown_logging()

    def _run_failing_template(self) -> None:
        # Fails validation, so no SSH connection is attempted.
        ssht("_HOST: 10.0.0.1\n_USERNAME: admin\n_TIMEOUT: abc\nshow version")

    def test_no_log_file_written_when_save_to_disk_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                self._run_failing_template()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_caller_handler_survives_and_receives_records(self):
        self.logger.addHandler(self.collector)

        self._run_failing_template()

        self.assertIn(self.collector, self.logger.handlers)
        self.assertTrue(
            any(
                "template_validation_error" in record.getMessage()
                for record in self.records
            )
        )

    def test_repeated_runs_do_not_accumulate_handlers(self):
        self._run_failing_template()
        handler_count = len(self.logger.handlers)

        self._run_failing_template()
        self._run_failing_template()

        self.assertEqual(len(self.logger.handlers), handler_count)


class SshtValidationTests(unittest.TestCase):
    def test_no_env_substitution_in_ssht(self):
        result = ssht("_HOST: 10.0.0.1\n_USERNAME: admin\n_PASSWORD: ${MISSING}\n")
        self.assertNotIn("missing environment variable", result.error or "")

    def test_empty_directive_prefix(self):
        result = ssht(
            "_HOST: 10.0.0.1\n_USERNAME: admin\n",
            directive_prefix="",
        )
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_missing_host(self):
        template = """
_USERNAME: admin
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_unknown_directive(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_xEXPECT: prompt
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_ending_is_unknown_directive(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_CAPTURE:out
show version
_ENDING
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")
        self.assertIn("unknown directive", result.error or "")

    def test_invalid_shell(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_SHELL: bad
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_invalid_recv_buffer(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_RECV_BUFFER: abc
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_invalid_encoding(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_ENCODING: not-a-codec
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")
        self.assertIn("_ENCODING", result.error or "")

    def test_empty_logid(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_LOGID:
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_if_without_then(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_IF: password
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_invalid_retry(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_RETRY: abc
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_invalid_sleep(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_SLEEP: abc
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")
        self.assertIn("_SLEEP", result.error or "")

    def test_invalid_settle(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_SETTLE: abc
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")
        self.assertIn("_SETTLE", result.error or "")

    def test_invalid_if_regex(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_IF: [unclosed
_THEN: secret
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")
        self.assertIn("_IF", result.error or "")

    def test_invalid_port(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_PORT: abc
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")
        self.assertIn("_PORT", result.error or "")

    def test_invalid_timeout(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_TIMEOUT:
show version
"""
        result = ssht(template)
        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")
        self.assertIn("_TIMEOUT", result.error or "")


class PollCommandOutputTests(unittest.TestCase):
    def test_error_fail_raises(self):
        session = _scripted_session(initial=b"MINOR: bad command\n")
        context = SshTempRunContext()

        with self.assertRaises(SshTempErrorPattern):
            _poll(
                session,
                context=context,
                error_pattern=r"(?i)minor:",
                error_fail="on",
            )

    def test_error_without_fail_records_warning(self):
        session = _scripted_session(initial=b"MINOR: bad\nA:host#\n")
        context = SshTempRunContext()

        outcome = _poll(
            session,
            context=context,
            expect_pattern=r"A:host#",
            error_pattern=r"(?i)minor:",
            error_fail="off",
        )

        self.assertTrue(outcome.had_error)
        self.assertEqual(len(context.warnings), 1)
        self.assertIn("MINOR", outcome.recv_text)

    def test_error_without_fail_logs_warning_immediately(self):
        session = _scripted_session(initial=b"MINOR: bad\nA:host#\n")
        context = SshTempRunContext(log_id="device-a")

        with self.assertLogs("sshtemp.engine", level="WARNING") as logs:
            _poll(
                session,
                context=context,
                expect_pattern=r"A:host#",
                error_pattern=r"(?i)minor:",
                error_fail="off",
            )

        warning_logs = [r for r in logs.records if r.message == "sshtemp.warning"]
        self.assertEqual(len(warning_logs), 1)
        self.assertEqual(warning_logs[0].log_id, "device-a")

    def test_drains_all_ready_chunks(self):
        session = _scripted_session(initial=[b"part1 ", b"part2\nA:host#\n"])
        start = time.monotonic()
        outcome = _poll(
            session,
            wait_seconds=5,
            expect_pattern=r"A:host#",
            command="show",
        )
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertIn("part1", outcome.recv_text)
        self.assertIn("part2", outcome.recv_text)
        self.assertFalse(outcome.channel_closed)

    def test_channel_eof_returns_closed_without_waiting(self):
        session = _scripted_session(initial=b"bye\n", eof=True)
        start = time.monotonic()
        outcome = _poll(
            session,
            wait_seconds=5,
            expect_pattern=r"PROMPT#",
            command="logout",
        )
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertTrue(outcome.channel_closed)
        self.assertIn("bye", outcome.recv_text)

    def test_wait_is_wall_clock(self):
        session = _scripted_session(initial=b"nope\n")
        start = time.monotonic()
        outcome = _poll(
            session,
            wait_seconds=0.25,
            expect_pattern=r"PROMPT#",
            command="show",
        )
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 1.0)
        self.assertFalse(outcome.channel_closed)

    def test_wakes_when_data_arrives_before_deadline(self):
        session = _scripted_session()

        def _deliver() -> None:
            time.sleep(0.08)
            session.deliver(b"A:host#\n")

        threading.Thread(target=_deliver, daemon=True).start()
        start = time.monotonic()
        outcome = _poll(
            session,
            wait_seconds=2,
            expect_pattern=r"A:host#",
            command="show",
        )
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertIn("A:host#", outcome.recv_text)
        self.assertFalse(outcome.channel_closed)

    def test_restores_channel_timeout_after_poll(self):
        session = _scripted_session(initial=b"nope\n")
        session.settimeout(7.5)
        _poll(
            session,
            wait_seconds=0.05,
            expect_pattern=r"PROMPT#",
            command="show",
        )
        self.assertEqual(session.gettimeout(), 7.5)


def _settle(session, **overrides):
    kwargs = dict(
        host="10.0.0.1",
        wait_seconds=5,
        idle_seconds=2,
        expect_pattern="",
        line_number=1,
        context=SshTempRunContext(),
        recv_buffer_size=65535,
        recv_encoding="ascii",
    )
    kwargs.update(overrides)
    return _settle_login_output(session, **kwargs)


class LoginSettleTests(unittest.TestCase):
    def test_consumes_banner_matching_expect_without_send(self):
        session = _scripted_session(initial=b"Welcome\nA:host#\n")
        start = time.monotonic()
        _settle(session, expect_pattern=r"A:host#")
        self.assertLess(time.monotonic() - start, 0.3)
        session.send.assert_not_called()

    def test_quiet_device_returns_without_failing(self):
        session = _scripted_session(b"A:host#\n")
        start = time.monotonic()
        _settle(session, expect_pattern=r"A:host#", idle_seconds=0.2, wait_seconds=2)
        self.assertLess(time.monotonic() - start, 0.3)
        session.send.assert_not_called()

    def test_does_not_apply_error_pattern(self):
        session = _scripted_session(initial=b"MINOR: banner\nA:host#\n")
        context = SshTempRunContext()
        _settle(
            session,
            expect_pattern=r"A:host#",
            context=context,
        )
        self.assertEqual(context.warnings, [])

    def test_idle_after_banner_when_expect_does_not_match(self):
        session = _scripted_session(initial=b"jumpuser@jump:~\n")
        start = time.monotonic()
        _settle(
            session,
            expect_pattern=r"#\s*$",
            idle_seconds=0.2,
            wait_seconds=5,
        )
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.0)
        session.send.assert_not_called()

    def test_wakes_when_banner_arrives_before_idle(self):
        session = _scripted_session()

        def _deliver() -> None:
            time.sleep(0.08)
            session.deliver(b"Welcome\nA:host#\n")

        threading.Thread(target=_deliver, daemon=True).start()
        start = time.monotonic()
        _settle(session, expect_pattern=r"A:host#", idle_seconds=2, wait_seconds=2)
        self.assertLess(time.monotonic() - start, 0.5)
        session.send.assert_not_called()


class SshtRuntimeTests(unittest.TestCase):
    def test_invalid_template_skips_connect(self):
        template = """
_USERNAME: admin
show version
"""
        result = ssht(template)

        self.assertFalse(result.success)
        self.assertIn("template validation failed", result.error or "")

    def test_log_id_on_result(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
"""
        result = ssht(template, log_id="router-a")
        self.assertEqual(result.log_id, "router-a")

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_template_logid_overrides_ssht_logid(self, mock_ssh_client_cls):
        _wired_ssh(mock_ssh_client_cls, _scripted_session(b"A:host#\n"))

        template = r"""
_LOGID: from-template
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template, log_id="from-ssht")

        self.assertTrue(result.success)
        self.assertEqual(result.log_id, "from-template")

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_shell_and_recv_buffer_directives(self, mock_ssh_client_cls):
        session = _scripted_session(b"A:host#\n")
        client = _wired_ssh(mock_ssh_client_cls, session)

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_SHELL: 132x43
_RECV_BUFFER: 8192
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        client.invoke_shell.assert_called_once_with(width=132, height=43)
        session.recv.assert_called_with(8192)
        client.get_transport.return_value.set_keepalive.assert_called_once_with(60)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_encoding_directive(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session("ü".encode("utf-8") + b"\nA:host#\n"),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_ENCODING: utf-8
_EXPECT: A:host#\s*$
_CAPTURE:out
show version
_END
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertIn("ü", result.captures["out"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_default_encoding_is_utf8(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session("ü".encode("utf-8") + b"\nA:host#\n"),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
_CAPTURE:out
show version
_END
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertIn("ü", result.captures["out"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_password_connect_disables_agent_and_keys(self, mock_ssh_client_cls):
        client = _wired_ssh(
            mock_ssh_client_cls, _scripted_session(b"A:host#\n")
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: secret
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        connect_kwargs = client.connect.call_args.kwargs
        self.assertFalse(connect_kwargs["allow_agent"])
        self.assertFalse(connect_kwargs["look_for_keys"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_login_banner_is_not_captured(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(
                b"A:host# show version\nTiMOS\nA:host#\n",
                initial=b"Welcome to TiMOS\nMINOR: motd\nA:host#\n",
            ),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
_ERROR:(?i)minor:
_CAPTURE:out
show version
_END
"""

        result = ssht(template)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.warnings, [])
        self.assertNotIn("Welcome to TiMOS", result.captures["out"])
        self.assertNotIn("MINOR: motd", result.captures["out"])
        self.assertIn("TiMOS", result.captures["out"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_quiet_login_still_runs_first_command(self, mock_ssh_client_cls):
        start = time.monotonic()
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(b"A:host# show version\nok\nA:host#\n"),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_EXPECT: A:host#\s*$
_CAPTURE:out
show version
_END
"""

        result = ssht(template)

        self.assertTrue(result.success, result.error)
        self.assertLess(time.monotonic() - start, 0.8)
        self.assertIn("ok", result.captures["out"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    @patch("sshtemp.engine._settle_login_output")
    def test_settle_directive_sets_idle(self, mock_settle, mock_ssh_client_cls):
        mock_settle.return_value = None
        _wired_ssh(mock_ssh_client_cls, _scripted_session(b"A:host#\n"))

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_SETTLE: 8
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success, result.error)
        self.assertEqual(mock_settle.call_args.kwargs["idle_seconds"], 8.0)
        self.assertEqual(mock_settle.call_args.kwargs["wait_seconds"], 20.0)

    @patch("sshtemp.engine.paramiko.SSHClient")
    @patch("sshtemp.engine._settle_login_output")
    def test_default_settle_idle_is_two_seconds(self, mock_settle, mock_ssh_client_cls):
        mock_settle.return_value = None
        _wired_ssh(mock_ssh_client_cls, _scripted_session(b"A:host#\n"))

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success, result.error)
        self.assertEqual(mock_settle.call_args.kwargs["idle_seconds"], 2.0)

    @patch("sshtemp.engine.paramiko.SSHClient")
    @patch("sshtemp.engine._settle_login_output")
    def test_settle_larger_than_default_wait_extends_login_budget(
        self, mock_settle, mock_ssh_client_cls
    ):
        mock_settle.return_value = None
        _wired_ssh(mock_ssh_client_cls, _scripted_session(b"A:host#\n"))

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_SETTLE: 30
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success, result.error)
        self.assertEqual(mock_settle.call_args.kwargs["idle_seconds"], 30.0)
        self.assertEqual(mock_settle.call_args.kwargs["wait_seconds"], 30.0)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_timeout_includes_capture_name(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls, _scripted_session(b"still running...\n")
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_EXPECT: A:host#\s*$
_CAPTURE:big_config
admin display-config
_END
"""

        result = ssht(template)

        self.assertFalse(result.success)
        self.assertIn("capture 'big_config'", result.error or "")
        self.assertIn("admin display-config", result.error or "")
        self.assertEqual(result.failed_line, 7)
        self.assertEqual(result.failed_command, "admin display-config")
        self.assertEqual(result.failed_capture_name, "big_config")
        self.assertIn("still running...", result.captures.get("big_config", ""))
        self.assertTrue(
            any("saved partially" in warning for warning in result.warnings)
        )

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_open_capture_written_to_disk_on_failure(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls, _scripted_session(b"still running...\n")
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_EXPECT: A:host#\s*$
_CAPTURE:big_config
admin display-config
_END
"""

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            result = ssht(template, save_to_disk=True, output_dir=out_dir)

            self.assertFalse(result.success)
            self.assertTrue((out_dir / "big_config").exists())
            self.assertIn(
                "still running...",
                (out_dir / "big_config").read_text(encoding="utf-8"),
            )

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_unnamed_open_capture_saved_on_failure(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls, _scripted_session(b"partial dump\n")
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_EXPECT: A:host#\s*$
_CAPTURE
show version
_END
"""

        result = ssht(template)

        self.assertFalse(result.success)
        self.assertIn("partial dump", result.captures.get("capture_0.txt", ""))
        self.assertTrue(
            any("capture_0.txt" in warning and "saved partially" in warning
                for warning in result.warnings)
        )

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_channel_closed_before_expect_fails_fast(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(b"Connection closed\n", eof=True),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 20
_EXPECT: A:host#\s*$
logout
"""

        start = time.monotonic()
        result = ssht(template)
        elapsed = time.monotonic() - start

        self.assertFalse(result.success)
        self.assertIn("channel closed", result.error or "")
        self.assertLess(elapsed, 2.0)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_errorfail_stops_template(self, mock_ssh_client_cls):
        session = _scripted_session([b"A:host# ", b"MINOR: invalid port\n"])
        _wired_ssh(mock_ssh_client_cls, session)

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_ERROR:(?i)minor:
bad-command
_CAPTURE:never
show version
_END
"""

        result = ssht(template, log_id="lab1")

        self.assertFalse(result.success)
        self.assertIsInstance(result.error, str)
        self.assertIn("minor:", result.error or "")
        self.assertEqual(result.failed_line, 5)
        self.assertEqual(result.failed_command, "bad-command")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("_EXPECT", result.warnings[0])
        self.assertEqual(result.log_id, "lab1")
        session.send.assert_called_once()

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_errorfail_off_override_warns_only(self, mock_ssh_client_cls):
        session = _scripted_session(
            b"MINOR: invalid port\nA:host# ",
            b"show version\nok\nA:host# ",
        )
        _wired_ssh(mock_ssh_client_cls, session)

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-command
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("minor:", result.warnings[0])
        self.assertEqual(session.send.call_count, 2)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_runtime_warnings_log_with_log_id_at_match_time(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(
                b"MINOR: on device-a\nA:host# ",
                b"ok\nA:host# ",
                b"MINOR: on device-b\nA:host# ",
                b"ok2\nA:host# ",
            ),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
_LOGID: device-a
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-a
show ok

_LOGID: device-b
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-b
show ok2
"""

        with self.assertLogs("sshtemp.engine", level="WARNING") as logs:
            result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(len(result.warnings), 2)
        warning_logs = [r for r in logs.records if r.message == "sshtemp.warning"]
        self.assertEqual(len(warning_logs), 2)
        self.assertEqual(warning_logs[0].log_id, "device-a")
        self.assertEqual(warning_logs[1].log_id, "device-b")

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_error_clear_turns_off_errorfail(self, mock_ssh_client_cls):
        session = _scripted_session(
            b"MINOR: invalid port\nA:host# ",
            b"MINOR: ignored\nA:host# ",
        )
        _wired_ssh(mock_ssh_client_cls, session)

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-command
_ERROR:
another-bad-command
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("minor:", result.warnings[0])
        self.assertEqual(session.send.call_count, 2)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_capture_on_expect_match(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(b"A:host# show version\nok\nA:host#\n"),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: A:host#\s*$
_CAPTURE:out
show version
_END
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(list(result.captures.keys()), ["out"])
        self.assertIn("show version", result.captures["out"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_if_then_response_sent(self, mock_ssh_client_cls):
        session = _scripted_session(b"Password:\n", b"Password:\nA:host#\n")
        _wired_ssh(mock_ssh_client_cls, session)

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_IF: .*assword:
_THEN: secret-pass
_EXPECT: A:host#\s*$
login
"""

        result = ssht(template)

        self.assertTrue(result.success)
        sent = [call.args[0] for call in session.send.call_args_list]
        self.assertIn("login\n", sent)
        self.assertIn("secret-pass\n", sent)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_if_then_matched_logged_when_verbose(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(b"Password:\n", b"Password:\nA:host#\n"),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_IF: .*assword:
_THEN: secret-pass
_EXPECT: A:host#\s*$
login
"""

        with self.assertLogs("sshtemp.engine", level="INFO") as logs:
            result = ssht(template, verbose=True)

        self.assertTrue(result.success)
        matched_logs = [
            record
            for record in logs.records
            if record.message == "sshtemp.if_then_matched"
        ]
        self.assertEqual(len(matched_logs), 1)
        self.assertEqual(matched_logs[0].pattern, ".*assword:")
        self.assertEqual(matched_logs[0].command, "login")
        self.assertNotIn("secret-pass", matched_logs[0].getMessage())

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_retry_on_expect_timeout(self, mock_ssh_client_cls):
        session = _scripted_session(
            b"partial output\n",
            b"full output\nA:host#\n",
        )
        _wired_ssh(mock_ssh_client_cls, session)

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_RETRY: 2
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(session.send.call_count, 2)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_wait_resets_after_command(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(b"A:host#\n", b"A:host#\n"),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 99
_EXPECT: A:host#\s*$
show version
show port
"""

        with self.assertLogs("sshtemp.engine", level="INFO") as logs:
            result = ssht(template, verbose=True)

        self.assertTrue(result.success)
        sent_logs = [
            r
            for r in logs.records
            if r.message == "sshtemp.command_sent" and r.command == "show version"
        ]
        port_logs = [
            r
            for r in logs.records
            if r.message == "sshtemp.command_sent" and r.command == "show port"
        ]
        self.assertEqual(sent_logs[0].wait_seconds, 99)
        self.assertEqual(port_logs[0].wait_seconds, 20)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_wait_before_capture_applies_to_block(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(b"A:host#\n", b"A:host#\n", b"A:host#\n"),
        )

        template = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 88
_EXPECT: A:host#\s*$
_CAPTURE:block
cmd-one
cmd-two
_END
show port
"""

        with self.assertLogs("sshtemp.engine", level="INFO") as logs:
            result = ssht(template, verbose=True)

        self.assertTrue(result.success)
        block_logs = [
            r
            for r in logs.records
            if r.message == "sshtemp.command_sent"
            and r.command in ("cmd-one", "cmd-two")
        ]
        port_logs = [
            r
            for r in logs.records
            if r.message == "sshtemp.command_sent" and r.command == "show port"
        ]
        self.assertEqual(len(block_logs), 2)
        self.assertTrue(all(r.wait_seconds == 88 for r in block_logs))
        self.assertEqual(port_logs[0].wait_seconds, 20)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_connect_failure_returns_error(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.side_effect = TimeoutError("timed out")

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version
"""

        result = ssht(template)

        self.assertFalse(result.success)
        self.assertIn("failed to connect", result.error or "")
        self.assertIn("10.0.0.1", result.error or "")

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_host_key_policy_auto(self, mock_ssh_client_cls):
        import paramiko

        client = _wired_ssh(
            mock_ssh_client_cls, _scripted_session(b"ok\n")
        )

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: .*
show version
"""
        ssht(template)

        policy = client.set_missing_host_key_policy.call_args[0][0]
        self.assertIsInstance(policy, paramiko.AutoAddPolicy)
        client.load_system_host_keys.assert_called_once()

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_host_key_policy_reject(self, mock_ssh_client_cls):
        import paramiko

        client = _wired_ssh(
            mock_ssh_client_cls, _scripted_session(b"ok\n")
        )

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: .*
show version
"""
        ssht(template, host_key_policy="reject")

        policy = client.set_missing_host_key_policy.call_args[0][0]
        self.assertIsInstance(policy, paramiko.RejectPolicy)
        client.load_system_host_keys.assert_called_once()

    def test_invalid_host_key_policy(self):
        with self.assertLogs("sshtemp.engine", level="ERROR") as logs:
            result = ssht(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n",
                host_key_policy="strict",
            )
        self.assertFalse(result.success)
        self.assertIn("host_key_policy", result.error or "")
        self.assertTrue(
            any("invalid_host_key_policy" in r.message for r in logs.records)
        )


class SshtTemplateInputTests(unittest.TestCase):
    def test_single_line_str_loads_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "router.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            result = ssht(str(template), save_to_disk=False)
            self.assertNotIn("template file not found", result.error or "")

    def test_single_line_missing_file(self):
        result = ssht("missing-router-template.txt", save_to_disk=False)
        self.assertFalse(result.success)
        self.assertIn("template file not found", result.error or "")

    def test_unreadable_utf8_template_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "bad.txt"
            template.write_bytes(b"\xff\xfe_HOST: 10.0.0.1\n")
            result = ssht(template, save_to_disk=False)
        self.assertFalse(result.success)
        self.assertIn("failed to read template", result.error or "")

    def test_template_read_oserror_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "router.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n",
                encoding="utf-8",
            )
            with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
                result = ssht(template, save_to_disk=False)
        self.assertFalse(result.success)
        self.assertIn("failed to read template", result.error or "")
        self.assertIn("permission denied", result.error or "")

    def test_multiline_str_is_inline_template(self):
        result = ssht("_HOST: 10.0.0.1\n_USERNAME: admin\n")
        self.assertNotIn("template file not found", result.error or "")


class SshtSaveToDiskTests(unittest.TestCase):
    def test_missing_template_file(self):
        result = ssht(Path("missing-template-file.txt"), save_to_disk=True)
        self.assertFalse(result.success)
        self.assertIn("not found", result.error or "")
        self.assertIsNotNone(result.output_dir)
        self.assertTrue(result.output_dir.name.startswith("output_"))
        self.assertTrue((result.output_dir / "sshtemp.log").exists())

    def test_validation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "bad.txt"
            template.write_text("_USERNAME: admin\nshow version\n")
            result = ssht(template, save_to_disk=True)
            self.assertFalse(result.success)
            self.assertIn("template validation failed", result.error or "")
            self.assertIn("_HOST", result.error or "")

    def test_validation_failure_logs_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "bad.txt"
            template.write_text("_USERNAME: admin\nshow version\n")
            with self.assertLogs("sshtemp.engine", level="ERROR") as logs:
                ssht(template, save_to_disk=True)
            self.assertTrue(
                any("template_validation_error" in r.message for r in logs.records)
            )
            self.assertTrue(any("_HOST" in r.error for r in logs.records))

    @patch("sshtemp.engine._ssh_temp")
    def test_validation_warnings_are_logged_before_ssh(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter([])

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version
"""
        with self.assertLogs("sshtemp.engine", level="WARNING") as logs:
            result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("_EXPECT", result.warnings[0])
        warning_logs = [r for r in logs.records if r.message == "sshtemp.warning"]
        self.assertEqual(len(warning_logs), 1)
        self.assertIn("_EXPECT", warning_logs[0].error)

    @patch("sshtemp.engine._ssh_temp")
    def test_capture_subdir(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter(
            [SshTempCapture(name="version/10.1.1.1.txt", output="show version\n")]
        )

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n"
            )
            out_dir = Path(tmp) / "out"
            result = ssht(template, save_to_disk=True, output_dir=out_dir)

            self.assertTrue(result.success)
            self.assertTrue((out_dir / "version" / "10.1.1.1.txt").exists())

    @patch("sshtemp.engine._ssh_temp")
    def test_capture_path_traversal_rejected(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter(
            [SshTempCapture(name="../escape.txt", output="bad\n")]
        )

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n"
            )
            out_dir = Path(tmp) / "out"
            result = ssht(template, save_to_disk=True, output_dir=out_dir)

            self.assertFalse(result.success)
            self.assertIn("invalid capture path", result.error or "")
            self.assertFalse((out_dir.parent / "escape.txt").exists())

    @patch("sshtemp.engine._ssh_temp")
    def test_capture_append_same_name_in_memory(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter(
            [
                SshTempCapture(name="device.txt", output="show version\n"),
                SshTempCapture(name="device.txt", output="show port\n"),
            ]
        )

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version
"""
        result = ssht(template, save_to_disk=False)

        self.assertTrue(result.success)
        self.assertIn("show version", result.captures["device.txt"])
        self.assertIn("show port", result.captures["device.txt"])

    @patch("sshtemp.engine._ssh_temp")
    def test_capture_append_same_name(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter(
            [
                SshTempCapture(name="device.txt", output="show version\n"),
                SshTempCapture(name="device.txt", output="show port\n"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n"
            )
            out_dir = Path(tmp) / "out"
            result = ssht(template, save_to_disk=True, output_dir=out_dir)

            self.assertTrue(result.success)
            file_content = (out_dir / "device.txt").read_text(encoding="utf-8")
            self.assertIn("show version", file_content)
            self.assertIn("show port", file_content)
            self.assertIn("show version", result.captures["device.txt"])
            self.assertIn("show port", result.captures["device.txt"])

    @patch("sshtemp.engine._ssh_temp")
    def test_unnamed_captures_numbered_from_zero(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter(
            [
                SshTempCapture(name="named.txt", output="a\n"),
                SshTempCapture(name="", output="b\n"),
                SshTempCapture(name="", output="c\n"),
            ]
        )

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version
"""
        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(
            sorted(result.captures),
            ["capture_0.txt", "capture_1.txt", "named.txt"],
        )

    @patch("sshtemp.engine._ssh_temp")
    def test_capture_write_oserror_returns_error(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter(
            [SshTempCapture(name="out.txt", output="data\n")]
        )

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n"
            )
            out_dir = Path(tmp) / "out"
            with patch(
                "sshtemp.engine._append_capture_file",
                side_effect=OSError("disk full"),
            ):
                result = ssht(template, save_to_disk=True, output_dir=out_dir)

            self.assertFalse(result.success)
            self.assertIn("failed to write output", result.error or "")
            self.assertIn("disk full", result.error or "")
            self.assertEqual(result.output_dir, out_dir)

    @patch("sshtemp.engine._ssh_temp")
    def test_runtime_error_keeps_partial_captures(self, mock_ssh_temp):
        from sshtemp.exceptions import SshTempTimeoutError

        def _generator(*_args, **_kwargs):
            yield SshTempCapture(name="partial.txt", output="first block\n")
            raise SshTempTimeoutError("timed out", host="10.0.0.1")

        mock_ssh_temp.side_effect = _generator

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n"
            )
            out_dir = Path(tmp) / "out"
            result = ssht(template, save_to_disk=True, output_dir=out_dir)

            self.assertFalse(result.success)
            self.assertTrue((out_dir / "partial.txt").exists())
            self.assertIn("first block", result.captures["partial.txt"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_runtime_error_logged_to_output_dir_log(self, mock_ssh_client_cls):
        _wired_ssh(
            mock_ssh_client_cls,
            _scripted_session(b"MINOR: invalid port\nA:host# "),
        )

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_ERROR:(?i)minor:
bad-command
"""

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                out_dir = Path(tmp) / "run-output"
                result = ssht(template, save_to_disk=True, output_dir=out_dir)
            finally:
                os.chdir(old_cwd)

            self.assertFalse(result.success)
            output_log = (out_dir / "sshtemp.log").read_text(encoding="utf-8")
            self.assertIn("sshtemp.run_failed", output_log)
            self.assertIn("minor:", output_log)

            self.assertFalse((Path(tmp) / "sshtemp.log").exists())


class ParallelInfrastructureTests(unittest.TestCase):
    @patch("sshtemp.engine._ssh_temp")
    def test_manage_logging_false_preserves_handlers(self, mock_ssh_temp):
        mock_ssh_temp.return_value = iter([])

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: #\\s*$
"""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "sshtemp.log"
            configure_logging(verbose=False, log_file=log_path, append=False)
            root_logger = logging.getLogger("sshtemp")
            handler_count = len(root_logger.handlers)

            ssht(template, manage_logging=False)
            ssht(template, manage_logging=False)

            self.assertEqual(len(root_logger.handlers), handler_count)
            shutdown_logging()

    def test_append_capture_file_thread_safe(self):
        import threading

        from sshtemp.engine import _append_capture_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.txt"

            def worker(index: int) -> None:
                _append_capture_file(path, f"line-{index}\n")

            threads = [
                threading.Thread(target=worker, args=(index,))
                for index in range(10)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            content = path.read_text(encoding="utf-8")
            for index in range(10):
                self.assertIn(f"line-{index}", content)


if __name__ == "__main__":
    unittest.main()
