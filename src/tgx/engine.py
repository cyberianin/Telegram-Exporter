from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .errors import EmptyHistory, ExportInvariantError
from .models import DialogRecord
from .options import ExportOptions
from .storage import SQLiteStore
from .transport import TelegramTransport


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


async def blocking(function, *args, **kwargs):
    """Complete a file operation before cancellation can close its database connection."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class ExportEngine:
    def __init__(
        self,
        transport: TelegramTransport,
        store: SQLiteStore,
        account_id: str,
        on_progress: Callable[[dict[str, int | float | str]], None] | None = None,
    ) -> None:
        self.transport = transport
        self.store = store
        self.account_id = account_id
        self.on_progress = on_progress

    async def inspect(self, peer: str | int, page_size: int = 20) -> dict[str, object]:
        base: dict[str, object] = {
            "peer_resolved": False,
            "account_authorized": await self.transport.is_authorized(),
            "content_protected": None,
            "last_message_id": None,
            "history_probe_count": 0,
            "history_readable": False,
            "media_probe_status": "not_tested",
            "server_error": None,
            "recommended_strategy": "direct",
        }
        if not base["account_authorized"]:
            base["server_error"] = {"type": "AUTH_REQUIRED", "message": "Account is not authorized"}
            return base
        try:
            resolved = await self.transport.resolve_peer(peer)
            base.update(
                peer_resolved=True,
                peer_id=resolved.peer_id,
                title=resolved.title,
                kind=resolved.kind,
                content_protected=resolved.protected_content,
            )
            upper = await self.transport.latest_message_id(resolved.peer_id)
            page = await self.transport.history_page(
                resolved.peer_id, max_id=upper + 1 if upper else 0, limit=page_size
            )
            samples = [
                {
                    "transport": type(self.transport).__name__,
                    "request": "GetHistory",
                    "cursor": upper + 1,
                    "count": len(page.messages),
                }
            ]
            if not page.messages:
                page = await self.transport.history_page(
                    resolved.peer_id, max_id=upper + 1, limit=1
                )
                samples.append(
                    {
                        "transport": type(self.transport).__name__,
                        "request": "GetHistory(limit=1)",
                        "count": len(page.messages),
                    }
                )
                if not page.messages and hasattr(self.transport, "compatibility_page"):
                    page = await self.transport.compatibility_page(
                        resolved.peer_id, max_id=upper + 1, limit=page_size
                    )
                    samples.append(
                        {
                            "transport": type(self.transport).__name__,
                            "request": "iter_messages",
                            "count": len(page.messages),
                        }
                    )
            base["samples"] = samples
            base["recommended_strategy"] = "direct" if page.messages else "recovery"
        except Exception as error:
            base["server_error"] = {"type": type(error).__name__, "message": str(error)}
            return base
        base["last_message_id"] = upper
        base["history_probe_count"] = len(page.messages)
        base["history_readable"] = bool(page.messages)
        if page.messages and hasattr(self.transport, "probe_media"):
            media_message = next((m for m in page.messages if m.media), None)
            if media_message:
                try:
                    await self.transport.probe_media(resolved.peer_id, media_message.message_id)
                    base["media_probe_status"] = "readable_sample"
                except Exception as error:
                    base["media_probe_status"] = "denied"
                    base["media_error"] = {"type": type(error).__name__, "message": str(error)}
            else:
                base["media_probe_status"] = "no_media_in_sample"
        if page.messages and hasattr(self.transport, "get_messages"):
            try:
                checked = await self.transport.get_messages(
                    resolved.peer_id, [page.messages[0].message_id]
                )
                base["get_messages_probe_count"] = len(checked)
            except Exception as error:
                base["get_messages_error"] = {"type": type(error).__name__, "message": str(error)}
        return base

    async def export(
        self,
        peer: str | int,
        output: Path,
        *,
        page_size: int = 100,
        job_id: str | None = None,
        options: ExportOptions | None = None,
    ) -> str:
        options = options or ExportOptions(page_size=page_size)
        if options.mode == "recovery" and options.strategy == "direct":
            options = options.model_copy(update={"strategy": "recovery"})
        page_size = options.page_size
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        resolved = await self.transport.resolve_peer(peer)
        output.parent.mkdir(parents=True, exist_ok=True)
        job_id = job_id or uuid.uuid4().hex[:12]
        job = self.store.get_job(job_id)
        if job is None:
            upper = await self.transport.latest_message_id(resolved.peer_id)
            if options.max_id is not None:
                upper = min(upper, options.max_id)
            if options.mode == "incremental":
                row = self.store.connection.execute(
                    "SELECT MAX(message_id) FROM messages WHERE account_id=? AND peer_id=?",
                    (self.account_id, resolved.peer_id),
                ).fetchone()
                options = options.model_copy(update={"min_id": max(options.min_id, row[0] or 0)})
            if upper == 0:
                page = await self.transport.history_page(
                    resolved.peer_id, max_id=0, limit=page_size
                )
                if not page.messages:
                    raise EmptyHistory("Telegram returned no readable messages for this dialog")
                upper = max(message.message_id for message in page.messages)
            self.store.create_job(
                job_id=job_id,
                account_id=self.account_id,
                peer_id=resolved.peer_id,
                title=resolved.title,
                initial_max_id=upper,
                output_path=output,
                now=utcnow(),
            )
            self.store.update_job(job_id, options=options.model_dump_json())
            job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["account_id"] != self.account_id or job["peer_id"] != resolved.peer_id:
            raise ExportInvariantError("Checkpoint account or peer does not match requested export")
        if job["options"] != "{}":
            options = ExportOptions.model_validate_json(job["options"])
            page_size = options.page_size
        if not 1 <= int(job["next_offset_id"]) <= int(job["initial_max_id"]) + 1:
            raise ExportInvariantError("Checkpoint cursor is outside its captured history boundary")
        output = Path(job["output_path"])
        if job["status"] == "cancelled":
            raise ExportInvariantError("Cancelled job cannot be resumed")
        if options.strategy == "diagnostic":
            result = await self.inspect(peer)
            self.store.record_probe(
                job_id,
                int(job["next_offset_id"]),
                "diagnostic",
                int(result["history_probe_count"]),
                utcnow(),
            )
            self.store.finish_job(job_id, "completed", utcnow())
            return job_id
        self.store.update_job(job_id, retry_after=None, last_error=None)
        self.store.finish_job(job_id, "running", utcnow())
        if options.mode == "media" and not int(job["messages_saved"]):
            with self.store.connection:
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO job_messages SELECT ?,account_id,peer_id,message_id "
                    "FROM messages WHERE account_id=? AND peer_id=? AND message_id<=?",
                    (job_id, self.account_id, resolved.peer_id, job["initial_max_id"]),
                )
                self.store.connection.execute(
                    "UPDATE jobs SET messages_saved=(SELECT COUNT(*) FROM job_messages "
                    "WHERE job_id=?) WHERE job_id=?",
                    (job_id, job_id),
                )
            job = self.store.get_job(job_id)
        cursor = int(job["next_offset_id"])
        had_saved_messages = int(job["messages_saved"]) > 0
        saved_before_run = int(job["messages_saved"])
        previous_min: int | None = None
        started = time.monotonic()
        try:
            while cursor > max(1, options.min_id + 1) and options.mode != "media":
                current = self.store.get_job(job_id)
                if current["control"] in ("pause", "cancel", "delete"):
                    self.store.finish_job(
                        job_id,
                        "cancelled" if current["control"] in ("cancel", "delete") else "paused",
                        utcnow(),
                    )
                    return job_id
                method = (
                    self.transport.compatibility_page
                    if options.strategy == "compatibility"
                    else self.transport.history_page
                )
                for attempt in range(options.connection_retries + 1):
                    try:
                        page = await method(resolved.peer_id, max_id=cursor, limit=page_size)
                        break
                    except (ConnectionError, TimeoutError):
                        if attempt == options.connection_retries:
                            raise
                        await self.transport.disconnect()
                        await asyncio.sleep(0.25 * 2**attempt)
                        await self.transport.connect()
                self.store.record_probe(
                    job_id, cursor, options.strategy, len(page.messages), utcnow()
                )
                if not page.messages:
                    page = await self.transport.history_page(
                        resolved.peer_id, max_id=cursor, limit=1
                    )
                    if not page.messages:
                        if hasattr(self.transport, "compatibility_page"):
                            page = await self.transport.compatibility_page(
                                resolved.peer_id, max_id=cursor, limit=page_size
                            )
                            self.store.record_probe(
                                job_id, cursor, "iter_messages", len(page.messages), utcnow()
                            )
                    if not page.messages:
                        if not had_saved_messages:
                            self.store.finish_job(job_id, "failed", utcnow())
                            raise EmptyHistory(
                                "Telegram reported a last message but returned no readable history"
                            )
                        self.store.finish_job(job_id, "writing", utcnow())
                        break
                ids = [message.message_id for message in page.messages]
                if options.strategy == "recovery" and cursor == int(job["initial_max_id"]) + 1:
                    if int(job["initial_max_id"]) not in ids:
                        from .recovery import RecoveryEngine

                        extra = await RecoveryEngine(self.transport).recover(
                            resolved.peer_id, cursor, page_size, [int(job["initial_max_id"])]
                        )
                        combined = {m.message_id: m for m in page.messages + extra.messages}
                        page = type(page)(
                            sorted(combined.values(), key=lambda m: m.message_id, reverse=True),
                            True,
                        )
                        ids = [m.message_id for m in page.messages]
                        self.store.record_probe(
                            job_id,
                            cursor,
                            "recovery:GetMessages+iter_messages",
                            len(extra.messages),
                            utcnow(),
                        )
                if any(message_id >= cursor for message_id in ids):
                    if options.strategy == "recovery" and hasattr(
                        self.transport, "compatibility_page"
                    ):
                        recovered = await self.transport.compatibility_page(
                            resolved.peer_id, max_id=cursor, limit=page_size
                        )
                        self.store.record_probe(
                            job_id,
                            cursor,
                            "recovery:iter_messages",
                            len(recovered.messages),
                            utcnow(),
                        )
                        if recovered.messages and all(
                            m.message_id < cursor for m in recovered.messages
                        ):
                            page = recovered
                            ids = [m.message_id for m in page.messages]
                    if any(message_id >= cursor for message_id in ids):
                        self.store.update_job(
                            job_id,
                            unverified_ranges=json.dumps(
                                [{"cursor": cursor, "reason": "nondecreasing history response"}]
                            ),
                        )
                        raise ExportInvariantError(
                            "History response violated exclusive max_id cursor"
                        )
                min_id = min(ids)
                if previous_min is not None and min_id >= previous_min:
                    raise ExportInvariantError("History cursor did not move toward older messages")
                next_cursor = min_id
                selected = [
                    message for message in page.messages if message.message_id > options.min_id
                ]
                if options.since or options.until:
                    selected = [
                        message for message in selected if self._in_dates(message.date, options)
                    ]
                self.store.save_page(job_id, selected, next_cursor, utcnow())
                had_saved_messages = True
                cursor = next_cursor
                previous_min = min_id
                if self.on_progress is not None:
                    current_job = self.store.get_job(job_id)
                    elapsed = max(time.monotonic() - started, 0.001)
                    saved = int(current_job["messages_saved"])
                    saved_this_run = max(saved - saved_before_run, 0)
                    self.on_progress(
                        {
                            "job_id": job_id,
                            "phase": "history",
                            "pages": int(current_job["pages_read"]),
                            "messages": saved,
                            "current_message_id": min_id,
                            "elapsed_seconds": round(elapsed, 3),
                            "messages_per_second": round(saved_this_run / elapsed, 2),
                        }
                    )
            else:
                self.store.finish_job(job_id, "writing", utcnow())
        except EmptyHistory:
            self.store.finish_job(job_id, "failed", utcnow())
            raise
        except BaseException as error:
            self._save_failure(job_id, error)
            raise
        try:
            from .media import MediaWorker

            if options.media in ("full", "selected") or options.mode == "media":
                self.store.finish_job(job_id, "downloading", utcnow())
                media_counts = await MediaWorker(self.transport, self.store).run(
                    job_id, output.parent / f"{output.stem}-media", options, retry=True
                )
                self.store.update_job(job_id, media_queue_state=json.dumps(media_counts))
                control = self.store.get_job(job_id)["control"]
                if control in ("pause", "cancel", "delete"):
                    self.store.finish_job(
                        job_id, "paused" if control == "pause" else "cancelled", utcnow()
                    )
                    return job_id
            count = await blocking(self.write_jsonl, resolved, output, job_id)
            committed = int(self.store.get_job(job_id)["messages_saved"])
            if count != committed:
                raise ExportInvariantError("Archive count differs from committed job messages")
            if (
                had_saved_messages
                and options.min_id == 0
                and not options.since
                and not options.until
            ):
                highest = self.store.connection.execute(
                    "SELECT MAX(message_id) FROM job_messages WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                if highest != int(job["initial_max_id"]):
                    self.store.update_job(
                        job_id,
                        unverified_ranges=json.dumps(
                            [
                                {
                                    "max_id": int(job["initial_max_id"]),
                                    "reason": "upper boundary missing",
                                }
                            ]
                        ),
                    )
            digest = hashlib.sha256()
            with output.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    digest.update(chunk)
            manifest = {
                "format": "tgx-manifest-v1",
                "job_id": job_id,
                "account_id": self.account_id,
                "peer_id": resolved.peer_id,
                "title": resolved.title,
                "kind": resolved.kind,
                "content_protected": resolved.protected_content,
                "message_count": count,
                "initial_max_id": int(job["initial_max_id"]),
                "output": output.name,
                "sha256": digest.hexdigest(),
                "scan_status": "server_returned_range_enumerated",
                "unverified_ranges": json.loads(self.store.get_job(job_id)["unverified_ranges"]),
                "warnings": [
                    "Enumeration completion does not prove that deleted or server-hidden messages "
                    "existed or were recovered."
                ],
                "generated_at": utcnow(),
                "started_at": job["created_at"],
                "final_cursor": cursor,
                "pages_read": self.store.get_job(job_id)["pages_read"],
                "mode": options.mode,
                "media_count": self.store.connection.execute(
                    "SELECT COUNT(*) FROM media_files JOIN job_messages "
                    "USING(account_id,peer_id,message_id) WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
                "error_count": self.store.connection.execute(
                    "SELECT COUNT(*) FROM media_files JOIN job_messages "
                    "USING(account_id,peer_id,message_id) WHERE job_id=? AND status='failed'",
                    (job_id,),
                ).fetchone()[0],
                "media_queue_state": json.loads(self.store.get_job(job_id)["media_queue_state"]),
            }
            manifest_path = output.with_name(f"{output.name}.manifest.json")
            if manifest["unverified_ranges"]:
                manifest["scan_status"] = "partial_unverified"
            manifest_tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
            manifest_tmp.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            manifest_tmp.replace(manifest_path)
            from .archive import verify_jsonl

            await blocking(verify_jsonl, output)
            if options.format != "jsonl":
                from .formats import convert_archive

                destination = (
                    output.with_suffix(".html")
                    if options.format == "html"
                    else output.with_suffix("." + options.format)
                )
                await blocking(
                    convert_archive,
                    output,
                    destination,
                    options.format,
                    page_size=options.segment_size,
                )
        except BaseException as error:
            self._save_failure(job_id, error)
            raise
        self.store.finish_job(
            job_id,
            "completed_with_warnings"
            if manifest["unverified_ranges"] or manifest["media_queue_state"].get("failed")
            else "completed",
            utcnow(),
        )
        return job_id

    def write_jsonl(self, dialog: DialogRecord, output: Path, job_id: str | None = None) -> int:
        count = 0
        temporary = output.with_suffix(output.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            source = (
                self.store.stream_job(job_id)
                if job_id
                else self.store.stream_messages(self.account_id, dialog.peer_id)
            )
            for body in source:
                record = json.loads(body)
                if record.get("sender_id") == dialog.peer_id and not record.get("sender_name"):
                    record["sender_name"] = dialog.title
                    record["sender_username"] = dialog.username
                download = record.get("media_download") or {}
                if download.get("path"):
                    media_path = Path(download["path"])
                    if not media_path.resolve().is_relative_to(output.parent.resolve()):
                        import shutil

                        destination = output.parent / f"{output.stem}-media" / media_path.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(media_path, destination)
                        self.store.update_media(
                            record["account_id"],
                            record["peer_id"],
                            record["message_id"],
                            path=str(destination.resolve()),
                        )
                        download["path"] = str(destination.resolve())
                    download["path"] = os.path.relpath(download["path"], output.parent).replace(
                        "\\", "/"
                    )
                    body = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                file.write(body)
                file.write("\n")
                count += 1
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(output)
        return count

    @staticmethod
    def _in_dates(date: str | None, options: ExportOptions) -> bool:
        if not date:
            return False
        value = datetime.fromisoformat(date)
        return (not options.since or value >= datetime.fromisoformat(options.since)) and (
            not options.until or value <= datetime.fromisoformat(options.until)
        )

    def _save_failure(self, job_id: str, error: BaseException) -> None:
        while isinstance(error, BaseExceptionGroup) and error.exceptions:
            error = error.exceptions[0]
        name = type(error).__name__
        status = "paused"
        if "Auth" in name or "Session" in name:
            status = "waiting_auth"
        elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
            status = "waiting_storage"
        elif "FloodWait" in name:
            from datetime import timedelta

            status = "waiting_rate_limit"
            retry = datetime.now(UTC) + timedelta(seconds=getattr(error, "seconds", 0))
            self.store.update_job(job_id, retry_after=retry.isoformat())
        self.store.update_job(job_id, last_error=f"{name}: {error}")
        self.store.finish_job(job_id, status, utcnow())

    async def resume(self, job_id: str, *, page_size: int = 100) -> str:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return await self.export(
            job["peer_id"], Path(job["output_path"]), page_size=page_size, job_id=job_id
        )
