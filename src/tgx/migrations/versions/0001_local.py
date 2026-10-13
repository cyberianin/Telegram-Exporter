"""Local durable storage, including upgrade of the experimental 0.1 schema."""

from alembic import op
from sqlalchemy import inspect

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    statements = [
        """CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, peer_id INTEGER NOT NULL,
            title TEXT NOT NULL, initial_max_id INTEGER NOT NULL, next_offset_id INTEGER NOT NULL,
            pages_read INTEGER NOT NULL DEFAULT 0, messages_saved INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, output_path TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS messages (
            account_id TEXT NOT NULL, peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            date TEXT, body TEXT NOT NULL, PRIMARY KEY(account_id,peer_id,message_id))""",
        "CREATE INDEX IF NOT EXISTS ix_messages_peer_date ON messages(peer_id,date)",
        """CREATE TABLE IF NOT EXISTS job_messages (
            job_id TEXT NOT NULL REFERENCES jobs(job_id), account_id TEXT NOT NULL,
            peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            PRIMARY KEY(job_id,message_id))""",
        """CREATE TABLE IF NOT EXISTS message_versions (
            account_id TEXT, peer_id INTEGER, message_id INTEGER, observed_at TEXT, body TEXT,
            PRIMARY KEY(account_id,peer_id,message_id,observed_at))""",
        """CREATE TABLE IF NOT EXISTS media_files (
            account_id TEXT, peer_id INTEGER, message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            path TEXT, expected_size INTEGER, actual_size INTEGER, sha256 TEXT, error TEXT,
            attempts INTEGER DEFAULT 0, PRIMARY KEY(account_id,peer_id,message_id))""",
        """CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY, job_id TEXT, cursor INTEGER, request TEXT,
            count INTEGER, error TEXT, created_at TEXT)""",
        "CREATE INDEX IF NOT EXISTS media_status ON media_files(status,account_id,peer_id)",
    ]
    for statement in statements:
        op.execute(statement)
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("jobs")}
    for name, definition in {
        "options": "TEXT NOT NULL DEFAULT '{}'",
        "retry_after": "TEXT",
        "last_error": "TEXT",
        "last_committed_message_id": "INTEGER",
        "scan_status": "TEXT",
        "unverified_ranges": "TEXT NOT NULL DEFAULT '[]'",
        "control": "TEXT",
        "media_queue_state": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in columns:
            op.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
    op.execute("""INSERT OR IGNORE INTO job_messages
        SELECT j.job_id,m.account_id,m.peer_id,m.message_id FROM jobs j JOIN messages m
        ON j.account_id=m.account_id AND j.peer_id=m.peer_id
        WHERE m.message_id<=j.initial_max_id AND m.message_id>=j.next_offset_id
        AND j.options='{}'""")


def downgrade():
    raise RuntimeError("Destructive storage downgrades are intentionally disabled")
