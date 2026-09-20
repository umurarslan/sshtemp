import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sshtemp.logging_config import shutdown_logging
from sshtemp.models import SshTempResult
from sshtemp.parallel import ssht_parallel


class SshtParallelTests(unittest.TestCase):
    def tearDown(self) -> None:
        shutdown_logging()

    def test_rejects_invalid_max_workers(self):
        result = ssht_parallel("_HOST: 10.0.0.1\n_USERNAME: admin", max_workers=0)
        self.assertFalse(result.success)
        self.assertEqual(result.results, [])
        self.assertIn("max_workers", result.error or "")

    def test_template_not_found(self):
        result = ssht_parallel("missing-template.txt", max_workers=2)
        self.assertFalse(result.success)
        self.assertEqual(result.results, [])
        self.assertIn("not found", result.error or "")

    def test_unreadable_template_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "bad.txt"
            template.write_bytes(b"\xff\xfe")
            result = ssht_parallel(template, max_workers=1)
        self.assertFalse(result.success)
        self.assertEqual(result.results, [])
        self.assertIn("failed to read template", result.error or "")

    def test_prep_error_returns_without_workers(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin

_HOST: 10.0.0.2
_CAPTURE: out.txt
show version
"""
        with patch("sshtemp.parallel.ssht") as mock_ssht:
            result = ssht_parallel(template.strip(), max_workers=2)
        self.assertFalse(result.success)
        self.assertEqual(result.results, [])
        self.assertIn("unclosed _CAPTURE", result.error or "")
        mock_ssht.assert_not_called()

    @patch("sshtemp.parallel.ssht")
    def test_runs_all_sections_with_manage_logging_false(self, mock_ssht):
        mock_ssht.side_effect = [
            SshTempResult(success=True, host="10.0.0.1"),
            SshTempResult(success=True, host="10.0.0.2"),
        ]
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version

_HOST: 10.0.0.2
show version
"""
        result = ssht_parallel(template.strip(), max_workers=2)
        self.assertTrue(result.success)
        self.assertEqual(len(result.results), 2)
        self.assertEqual(mock_ssht.call_count, 2)
        for call in mock_ssht.call_args_list:
            self.assertFalse(call.kwargs["manage_logging"])

    @patch("sshtemp.parallel.ssht")
    def test_single_section_still_uses_parallel_path(self, mock_ssht):
        mock_ssht.return_value = SshTempResult(success=True, host="10.0.0.1")
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version
"""
        result = ssht_parallel(template.strip(), max_workers=1)
        self.assertTrue(result.success)
        self.assertEqual(len(result.results), 1)
        mock_ssht.assert_called_once()

    @patch("sshtemp.parallel.ssht")
    def test_runs_all_sections_when_one_fails(self, mock_ssht):
        mock_ssht.side_effect = [
            SshTempResult(success=True, host="10.0.0.1"),
            SshTempResult(success=False, host="10.0.0.2", error="timeout"),
            SshTempResult(success=True, host="10.0.0.3"),
        ]
        template = """
_HOST: 10.0.0.1
_USERNAME: admin

_HOST: 10.0.0.2

_HOST: 10.0.0.3
"""
        result = ssht_parallel(template.strip(), max_workers=3)
        self.assertFalse(result.success)
        self.assertEqual(len(result.results), 3)
        self.assertEqual(result.failed_section_index, 1)
        self.assertIn("section 2 failed", result.error or "")
        self.assertEqual(mock_ssht.call_count, 3)

    @patch("sshtemp.parallel.ssht")
    @patch("sshtemp.parallel.configure_logging")
    def test_configures_logging_once(self, mock_configure_logging, mock_ssht):
        mock_ssht.return_value = SshTempResult(success=True)
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
"""
        ssht_parallel(template.strip(), max_workers=1, save_to_disk=False)
        mock_configure_logging.assert_called_once()

    @patch("sshtemp.parallel.ssht")
    @patch("sshtemp.parallel.configure_logging")
    @patch("sshtemp.parallel._prepare_output_dir")
    def test_save_to_disk_passes_shared_output_dir(
        self,
        mock_prepare,
        mock_configure_logging,
        mock_ssht,
    ):
        shared_dir = Path("/tmp/shared-output")
        mock_prepare.return_value = shared_dir
        mock_ssht.return_value = SshTempResult(success=True, output_dir=shared_dir)
        template = """
_HOST: 10.0.0.1
_USERNAME: admin

_HOST: 10.0.0.2
"""
        result = ssht_parallel(
            template.strip(),
            max_workers=2,
            save_to_disk=True,
            output_dir=shared_dir,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output_dir, shared_dir)
        for call in mock_ssht.call_args_list:
            self.assertTrue(call.kwargs["save_to_disk"])
            self.assertEqual(call.kwargs["output_dir"], shared_dir)

    @patch("sshtemp.parallel.ssht")
    def test_preserves_section_order(self, mock_ssht):
        def fake_ssht(section: str, **_kwargs) -> SshTempResult:
            host = section.splitlines()[0].removeprefix("_HOST:").strip()
            return SshTempResult(success=True, host=host)

        mock_ssht.side_effect = fake_ssht
        template = """
_HOST: 10.0.0.1
_USERNAME: admin

_HOST: 10.0.0.2

_HOST: 10.0.0.3
"""
        result = ssht_parallel(template.strip(), max_workers=2)
        self.assertEqual(
            [item.host for item in result.results],
            ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
        )

    @patch("sshtemp.parallel.ssht")
    @patch("sshtemp.parallel.configure_logging")
    @patch("sshtemp.parallel._prepare_output_dir")
    def test_prepare_output_oserror_returns_error(
        self,
        mock_prepare,
        mock_configure_logging,
        mock_ssht,
    ):
        mock_prepare.side_effect = OSError("disk full")
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
"""
        result = ssht_parallel(
            template.strip(),
            max_workers=1,
            save_to_disk=True,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.results, [])
        self.assertIn("failed to write output", result.error or "")
        self.assertIn("disk full", result.error or "")
        mock_ssht.assert_not_called()
        mock_configure_logging.assert_not_called()


if __name__ == "__main__":
    unittest.main()
