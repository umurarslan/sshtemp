import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sshtemp.cli import main
from sshtemp.models import SshTempParallelResult, SshTempResult


class CliTests(unittest.TestCase):
    def test_no_args_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_template_exits_1(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            main(["missing-template.txt"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not found", stderr.getvalue())

    def test_check_valid_template_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as ctx:
                main(["--check", str(template)])
            self.assertEqual(ctx.exception.code, 0)

    def test_check_invalid_template_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "bad.txt"
            template.write_text("_USERNAME: admin\nshow version\n", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main(["--check", str(template)])
            self.assertEqual(ctx.exception.code, 1)
            output = stderr.getvalue()
            self.assertIn("template validation failed", output)
            self.assertIn("_HOST", output)

    @patch("sshtemp.cli.ssht")
    def test_run_success_prints_output_dir(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht.return_value = SshTempResult(success=True, output_dir=out_dir)

            stdout = io.StringIO()
            with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                main([str(template)])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn(f"output: {out_dir}", stdout.getvalue())

    @patch("sshtemp.cli.ssht")
    def test_run_success_with_warnings_prints_stderr(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht.return_value = SshTempResult(
                success=True,
                output_dir=out_dir,
                warnings=["error pattern matched at line 5"],
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as ctx,
            ):
                main([str(template)])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("1 warning(s)", stderr.getvalue())
            self.assertIn("error pattern matched", stderr.getvalue())

    @patch("sshtemp.cli.ssht")
    def test_run_passes_rendered_template_to_ssht(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht.return_value = SshTempResult(success=True, output_dir=out_dir)

            with self.assertRaises(SystemExit):
                main([str(template)])

            rendered = mock_ssht.call_args[0][0]
            self.assertIn("_HOST: 10.0.0.1", rendered)
            mock_ssht.assert_called_once_with(
                rendered,
                verbose=False,
                save_to_disk=True,
                output_dir=None,
                host_key_policy="auto",
            )

    @patch("sshtemp.cli.ssht")
    def test_run_failure_prints_error(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            mock_ssht.return_value = SshTempResult(
                success=False,
                error="failed to connect",
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main([str(template)])
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("failed to connect", stderr.getvalue())

    @patch("sshtemp.cli.ssht")
    def test_run_failure_prints_partial_captures_and_output_dir(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht.return_value = SshTempResult(
                success=False,
                output_dir=out_dir,
                captures={"show_version.txt": "output"},
                failed_line=28,
                failed_command="port x1/4/8",
                failed_capture_name="config.txt",
                error='error pattern "(?i)minor:" matched at line 28',
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main([str(template)])
            self.assertEqual(ctx.exception.code, 1)
            output = stderr.getvalue()
            self.assertIn("partial captures (1): show_version.txt", output)
            self.assertIn(f"output: {out_dir}", output)
            self.assertIn("failed at line 28", output)
            self.assertIn("command 'port x1/4/8'", output)
            self.assertIn("capture 'config.txt'", output)
            self.assertIn("error pattern", output)

    @patch("sshtemp.cli.ssht")
    def test_output_dir_passed_to_ssht(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "custom-out"
            mock_ssht.return_value = SshTempResult(success=True, output_dir=out_dir)

            with self.assertRaises(SystemExit):
                main(["-o", str(out_dir), str(template)])

            rendered = mock_ssht.call_args[0][0]
            mock_ssht.assert_called_once_with(
                rendered,
                verbose=False,
                save_to_disk=True,
                output_dir=str(out_dir),
                host_key_policy="auto",
            )

    @patch("sshtemp.cli.ssht")
    def test_verbose_flag_passed_to_ssht(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht.return_value = SshTempResult(success=True, output_dir=out_dir)

            with self.assertRaises(SystemExit):
                main(["-v", str(template)])

            rendered = mock_ssht.call_args[0][0]
            mock_ssht.assert_called_once_with(
                rendered,
                verbose=True,
                save_to_disk=True,
                output_dir=None,
                host_key_policy="auto",
            )

    @patch("sshtemp.cli.ssht")
    def test_host_key_policy_passed_to_ssht(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht.return_value = SshTempResult(success=True, output_dir=out_dir)

            with self.assertRaises(SystemExit):
                main(["--host-key-policy", "reject", str(template)])

            rendered = mock_ssht.call_args[0][0]
            mock_ssht.assert_called_once_with(
                rendered,
                verbose=False,
                save_to_disk=True,
                output_dir=None,
                host_key_policy="reject",
            )

    @patch("sshtemp.cli.ssht")
    def test_run_failure_prints_warnings_before_error(self, mock_ssht):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\nshow version\n",
                encoding="utf-8",
            )
            mock_ssht.return_value = SshTempResult(
                success=False,
                warnings=["error pattern matched at line 3"],
                error="failed to connect",
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main([str(template)])
            self.assertEqual(ctx.exception.code, 1)
            output = stderr.getvalue()
            warning_pos = output.find("error pattern matched")
            error_pos = output.find("failed to connect")
            self.assertNotEqual(warning_pos, -1)
            self.assertNotEqual(error_pos, -1)
            self.assertLess(warning_pos, error_pos)

    def test_check_allows_env_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "with-env.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n_PASSWORD: ${MISSING}\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as ctx:
                main(["--check", str(template)])
            self.assertEqual(ctx.exception.code, 0)

    @patch("sshtemp.cli.substitute_env_vars", return_value=("", "missing environment variable: MISSING"))
    def test_run_missing_env_exits_1(self, _mock_subst):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "bad-env.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n_PASSWORD: ${MISSING}\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main([str(template)])
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("missing environment variable: MISSING", stderr.getvalue())

    def test_check_invalid_placeholder_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "bad-placeholder.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n_PASSWORD: ${my-var}\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main(["--check", str(template)])
            self.assertEqual(ctx.exception.code, 1)
            output = stderr.getvalue()
            self.assertIn("invalid placeholder", output)
            self.assertIn("my-var", output)

    def test_check_keeps_env_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok-env.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n_PASSWORD: ${TEST_PASS}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"TEST_PASS": "secret"}, clear=False):
                with self.assertRaises(SystemExit) as ctx:
                    main(["--check", str(template)])
            self.assertEqual(ctx.exception.code, 0)


    @patch("sshtemp.cli.ssht_parallel")
    def test_parallel_flag_calls_ssht_parallel(self, mock_ssht_parallel):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n\n"
                "_HOST: 10.0.0.2\n_USERNAME: admin\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht_parallel.return_value = SshTempParallelResult(
                success=True,
                output_dir=out_dir,
                results=[
                    SshTempResult(success=True, host="10.0.0.1"),
                    SshTempResult(success=True, host="10.0.0.2"),
                ],
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                main(["--parallel", "2", str(template)])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn(f"output: {out_dir}", stdout.getvalue())
            rendered = mock_ssht_parallel.call_args[0][0]
            self.assertIn("_HOST: 10.0.0.1", rendered)
            mock_ssht_parallel.assert_called_once_with(
                rendered,
                2,
                verbose=False,
                save_to_disk=True,
                output_dir=None,
                host_key_policy="auto",
            )

    @patch("sshtemp.cli.ssht_parallel")
    def test_short_parallel_flag_calls_ssht_parallel(self, mock_ssht_parallel):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n",
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            mock_ssht_parallel.return_value = SshTempParallelResult(
                success=True,
                output_dir=out_dir,
                results=[SshTempResult(success=True)],
            )

            with self.assertRaises(SystemExit):
                main(["-p", "4", str(template)])

            mock_ssht_parallel.assert_called_once()
            self.assertEqual(mock_ssht_parallel.call_args[0][1], 4)

    def test_parallel_invalid_workers_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "ok.txt"
            template.write_text(
                "_HOST: 10.0.0.1\n_USERNAME: admin\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main(["-p", "0", str(template)])
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("max_workers", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
