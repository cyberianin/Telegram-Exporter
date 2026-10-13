from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tgx.session_lock import SessionBusy, SessionGuard


class SessionGuardTests(unittest.TestCase):
    def test_excludes_second_process_guard_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.lock"
            with SessionGuard(path):
                with self.assertRaises(SessionBusy):
                    with SessionGuard(path):
                        pass
            with SessionGuard(path):
                self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
