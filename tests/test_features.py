from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_engine import FakeTransport, message

from tgx.archive import verify_jsonl
from tgx.engine import ExportEngine
from tgx.formats import convert_archive, records
from tgx.importer import import_archive
from tgx.options import ExportOptions
from tgx.service import ExportService, SessionManager
from tgx.storage import SQLiteStore


class Features(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_repeated_export_counts_existing_messages_per_job(self):
        engine = ExportEngine(FakeTransport([1, 3, 5]), self.store, "main")
        await engine.export(42, self.root / "a.jsonl")
        job = await engine.export(42, self.root / "b.jsonl")
        self.assertEqual(self.store.get_job(job)["messages_saved"], 3)
        self.assertEqual(verify_jsonl(self.root / "b.jsonl")["messages"], 3)

    async def test_range_excludes_previous_exports_and_future_messages(self):
        engine = ExportEngine(FakeTransport([1, 2, 3, 4, 5]), self.store, "main")
        await engine.export(42, self.root / "all.jsonl")
        await engine.export(
            42, self.root / "range.jsonl", options=ExportOptions(mode="range", min_id=2, max_id=4)
        )
        self.assertEqual([r["message_id"] for r in records(self.root / "range.jsonl")], [3, 4])

    async def test_incremental_only_exports_new_messages(self):
        transport = FakeTransport([1, 2])
        engine = ExportEngine(transport, self.store, "main")
        await engine.export(42, self.root / "first.jsonl")
        transport.ids = [4, 3, 2, 1]
        await engine.export(42, self.root / "next.jsonl", options=ExportOptions(mode="incremental"))
        self.assertEqual([r["message_id"] for r in records(self.root / "next.jsonl")], [3, 4])

    async def test_reconcile_retains_old_version(self):
        transport = FakeTransport([1])
        engine = ExportEngine(transport, self.store, "main")
        await engine.export(42, self.root / "first.jsonl")

        async def edited(peer_id, *, max_id, limit):
            from tgx.models import HistoryPage

            return HistoryPage([replace(message(1), text="edited")], False)

        transport.history_page = edited
        await engine.export(42, self.root / "edited.jsonl", options=ExportOptions(mode="reconcile"))
        self.assertEqual(next(records(self.root / "edited.jsonl"))["text"], "edited")
        self.assertEqual(
            self.store.connection.execute("SELECT count(*) FROM message_versions").fetchone()[0], 1
        )

    async def test_conversion_all_formats_and_integrity(self):
        source = self.root / "archive.jsonl"
        await ExportEngine(FakeTransport([1, 2, 3]), self.store, "main").export(42, source)
        for format in ("json", "txt", "csv", "sqlite", "html", "jsonl.zst"):
            output = self.root / ("converted." + format)
            manifest = convert_archive(source, output, format, page_size=2)
            self.assertEqual(manifest["message_count"], 3)
            self.assertTrue(verify_jsonl(output)["valid"])
        self.assertEqual(len(json.loads((self.root / "converted.json").read_text())["messages"]), 3)
        connection = sqlite3.connect(self.root / "converted.sqlite")
        self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
        connection.close()

    async def test_media_queue_does_not_hold_a_wal_read_snapshot(self):
        from test_engine import message

        from tgx.engine import utcnow

        self.store.create_job(
            job_id="locking",
            account_id="main",
            peer_id=42,
            title="test",
            initial_max_id=3,
            output_path=self.root / "lock.jsonl",
            now=utcnow(),
        )
        self.store.save_page(
            "locking", [replace(message(i), media={"photo": {}}) for i in (1, 2)], 1, utcnow()
        )
        pending = self.store.media_rows("locking")
        row = next(pending)
        second = SQLiteStore(
            Path(self.store.connection.execute("PRAGMA database_list").fetchone()[2])
        )
        try:
            # Use a second connection to the same DB, as API controls do.
            second.update_job("locking", last_error="external update")
            self.store.update_media(
                row["account_id"], row["peer_id"], row["message_id"], status="downloaded"
            )
            self.assertEqual(len(list(pending)), 1)
        finally:
            second.close()

    async def test_media_flood_wait_is_resumable_without_text_loss(self):
        from telethon.errors import FloodWaitError

        from tgx.models import HistoryPage

        class LimitedMedia(FakeTransport):
            async def history_page(self, peer_id, *, max_id, limit):
                page = await super().history_page(peer_id, max_id=max_id, limit=limit)
                return HistoryPage(
                    [replace(m, media={"photo": {}}) for m in page.messages], page.has_more
                )

            async def download_media(self, peer, identity, path):
                if not self.failed:
                    self.failed = True
                    raise FloodWaitError(request=None, capture=1)
                path.write_bytes(b"photo")
                return 5

        engine = ExportEngine(LimitedMedia([1, 2]), self.store, "main")
        output = self.root / "limited.jsonl"
        with self.assertRaises(ExceptionGroup):
            await engine.export(
                42, output, options=ExportOptions(media="full", media_concurrency=1)
            )
        job = self.store.list_jobs()[0]
        self.assertEqual(job["status"], "waiting_rate_limit")
        self.assertEqual(job["messages_saved"], 2)
        self.assertIn("FloodWaitError", job["last_error"])
        await engine.resume(job["job_id"])
        self.assertEqual(self.store.get_job(job["job_id"])["status"], "completed")
        self.assertEqual(verify_jsonl(output)["messages"], 2)

    async def test_media_failure_does_not_lose_text_and_retry_is_independent(self):
        from tgx.media import MediaWorker
        from tgx.models import HistoryPage

        class MediaTransport(FakeTransport):
            async def history_page(self, peer_id, *, max_id, limit):
                page = await super().history_page(peer_id, max_id=max_id, limit=limit)
                return HistoryPage(
                    [replace(m, media={"photo": {}}) for m in page.messages], page.has_more
                )

            async def download_media(self, peer, identity, path):
                if identity == 2:
                    raise PermissionError("server denied media")
                path.write_bytes(b"photo")
                return 5

        transport = MediaTransport([1, 2])
        output = self.root / "media.jsonl"
        job = await ExportEngine(transport, self.store, "main").export(
            42, output, options=ExportOptions(media="full")
        )
        rows = list(records(output))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["media_download"]["status"], "downloaded")
        self.assertEqual(rows[1]["media_download"]["status"], "failed")
        self.assertTrue(verify_jsonl(output)["valid"])
        result = await MediaWorker(transport, self.store).run(
            job, self.root / "media-media", ExportOptions(media="full"), retry=True
        )
        self.assertEqual(result["failed"], 1)
        (self.root / rows[0]["media_download"]["path"]).write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify_jsonl(output)

    async def test_import_has_provenance_and_merges_identity(self):
        source = self.root / "import.jsonl"
        source.write_text(json.dumps(message(1).to_dict()) + "\n", encoding="utf-8")
        job = import_archive(self.store, source, "main", 42)
        self.assertEqual(self.store.get_job(job)["messages_saved"], 1)
        self.assertEqual(
            json.loads(next(self.store.stream_job(job)))["source"], "import:import.jsonl"
        )

    async def test_auth_failure_is_structured_diagnostic(self):
        transport = FakeTransport([1])

        async def broken(value):
            raise PermissionError("CHANNEL_PRIVATE")

        transport.resolve_peer = broken
        result = await ExportEngine(transport, self.store, "main").inspect("42")
        self.assertFalse(result["peer_resolved"])
        self.assertEqual(result["server_error"]["type"], "PermissionError")

    async def test_pause_does_not_finalize_partial_archive(self):
        def pause(progress):
            self.store.update_job(progress["job_id"], control="pause")

        output = self.root / "pause.jsonl"
        job = await ExportEngine(FakeTransport([1, 2, 3]), self.store, "main", pause).export(
            42, output, page_size=1
        )
        self.assertEqual(self.store.get_job(job)["status"], "paused")
        self.assertFalse(output.exists())

    async def test_service_persists_queue_and_runs_independent_worker(self):
        with patch.dict("os.environ", {"TGX_DATA_DIR": str(self.root)}):
            service = ExportService(
                self.root, SessionManager(lambda account: FakeTransport([1, 2]))
            )
            job = service.create("main", "42", ExportOptions())
            await service.start()
            try:
                for _ in range(100):
                    if service.status(job)["status"] == "completed":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(service.status(job)["status"], "completed")
                self.assertTrue(service.artifacts(job))
            finally:
                await service.close()


class API(unittest.TestCase):
    def test_owner_auth_and_artifact_traversal(self):
        from fastapi.testclient import TestClient

        from tgx.api import create_app

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict("os.environ", {"TGX_DATA_DIR": directory}):
                service = ExportService(root, SessionManager(lambda account: FakeTransport([1])))
                with TestClient(create_app(service, "a" * 32)) as client:
                    self.assertEqual(client.get("/api/v1/accounts").status_code, 401)
                    headers = {"Authorization": "Bearer " + "a" * 32}
                    response = client.post("/api/v1/exports", headers=headers, json={"peer": "42"})
                    self.assertEqual(response.status_code, 202)
                    job = response.json()["job_id"]
                    self.assertEqual(
                        client.get(f"/api/v1/exports/{job}", headers=headers).status_code, 200
                    )
                    self.assertEqual(
                        client.get(
                            f"/api/v1/exports/{job}/files/%2e%2e%2fsecret", headers=headers
                        ).status_code,
                        404,
                    )
                    self.assertIn("История", client.get("/").text)
