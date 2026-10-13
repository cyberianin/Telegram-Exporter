from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tgx.archive import verify_jsonl


class ArchiveTests(unittest.TestCase):
    def test_validates_manifest_and_ordered_jsonl(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.jsonl"
            rows = [
                {"account_id": "main", "peer_id": -1, "message_id": 1},
                {"account_id": "main", "peer_id": -1, "message_id": 2},
            ]
            data = "".join(json.dumps(row) + "\n" for row in rows).encode()
            path.write_bytes(data)
            (Path(f"{path}.manifest.json")).write_text(
                json.dumps({"message_count": 2, "sha256": hashlib.sha256(data).hexdigest()}),
                encoding="utf-8",
            )
            self.assertEqual(verify_jsonl(path)["messages"], 2)

    def test_rejects_modified_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.jsonl"
            path.write_text('{"account_id":"main","peer_id":-1,"message_id":1}\n')
            Path(f"{path}.manifest.json").write_text(
                json.dumps({"message_count": 1, "sha256": "wrong"})
            )
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verify_jsonl(path)


if __name__ == "__main__":
    unittest.main()
