from __future__ import annotations

import asyncio
import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_engine import FakeTransport

from tgx.engine import ExportEngine
from tgx.errors import ExportInvariantError
from tgx.storage import SQLiteStore


class Recovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_connection_retry_is_bounded_and_continues_cursor(self):
        transport = FakeTransport([1, 2, 3])
        transport.fail_once = True
        job = await ExportEngine(transport, self.store, "main").export(
            42, self.root / "retry.jsonl", page_size=1
        )
        self.assertEqual(self.store.get_job(job)["messages_saved"], 3)

    async def test_flood_wait_persists_retry_and_cursor(self):
        class FloodWaitError(Exception):
            seconds = 60

        transport = FakeTransport([1, 2])
        original = transport.history_page

        async def flood(peer_id, *, max_id, limit):
            if max_id < 3:
                raise FloodWaitError("wait 60 seconds")
            return await original(peer_id, max_id=max_id, limit=limit)

        transport.history_page = flood
        with self.assertRaises(FloodWaitError):
            await ExportEngine(transport, self.store, "main").export(
                42, self.root / "flood.jsonl", job_id="flood", page_size=1
            )
        job = self.store.get_job("flood")
        self.assertEqual(job["status"], "waiting_rate_limit")
        self.assertEqual(job["next_offset_id"], 2)
        self.assertTrue(job["retry_after"])

    async def test_full_disk_preserves_checkpoint(self):
        engine = ExportEngine(FakeTransport([1, 2]), self.store, "main")
        with patch.object(engine, "write_jsonl", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                await engine.export(42, self.root / "disk.jsonl", job_id="disk")
        self.assertEqual(self.store.get_job("disk")["status"], "waiting_storage")
        self.assertEqual(self.store.get_job("disk")["messages_saved"], 2)

    async def test_corrupted_checkpoint_is_rejected_without_scanning(self):
        engine = ExportEngine(FakeTransport([1, 2]), self.store, "main")
        job = await engine.export(42, self.root / "cursor.jsonl")
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE jobs SET next_offset_id=999 WHERE job_id=?", (job,)
            )
        with self.assertRaises(ExportInvariantError):
            await engine.resume(job)

    async def test_cancellation_waits_for_atomic_writer(self):
        engine = ExportEngine(FakeTransport([1, 2]), self.store, "main")
        original = engine.write_jsonl
        import threading
        import time

        started = threading.Event()

        def slow(*args):
            started.set()
            time.sleep(0.05)
            return original(*args)

        with patch.object(engine, "write_jsonl", side_effect=slow):
            task = asyncio.create_task(
                engine.export(42, self.root / "cancel.jsonl", job_id="cancel")
            )
            while not started.is_set():
                await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.store.get_job("cancel")["status"], "paused")
        await engine.resume("cancel")
        self.assertEqual(len((self.root / "cancel.jsonl").read_text().splitlines()), 2)

    async def test_legacy_schema_upgrade_preserves_messages(self):
        import sqlite3

        path = self.root / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE jobs(job_id TEXT PRIMARY KEY,account_id TEXT,peer_id INTEGER,title TEXT,
                initial_max_id INTEGER,next_offset_id INTEGER,pages_read INTEGER DEFAULT 0,
                messages_saved INTEGER DEFAULT 0,status TEXT,output_path TEXT,
                created_at TEXT,updated_at TEXT);
            CREATE TABLE messages(account_id TEXT,peer_id INTEGER,message_id INTEGER,date TEXT,
                body TEXT,PRIMARY KEY(account_id,peer_id,message_id));
            INSERT INTO jobs VALUES
                ('legacy','main',42,'channel',2,1,1,1,'paused','old.jsonl','now','now');
        """)
        body = json.dumps({"account_id": "main", "peer_id": 42, "message_id": 2})
        connection.execute("INSERT INTO messages VALUES ('main',42,2,NULL,?)", (body,))
        connection.commit()
        connection.close()
        store = SQLiteStore(path)
        try:
            self.assertEqual(len(list(store.stream_job("legacy"))), 1)
            self.assertEqual(
                store.connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
                "0001",
            )
        finally:
            store.close()
