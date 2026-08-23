import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sshtemp import ssht
from sshtemp.models import SshTempCapture
from sshtemp.exceptions import SshTempErrorPattern
from sshtemp.engine import _poll_command_output
from sshtemp.logging_config import SshTempFormatter, configure_logging, shutdown_logging
from sshtemp.models import SshTempRunContext


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


class PollCommandOutputTests(unittest.TestCase):
    def test_error_fail_raises(self):
        session = MagicMock()
        session.recv_ready.return_value = True
        session.recv.return_value = b"MINOR: bad command\n"
        context = SshTempRunContext()

        with self.assertRaises(SshTempErrorPattern):
            _poll_command_output(
                session,
                host="10.0.0.1",
                poll_expect_interval=0,
                polls_left=1,
                current_if_rules=[],
                responded=[],
                expect_pattern="",
                error_pattern=r"(?i)minor:",
                error_fail="on",
                line_number=1,
                command="bad",
                context=context,
                capture_name=None,
                recv_buffer_size=65535,
                recv_encoding="ascii",
            )

    def test_error_without_fail_records_warning(self):
        session = MagicMock()
        session.recv_ready.return_value = True
        session.recv.return_value = b"MINOR: bad\nA:host#\n"
        context = SshTempRunContext()

        recv_text, had_error = _poll_command_output(
            session,
            host="10.0.0.1",
            poll_expect_interval=0,
            polls_left=3,
            current_if_rules=[],
            responded=[],
            expect_pattern=r"A:host#",
            error_pattern=r"(?i)minor:",
            error_fail="off",
            line_number=1,
            command="bad",
            context=context,
            recv_buffer_size=65535,
            recv_encoding="ascii",
        )

        self.assertTrue(had_error)
        self.assertEqual(len(context.warnings), 1)
        self.assertIn("MINOR", recv_text)

    def test_error_without_fail_logs_warning_immediately(self):
        session = MagicMock()
        session.recv_ready.return_value = True
        session.recv.return_value = b"MINOR: bad\nA:host#\n"
        context = SshTempRunContext(log_id="device-a")

        with self.assertLogs("sshtemp.engine", level="WARNING") as logs:
            _poll_command_output(
                session,
                host="10.0.0.1",
                poll_expect_interval=0,
                polls_left=3,
                current_if_rules=[],
                responded=[],
                expect_pattern=r"A:host#",
                error_pattern=r"(?i)minor:",
                error_fail="off",
                line_number=1,
                command="bad",
                context=context,
                recv_buffer_size=65535,
                recv_encoding="ascii",
            )

        warning_logs = [r for r in logs.records if r.message == "sshtemp.warning"]
        self.assertEqual(len(warning_logs), 1)
        self.assertEqual(warning_logs[0].log_id, "device-a")


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
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"A:host#\n"

        template = """
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
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_transport = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.get_transport.return_value = mock_transport
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"A:host#\n"

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_SHELL: 132x43
_RECV_BUFFER: 8192
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        mock_client.invoke_shell.assert_called_once_with(width=132, height=43)
        mock_session.recv.assert_called_with(8192)
        mock_transport.set_keepalive.assert_called_once_with(60)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_encoding_directive(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_transport = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.get_transport.return_value = mock_transport
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = "ü".encode("utf-8") + b"\n"

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_ENCODING: utf-8
_WAIT: 1
_CAPTURE:out
show version
_END
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertIn("ü", result.captures["out"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_default_encoding_is_utf8(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_transport = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.get_transport.return_value = mock_transport
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = "ü".encode("utf-8") + b"\n"

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_CAPTURE:out
show version
_END
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertIn("ü", result.captures["out"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_password_connect_disables_agent_and_keys(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"A:host#\n"

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: secret
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        connect_kwargs = mock_client.connect.call_args.kwargs
        self.assertFalse(connect_kwargs["allow_agent"])
        self.assertFalse(connect_kwargs["look_for_keys"])

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_timeout_includes_capture_name(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"still running...\n"

        template = """
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

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_errorfail_stops_template(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, True, False]
        mock_session.recv.side_effect = [
            b"A:host# ",
            b"MINOR: invalid port\n",
        ]

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
        mock_session.send.assert_called_once()

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_errorfail_off_override_warns_only(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False, True, False]
        mock_session.recv.side_effect = [
            b"MINOR: invalid port\nA:host# ",
            b"show version\nok\nA:host# ",
        ]

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-command
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("minor:", result.warnings[0])
        self.assertEqual(mock_session.send.call_count, 2)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_runtime_warnings_log_with_log_id_at_match_time(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
        ]
        mock_session.recv.side_effect = [
            b"MINOR: on device-a\nA:host# ",
            b"ok\nA:host# ",
            b"MINOR: on device-b\nA:host# ",
            b"ok2\nA:host# ",
        ]

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_LOGID: device-a
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-a
_EXPECT: A:host#\s*$
show ok

_LOGID: device-b
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-b
_EXPECT: A:host#\s*$
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
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False, True, False]
        mock_session.recv.side_effect = [
            b"MINOR: invalid port\nA:host# ",
            b"MINOR: ignored\nA:host# ",
        ]

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_ERROR:(?i)minor:
_ERRORFAIL: off
bad-command
_ERROR:
_EXPECT: A:host#\s*$
another-bad-command
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("minor:", result.warnings[0])
        self.assertEqual(mock_session.send.call_count, 2)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_capture_on_expect_match(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"A:host# show version\nok\nA:host#\n"

        template = """
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
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, True, False]
        mock_session.recv.side_effect = [
            b"Password:\n",
            b"Password:\nA:host#\n",
        ]

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_IF: .*assword
_THEN: secret-pass
_EXPECT: A:host#\s*$
login
"""

        result = ssht(template)

        self.assertTrue(result.success)
        sent = [call.args[0] for call in mock_session.send.call_args_list]
        self.assertIn("login\n", sent)
        self.assertIn("secret-pass\n", sent)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_if_then_matched_logged_when_verbose(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, True, False]
        mock_session.recv.side_effect = [
            b"Password:\n",
            b"Password:\nA:host#\n",
        ]

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_IF: .*assword
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
        self.assertEqual(matched_logs[0].pattern, ".*assword")
        self.assertEqual(matched_logs[0].command, "login")
        self.assertNotIn("secret-pass", matched_logs[0].getMessage())

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_retry_on_expect_timeout(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False, True, False]
        mock_session.recv.side_effect = [
            b"partial output\n",
            b"full output\nA:host#\n",
        ]

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
_RETRY: 2
_EXPECT: A:host#\s*$
show version
"""

        result = ssht(template)

        self.assertTrue(result.success)
        self.assertEqual(mock_session.send.call_count, 2)

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_wait_resets_after_command(self, mock_ssh_client_cls):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [
            True,
            False,
            True,
            False,
            True,
            False,
        ]
        mock_session.recv.return_value = b"A:host#\n"

        template = """
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
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [
            True,
            False,
            True,
            False,
            True,
            False,
        ]
        mock_session.recv.return_value = b"A:host#\n"

        template = """
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

        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session
        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"ok\n"

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: .*
show version
"""
        ssht(template)

        policy = mock_client.set_missing_host_key_policy.call_args[0][0]
        self.assertIsInstance(policy, paramiko.AutoAddPolicy)
        mock_client.load_system_host_keys.assert_called_once()

    @patch("sshtemp.engine.paramiko.SSHClient")
    def test_host_key_policy_reject(self, mock_ssh_client_cls):
        import paramiko

        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session
        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"ok\n"

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: .*
show version
"""
        ssht(template, host_key_policy="reject")

        policy = mock_client.set_missing_host_key_policy.call_args[0][0]
        self.assertIsInstance(policy, paramiko.RejectPolicy)
        mock_client.load_system_host_keys.assert_called_once()

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
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.connect.return_value = None
        mock_client.invoke_shell.return_value = mock_session

        mock_session.recv_ready.side_effect = [True, False]
        mock_session.recv.return_value = b"MINOR: invalid port\nA:host# "

        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_WAIT: 1
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

            cwd_log = Path(tmp) / "sshtemp.log"
            if cwd_log.exists():
                self.assertNotIn("sshtemp.run_failed", cwd_log.read_text(encoding="utf-8"))


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
