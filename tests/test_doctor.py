from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tgx.doctor import doctor_report


class DoctorTests(unittest.TestCase):
    def test_reports_writable_directory_without_exposing_api_credentials(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"TGX_API_ID": "12345", "TGX_API_HASH": "private-value"},
                clear=False,
            ),
            patch("tgx.doctor.importlib.metadata.version", return_value="1.0"),
        ):
            result = doctor_report(Path(directory) / "data")
            data_check = next(
                check for check in result["checks"] if check["name"] == "data_directory"
            )
            self.assertEqual(data_check["status"], "ok")

        rendered = repr(result)
        self.assertTrue(result["ready"])
        self.assertIn("telegram_api_credentials", rendered)
        self.assertNotIn("private-value", rendered)

    def test_reports_invalid_python_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("tgx.doctor.sys.version_info", (3, 11, 0)):
                result = doctor_report(Path(directory))

        python_check = next(check for check in result["checks"] if check["name"] == "python")
        self.assertEqual(python_check["status"], "error")
        self.assertFalse(result["ready"])
