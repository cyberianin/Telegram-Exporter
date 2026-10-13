from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_engine import FakeTransport

from tgx.options import ExportOptions
from tgx.service import ExportService, SessionManager


class Management(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_keep_separate_history(self):
        from dataclasses import replace

        class ProfileTransport(FakeTransport):
            def __init__(self, profile):
                super().__init__([1] if profile == "main" else [2, 3])
                self.profile = profile

            async def history_page(self, peer_id, *, max_id, limit):
                page = await super().history_page(peer_id, max_id=max_id, limit=limit)
                return replace(
                    page,
                    messages=[replace(item, account_id=self.profile) for item in page.messages],
                )

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                service = ExportService(
                    Path(directory),
                    SessionManager(ProfileTransport),
                )
                await service.start()
                try:
                    first = service.create("main", "42", ExportOptions())
                    second = service.create("second", "42", ExportOptions())
                    await self.wait_done(service, first)
                    await self.wait_done(service, second)
                    self.assertEqual(service.status(first)["account_id"], "main")
                    self.assertEqual(service.status(second)["account_id"], "second")
                    self.assertEqual(service.status(first)["messages_saved"], 1)
                    self.assertEqual(service.status(second)["messages_saved"], 2)
                finally:
                    await service.close()

    async def test_flood_wait_resumes_automatically_from_checkpoint(self):
        from telethon.errors import FloodWaitError

        class LimitedTransport(FakeTransport):
            async def history_page(self, peer_id, *, max_id, limit):
                if max_id < 151 and not self.failed:
                    self.failed = True
                    raise FloodWaitError(request=None, capture=1)
                return await super().history_page(peer_id, max_id=max_id, limit=limit)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                transport = LimitedTransport(list(range(1, 151)))
                service = ExportService(Path(directory), SessionManager(lambda _: transport))
                await service.start()
                try:
                    job = service.create("main", "42", ExportOptions())
                    for _ in range(100):
                        if service.status(job)["status"] == "waiting_rate_limit":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(service.status(job)["status"], "waiting_rate_limit")
                    self.assertEqual(service.status(job)["messages_saved"], 100)
                    other = service.create("main", "42", ExportOptions())
                    await self.wait_done(service, other)
                    await self.wait_done(service, job)
                    result = service.status(job)
                    self.assertEqual(result["messages_saved"], 150)
                    self.assertIsNone(result["retry_after"])
                    self.assertIsNone(result["last_error"])
                finally:
                    await service.close()

    async def wait_done(self, service, job):
        for _ in range(100):
            if service.status(job)["status"] == "completed":
                return
            await asyncio.sleep(0.03)
        self.fail("Worker did not complete")

    async def test_named_files_and_delete_preserve_other_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                root = Path(directory)
                service = ExportService(root, SessionManager(lambda _: FakeTransport([1, 2])))
                await service.start()
                try:
                    first = service.create("main", "42", ExportOptions(format="txt"))
                    await self.wait_done(service, first)
                    second = service.create("main", "42", ExportOptions())
                    await self.wait_done(service, second)
                    self.assertTrue(
                        Path(service.status(first)["output_path"]).name.startswith("private_")
                    )
                    self.assertEqual(service.delete(first)["status"], "deleted")
                    with self.assertRaises(KeyError):
                        service.status(first)
                    self.assertFalse((root / "exports" / first).exists())
                    self.assertEqual(service.status(second)["messages_saved"], 2)
                    self.assertEqual(
                        service.store.connection.execute(
                            "SELECT count(*) FROM messages"
                        ).fetchone()[0],
                        2,
                    )
                finally:
                    await service.close()

    async def test_delete_queued_request_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                service = ExportService(Path(directory))
                try:
                    job = service.create("main", "42", ExportOptions())
                    self.assertEqual(service.delete(job)["status"], "deleted")
                    self.assertEqual(service.list_jobs(), [])
                finally:
                    await service.close()

    async def test_logout_revokes_session_and_deletes_saved_credentials(self):
        called = []

        class Client:
            async def log_out(self):
                called.append("logout")

        class Transport(FakeTransport):
            client = Client()

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                root = Path(directory)
                service = ExportService(root, SessionManager(lambda _: Transport([])))
                (root / "main.session").write_bytes(b"test-only")
                (root / "main.session.enc").write_bytes(b"test-only")
                service.configure_credentials(123, "a" * 32)
                try:
                    result = await service.logout("main")
                    self.assertEqual(result["status"], "not_authorized")
                    self.assertEqual(called, ["logout"])
                    for name in ("main.session", "main.session.enc", "credentials.json"):
                        self.assertFalse((root / name).exists())
                    self.assertTrue((root / "tgx.sqlite3").exists())
                finally:
                    await service.close()
