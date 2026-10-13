from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from pathlib import Path

from .options import ExportOptions
from .storage import SQLiteStore
from .transport import TelegramTransport


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class MediaWorker:
    def __init__(self, transport: TelegramTransport, store: SQLiteStore) -> None:
        self.transport, self.store = transport, store

    async def run(
        self, job_id: str, root: Path, options: ExportOptions, *, retry: bool = False
    ) -> dict[str, int]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=options.media_concurrency * 2)
        counts = {"downloaded": 0, "failed": 0, "skipped": 0}
        root.mkdir(parents=True, exist_ok=True)

        async def consume() -> None:
            while True:
                row = await queue.get()
                try:
                    if row is None:
                        return
                    job = self.store.get_job(job_id)
                    if job["control"] in ("pause", "cancel", "delete"):
                        continue
                    identity = (row["account_id"], row["peer_id"], row["message_id"])
                    body = json.loads(row["body"])
                    descriptor = body.get("media") or {}
                    kind = "photo" if "photo" in descriptor else "document"
                    if options.media == "selected" and kind not in options.selected_types:
                        self.store.update_media(*identity, status="skipped", error="type excluded")
                        counts["skipped"] += 1
                        continue
                    mime = (descriptor.get("document") or {}).get("mime_type", "")
                    extension = (
                        ".jpg" if kind == "photo" else mimetypes.guess_extension(mime) or ".bin"
                    )
                    destination = root / f"{row['peer_id']}_{row['message_id']}{extension}"
                    temporary = destination.with_suffix(".part")
                    for attempt in range(3):
                        try:
                            expected = await self.transport.download_media(
                                row["peer_id"], row["message_id"], temporary
                            )
                            actual = temporary.stat().st_size
                            if actual == 0 or (expected is not None and actual != expected):
                                raise ValueError(
                                    f"Media size mismatch: expected={expected}, actual={actual}"
                                )
                            digest = await asyncio.to_thread(sha256_file, temporary)
                            with temporary.open("r+b") as handle:
                                os.fsync(handle.fileno())
                            temporary.replace(destination)
                            self.store.update_media(
                                *identity,
                                status="downloaded",
                                path=str(destination.resolve()),
                                expected_size=expected,
                                actual_size=actual,
                                sha256=digest,
                                error=None,
                                attempts=row["attempts"] + attempt + 1,
                            )
                            counts["downloaded"] += 1
                            break
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            if "FloodWait" in type(error).__name__:
                                temporary.unlink(missing_ok=True)
                                raise
                            if attempt < 2 and isinstance(error, (ConnectionError, TimeoutError)):
                                await asyncio.sleep(0.25 * 2**attempt)
                                continue
                            temporary.unlink(missing_ok=True)
                            self.store.update_media(
                                *identity,
                                status="failed",
                                error=f"{type(error).__name__}: {error}",
                                attempts=row["attempts"] + attempt + 1,
                            )
                            counts["failed"] += 1
                            break
                finally:
                    queue.task_done()

        async def produce():
            for row in self.store.media_rows(job_id, retry):
                await queue.put(row)
            for _ in range(options.media_concurrency):
                await queue.put(None)

        async with asyncio.TaskGroup() as group:
            group.create_task(produce())
            for _ in range(options.media_concurrency):
                group.create_task(consume())
        return counts
