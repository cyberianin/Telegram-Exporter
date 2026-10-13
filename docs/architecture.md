# Architecture

`tgx_core` exposes the embedding API. `tgx_cli`, `tgx_api` and `tgx_bot` are entry points over
the implementation in `tgx`. A TelegramTransport protocol isolates Telethon. ExportEngine owns
page validation, range filtering, checkpointing, normalized storage and archive generation.

ExportService maintains a durable SQLite request queue. One worker and a cross-process worker
guard prevent duplicate queue processing. SessionManager serializes access to each profile and
SessionGuard prevents separate processes from opening the same MTProto session concurrently.
The bot talks to the running service through HTTP; no history reader exists in bot handlers.

History is committed page by page. `job_messages` associates a scan with its exact saved range;
`messages` deduplicates account/peer/message identities. Reconcile keeps the previous observed
body in `message_versions`. Alembic upgrades legacy local databases in place. SQLite uses WAL,
FULL synchronous writes and foreign keys.

MediaWorker uses a bounded producer/consumer queue and a fixed concurrency. It refreshes a
descriptor after expired references, writes to `.part`, verifies size/hash and atomically publishes.
Archive records store portable relative paths. Reusing previously downloaded media copies it into
the current archive directory, so deleting another job does not break the archive.

Serialization uses database cursors. Heavy final writers and hash validation run outside the event
loop. Cancellation awaits an in-flight atomic writer before closing its database connection.
JSONL, JSON, TXT, CSV, SQLite and zstd are streamed; HTML is paginated. A media index enables
streamed verification for formats that do not retain full message metadata.

Recovery uses smaller probes, compatible history and known-ID GetMessages. ID gaps alone are
not treated as missing messages. Failed boundary verification creates an unverified-range record.
Content protection never suppresses a history request. Server-denied media stays denied.

Owner Bearer authentication protects API/account/archive operations. Signed artifact links have
a short expiration. Telegram sessions are never stored in browser storage. Local profile files
are private; server sessions may be encrypted with a separate Fernet master key. Browser session
storage contains only the panel owner token, not a Telegram session, login code or 2FA password.

The supported server is a single-owner modular monolith with SQLite and one worker. PostgreSQL,
independent TDLib verification and multiple worker owners are future optional adapters.
