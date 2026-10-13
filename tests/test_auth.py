from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_engine import FakeTransport

from tgx.service import ExportService, SessionManager


class Auth(unittest.IsolatedAsyncioTestCase):
    async def test_code_precedes_password_and_authorized_status_is_explicit(self):
        from telethon.errors import SessionPasswordNeededError

        calls = []
        authorized = False

        class Client:
            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="test-only-hash")

            async def sign_in(self, **arguments):
                nonlocal authorized
                if "code" in arguments:
                    calls.append("code")
                    raise SessionPasswordNeededError(None)
                calls.append("password")
                authorized = True

            async def get_me(self):
                return SimpleNamespace(id=123, first_name="Test", username="test")

        class Transport(FakeTransport):
            client = Client()

            async def is_authorized(self):
                return authorized

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                service = ExportService(Path(directory), SessionManager(lambda _: Transport([])))
                try:
                    first = await service.login("main", "+10000000000")
                    self.assertEqual(first["status"], "waiting_code")
                    self.assertEqual((await service.auth_status("main"))["status"], "waiting_code")
                    result = await service.confirm_login("main", "test-code", "test-password")
                    self.assertEqual(calls, ["code", "password"])
                    self.assertEqual(result["status"], "authorized")
                    self.assertEqual(result["user"]["id"], 123)
                    self.assertEqual((await service.auth_status("main"))["status"], "authorized")
                    self.assertFalse(service.pending_auth)
                finally:
                    await service.close()

    async def test_password_is_requested_only_after_accepted_code(self):
        from telethon.errors import SessionPasswordNeededError

        class Client:
            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="test-only")

            async def sign_in(self, **arguments):
                raise SessionPasswordNeededError(None)

        class Transport(FakeTransport):
            client = Client()

            async def is_authorized(self):
                return False

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                service = ExportService(Path(directory), SessionManager(lambda _: Transport([])))
                try:
                    await service.login("main", "+10000000000")
                    result = await service.confirm_login("main", "test-code", None)
                    self.assertEqual(result["status"], "waiting_password")
                    self.assertEqual(
                        (await service.auth_status("main"))["status"], "waiting_password"
                    )
                finally:
                    await service.close()
