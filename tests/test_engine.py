from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tgx.engine import ExportEngine
from tgx.errors import EmptyHistory, ExportInvariantError
from tgx.models import DialogRecord, HistoryPage, MessageRecord
from tgx.options import ExportOptions
from tgx.storage import SQLiteStore


def message(message_id: int) -> MessageRecord:
    return MessageRecord(
        "main",
        42,
        message_id,
        None,
        f"message {message_id}",
        None,
        None,
        None,
        None,
        None,
        [],
        None,
        "Message",
    )


class FakeTransport:
    def __init__(self, ids: list[int]) -> None:
        self.ids = sorted(ids, reverse=True)
        self.fail_once = False
        self.failed = False
        self.short_once = False
        self.empty_once = False

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_authorized(self):
        return True

    async def dialogs(self):
        return [DialogRecord(42, "private", None, "channel", True)]

    async def resolve_peer(self, value):
        return (await self.dialogs())[0]

    async def latest_message_id(self, peer_id):
        return max(self.ids, default=0)

    async def history_page(self, peer_id, *, max_id, limit):
        if self.empty_once:
            self.empty_once = False
            return HistoryPage([], False)
        if self.fail_once and not self.failed and max_id < max(self.ids) + 1:
            self.failed = True
            raise ConnectionError("simulated disconnect")
        selected = [item for item in self.ids if item < max_id][:limit]
        if self.short_once and limit > 1 and selected:
            self.short_once = False
            selected = selected[:1]
        return HistoryPage([message(item) for item in selected], len(selected) == limit)


class GeneratedTransport(FakeTransport):
    def __init__(self, upper: int) -> None:
        self.upper = upper

    async def latest_message_id(self, peer_id):
        return self.upper

    async def history_page(self, peer_id, *, max_id, limit):
        selected = list(range(min(max_id - 1, self.upper), max(0, max_id - limit - 1), -1))
        return HistoryPage([message(item) for item in selected], len(selected) == limit)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_streams_history_across_pages_with_gaps(self) -> None:
        output = self.root / "history.jsonl"
        engine = ExportEngine(FakeTransport([1, 2, 7, 9, 15]), self.store, "main")
        job_id = await engine.export("42", output, page_size=2)
        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["message_id"] for line in lines], [1, 2, 7, 9, 15])
        self.assertEqual(self.store.get_job(job_id)["status"], "completed")
        manifest = json.loads(Path(f"{output}.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["message_count"], 5)
        self.assertEqual(manifest["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(manifest["scan_status"], "server_returned_range_enumerated")

    async def test_resume_after_transport_error_does_not_duplicate(self) -> None:
        output = self.root / "history.jsonl"
        transport = FakeTransport([1, 2, 3, 4, 5])
        transport.fail_once = True
        engine = ExportEngine(transport, self.store, "main")
        with self.assertRaises(ConnectionError):
            await engine.export(
                "42",
                output,
                page_size=2,
                job_id="resume-me",
                options=ExportOptions(page_size=2, connection_retries=0),
            )
        self.assertEqual(self.store.get_job("resume-me")["status"], "paused")
        await engine.resume("resume-me", page_size=2)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 5)
        self.assertEqual(self.store.get_job("resume-me")["messages_saved"], 5)

    async def test_short_page_is_checked_before_declaring_end(self) -> None:
        output = self.root / "short-page.jsonl"
        transport = FakeTransport([1, 2, 3, 4, 5])
        transport.short_once = True
        engine = ExportEngine(transport, self.store, "main")
        await engine.export("42", output, page_size=3)
        self.assertEqual(
            [
                json.loads(line)["message_id"]
                for line in output.read_text(encoding="utf-8").splitlines()
            ],
            [1, 2, 3, 4, 5],
        )

    async def test_transient_empty_page_is_reprobed(self) -> None:
        output = self.root / "transient-empty.jsonl"
        transport = FakeTransport([1, 2, 3])
        transport.empty_once = True
        engine = ExportEngine(transport, self.store, "main")
        await engine.export("42", output, page_size=3)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 3)

    async def test_large_export_uses_paged_records(self) -> None:
        output = self.root / "large.jsonl"
        engine = ExportEngine(GeneratedTransport(10_000), self.store, "main")
        job_id = await engine.export("42", output, page_size=100)
        self.assertEqual(self.store.get_job(job_id)["messages_saved"], 10_000)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 10_000)

    async def test_failed_output_write_keeps_previous_archive_and_can_resume(self) -> None:
        output = self.root / "atomic.jsonl"
        output.write_text("old archive\n", encoding="utf-8")
        engine = ExportEngine(FakeTransport([1, 2, 3]), self.store, "main")
        original = self.store.stream_job

        def broken_stream(job_id, batch_size=500):
            yield from original(job_id, batch_size=batch_size)
            raise OSError("simulated full disk")

        with patch.object(self.store, "stream_job", side_effect=broken_stream):
            with self.assertRaises(OSError):
                await engine.export("42", output, page_size=2, job_id="disk-recovery")
        self.assertEqual(output.read_text(encoding="utf-8"), "old archive\n")
        self.assertEqual(self.store.get_job("disk-recovery")["status"], "paused")
        await engine.resume("disk-recovery", page_size=2)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 3)

    async def test_empty_history_is_not_reported_as_success(self) -> None:
        engine = ExportEngine(FakeTransport([]), self.store, "main")
        with self.assertRaises(EmptyHistory):
            await engine.export("42", self.root / "empty.jsonl")
        self.assertFalse((self.root / "empty.jsonl").exists())

    async def test_inspection_reports_authorization_and_probe_fields(self) -> None:
        result = await ExportEngine(FakeTransport([2, 5]), self.store, "main").inspect("42")
        self.assertTrue(result["peer_resolved"])
        self.assertTrue(result["account_authorized"])
        self.assertTrue(result["history_readable"])
        self.assertEqual(result["last_message_id"], 5)
        self.assertEqual(result["history_probe_count"], 2)
        self.assertTrue(result["content_protected"])
        self.assertEqual(result["media_probe_status"], "not_tested")

    async def test_inspection_preserves_server_error_details(self) -> None:
        class Rejected(FakeTransport):
            async def latest_message_id(self, peer_id):
                raise PermissionError("server refused history request")

        result = await ExportEngine(Rejected([5]), self.store, "main").inspect("42")
        self.assertEqual(result["server_error"]["type"], "PermissionError")
        self.assertIn("refused", result["server_error"]["message"])

    async def test_progress_reports_committed_page_and_monotonic_cursor(self) -> None:
        progress = []
        engine = ExportEngine(FakeTransport([1, 4, 8, 10, 20]), self.store, "main", progress.append)
        await engine.export("42", self.root / "progress.jsonl", page_size=2)
        self.assertEqual([item["messages"] for item in progress], [2, 4, 5])
        self.assertEqual([item["current_message_id"] for item in progress], [10, 4, 1])
        self.assertTrue(all(item["phase"] == "history" for item in progress))

    async def test_rejects_non_decreasing_history_cursor(self) -> None:
        class Broken(FakeTransport):
            async def history_page(self, peer_id, *, max_id, limit):
                return HistoryPage([message(max_id)], True)

        engine = ExportEngine(Broken([4]), self.store, "main")
        with self.assertRaises(ExportInvariantError):
            await engine.export("42", self.root / "bad.jsonl")

    async def test_last_message_probe_followed_by_empty_page_is_failure(self) -> None:
        class EmptyAfterProbe(FakeTransport):
            async def history_page(self, peer_id, *, max_id, limit):
                return HistoryPage([], False)

        engine = ExportEngine(EmptyAfterProbe([4]), self.store, "main")
        with self.assertRaises(EmptyHistory):
            await engine.export("42", self.root / "missing.jsonl")
        self.assertEqual(self.store.list_jobs()[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
