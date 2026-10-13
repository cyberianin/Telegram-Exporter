from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import MessageRecord


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        from .migrations import upgrade_database

        path.parent.mkdir(parents=True, exist_ok=True)
        upgrade_database(path)
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.connection.close()

    def create_job(
        self,
        *,
        job_id: str,
        account_id: str,
        peer_id: int,
        title: str,
        initial_max_id: int,
        output_path: Path,
        now: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO jobs(job_id, account_id, peer_id, title, initial_max_id, "
            "next_offset_id, status, output_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
            (
                job_id,
                account_id,
                peer_id,
                title,
                initial_max_id,
                initial_max_id + 1,
                str(output_path),
                now,
                now,
            ),
        )
        self.connection.commit()

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def list_jobs(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM jobs ORDER BY created_at DESC"))

    def save_page(
        self, job_id: str, messages: list[MessageRecord], next_offset_id: int, now: str
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        inserted = 0
        with self.connection:
            for message in messages:
                if message.account_id != job["account_id"] or message.peer_id != job["peer_id"]:
                    raise ValueError("Page contains a message from another account or peer")
                body = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
                old = self.connection.execute(
                    "SELECT body FROM messages WHERE account_id=? AND peer_id=? AND message_id=?",
                    (message.account_id, message.peer_id, message.message_id),
                ).fetchone()
                if old and old[0] != body:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO message_versions VALUES (?,?,?,?,?)",
                        (message.account_id, message.peer_id, message.message_id, now, old[0]),
                    )
                self.connection.execute(
                    "INSERT INTO messages(account_id, peer_id, message_id, date, body) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id,peer_id,message_id) "
                    "DO UPDATE SET date=excluded.date, body=excluded.body",
                    (message.account_id, message.peer_id, message.message_id, message.date, body),
                )
                cursor = self.connection.execute(
                    "INSERT OR IGNORE INTO job_messages VALUES (?,?,?,?)",
                    (job_id, message.account_id, message.peer_id, message.message_id),
                )
                inserted += cursor.rowcount
                if message.media:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO media_files(account_id,peer_id,message_id) "
                        "VALUES (?,?,?)",
                        (message.account_id, message.peer_id, message.message_id),
                    )
            self.connection.execute(
                "UPDATE jobs SET next_offset_id=?, pages_read=pages_read+1, "
                "messages_saved=messages_saved+?, updated_at=?, last_committed_message_id=? "
                "WHERE job_id=?",
                (next_offset_id, inserted, now, next_offset_id, job_id),
            )

    def finish_job(self, job_id: str, status: str, now: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (status, now, job_id)
            )

    def stream_messages(self, account_id: str, peer_id: int, *, batch_size: int = 500):
        cursor = self.connection.execute(
            "SELECT body FROM messages WHERE account_id=? AND peer_id=? ORDER BY message_id ASC",
            (account_id, peer_id),
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                yield row[0]

    def stream_job(self, job_id: str, batch_size: int = 500):
        cursor = self.connection.execute(
            """
            SELECT m.body, f.status, f.path, f.expected_size, f.actual_size, f.sha256, f.error
            FROM job_messages j JOIN messages m USING(account_id,peer_id,message_id)
            LEFT JOIN media_files f USING(account_id,peer_id,message_id)
            WHERE j.job_id=? ORDER BY m.message_id
        """,
            (job_id,),
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                record = json.loads(row[0])
                if row[1]:
                    record["media_download"] = dict(
                        zip(
                            ("status", "path", "expected_size", "actual_size", "sha256", "error"),
                            tuple(row)[1:],
                            strict=True,
                        )
                    )
                yield json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    def update_job(self, job_id: str, **values: object) -> None:
        allowed = {
            "options",
            "retry_after",
            "last_error",
            "scan_status",
            "unverified_ranges",
            "control",
            "media_queue_state",
        }
        if not values.keys() <= allowed:
            raise ValueError("Invalid job fields")
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET " + ",".join(f"{name}=?" for name in values) + " WHERE job_id=?",
                (*values.values(), job_id),
            )

    def record_probe(
        self, job_id: str, cursor: int, request: str, count: int, now: str, error: str | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO diagnostics(job_id,cursor,request,count,error,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, cursor, request, count, error, now),
            )

    def media_rows(self, job_id: str, retry: bool = False):
        statuses = "('pending','failed')" if retry else "('pending')"
        after = 0
        while True:
            # Finish the read statement before yielding to network tasks. A live
            # WAL read snapshot cannot be upgraded after another connection writes.
            rows = self.connection.execute(
                f"""
            SELECT f.*, m.body FROM media_files f JOIN job_messages j
            USING(account_id,peer_id,message_id) JOIN messages m
            USING(account_id,peer_id,message_id) WHERE j.job_id=? AND f.status IN {statuses}
            AND f.message_id>? ORDER BY f.message_id LIMIT 100
                """,
                (job_id, after),
            ).fetchall()
            if not rows:
                return
            after = rows[-1]["message_id"]
            yield from rows

    def update_media(self, account: str, peer: int, message: int, **values: object) -> None:
        if not values.keys() <= {
            "status",
            "path",
            "expected_size",
            "actual_size",
            "sha256",
            "error",
            "attempts",
        }:
            raise ValueError("Invalid media fields")
        with self.connection:
            self.connection.execute(
                "UPDATE media_files SET "
                + ",".join(f"{name}=?" for name in values)
                + " WHERE account_id=? AND peer_id=? AND message_id=?",
                (*values.values(), account, peer, message),
            )
