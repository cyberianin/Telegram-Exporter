import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tgx.formats import write_format
from tgx.media import sha256_file
from tgx.package import media_folder, package_archive


class PackageTests(unittest.TestCase):
    def test_portable_media_links_and_named_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "photo.jpg"
            photo.write_bytes(b"test photo")
            source = root / "Канал_2026-10-13_12-00-00.jsonl"
            item = {
                "account_id": "main",
                "peer_id": 42,
                "message_id": 1,
                "text": "Caption",
                "media": {"photo": {}},
                "media_download": {
                    "status": "downloaded",
                    "path": str(photo),
                    "sha256": sha256_file(photo),
                    "actual_size": photo.stat().st_size,
                },
            }
            write_format([item], source, "jsonl", {"title": "Канал"})
            archive = package_archive(source, "txt")
            self.assertEqual(archive.stem, source.stem)
            with zipfile.ZipFile(archive) as bundle:
                prefix = source.stem + "/"
                self.assertIsNone(bundle.testzip())
                self.assertIn(prefix + "photos/photo.jpg", bundle.namelist())
                record = json.loads(bundle.read(prefix + "messages.jsonl"))
                self.assertEqual(record["media_download"]["path"], "photos/photo.jpg")
                page = bundle.read(prefix + "messages.html").decode()
                self.assertIn('href="photos/photo.jpg"', page)
                self.assertIn(prefix + "messages.txt", bundle.namelist())
                self.assertIn(prefix + "index.html", bundle.namelist())

    def test_desktop_media_categories(self):
        self.assertEqual(media_folder({"photo": {}}), "photos")
        self.assertEqual(
            media_folder(
                {"document": {"attributes": [{"type": "DocumentAttributeAudio", "voice": True}]}}
            ),
            "voice_messages",
        )
        self.assertEqual(
            media_folder(
                {
                    "document": {
                        "attributes": [{"_": "DocumentAttributeVideo", "round_message": True}]
                    }
                }
            ),
            "round_video_messages",
        )
