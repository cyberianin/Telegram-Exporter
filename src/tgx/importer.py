from __future__ import annotations

import json
import uuid
from pathlib import Path

from .engine import utcnow
from .models import MessageRecord
from .storage import SQLiteStore


def import_archive(
    store: SQLiteStore, path: Path, account: str, peer_id: int, *, title: str = "Imported archive"
) -> str:
    """Import JSONL or Telegram Desktop JSON, retaining per-record provenance."""
    job_id = uuid.uuid4().hex
    store.create_job(
        job_id=job_id,
        account_id=account,
        peer_id=peer_id,
        title=title,
        initial_max_id=0,
        output_path=path.with_suffix(".merged.jsonl"),
        now=utcnow(),
    )
    store.update_job(job_id, options=json.dumps({"mode": "reconcile"}))
    if path.suffix.lower() == ".json":
        # Desktop JSON is parsed incrementally through ijson, an optional import dependency.
        try:
            import ijson
        except ImportError as error:
            raise ValueError("Desktop JSON import requires pip install ijson") from error
        handle = path.open("rb")
        items = ijson.items(handle, "messages.item")
    else:
        handle = path.open(encoding="utf-8")
        items = (json.loads(line) for line in handle)
    upper = 0
    batch: list[MessageRecord] = []
    try:
        for item in items:
            identity = int(item.get("message_id", item.get("id", 0)))
            if identity <= 0:
                raise ValueError("Imported message lacks a valid ID")
            if item.get("peer_id", peer_id) != peer_id:
                raise ValueError("Imported peer differs from selected peer")
            text = item.get("text", "")
            if isinstance(text, list):
                text = "".join(
                    part if isinstance(part, str) else part.get("text", "") for part in text
                )
            fields = {
                name: item[name] for name in MessageRecord.__dataclass_fields__ if name in item
            }
            fields.update(
                account_id=account,
                peer_id=peer_id,
                message_id=identity,
                date=item.get("date"),
                text=text,
                source=f"import:{path.name}",
            )
            fields.setdefault("sender_name", item.get("from") or item.get("actor"))
            fields.setdefault("post_author", item.get("author"))
            fields.setdefault("reply_to_id", item.get("reply_to_message_id"))
            if item.get("forwarded_from") and "forward" not in fields:
                fields["forward"] = {"from_name": item["forwarded_from"]}
            for name in ("sender_id", "grouped_id", "reply_to_id", "views", "forwards", "media"):
                fields.setdefault(name, None)
            fields.setdefault("entities", [])
            fields.setdefault("raw_type", "ImportedMessage")
            batch.append(MessageRecord(**fields))
            upper = max(upper, identity)
            if len(batch) == 100:
                store.save_page(job_id, batch, 1, utcnow())
                batch.clear()
        if batch:
            store.save_page(job_id, batch, 1, utcnow())
        with store.connection:
            store.connection.execute(
                "UPDATE jobs SET initial_max_id=? WHERE job_id=?", (upper, job_id)
            )
        store.finish_job(job_id, "completed", utcnow())
    except BaseException:
        store.finish_job(job_id, "failed", utcnow())
        raise
    finally:
        handle.close()
    return job_id
