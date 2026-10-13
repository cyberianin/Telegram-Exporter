from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_data_dir

from .engine import ExportEngine, blocking, utcnow
from .errors import AuthenticationRequired
from .options import ExportOptions
from .session_lock import SessionGuard
from .storage import SQLiteStore
from .transport import TelethonTransport


def data_root() -> Path:
    root = Path(os.getenv("TGX_DATA_DIR") or user_data_dir("tgx", "TGX"))
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def profile_paths(account: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account):
        raise ValueError("Invalid account profile")
    root = data_root()
    return root / f"{account}.session", root / "tgx.sqlite3"


class SessionManager:
    def __init__(self, factory=None) -> None:
        self.factory = factory
        self.locks: dict[str, asyncio.Lock] = {}
        self.active_authorized: dict[str, bool] = {}

    @asynccontextmanager
    async def session(self, account: str, *, authorized: bool = True):
        session, _ = profile_paths(account)
        async with self.locks.setdefault(account, asyncio.Lock()):
            with SessionGuard(session.with_suffix(".lock")):
                if self.factory:
                    transport = self.factory(account)
                else:
                    from telethon import TelegramClient
                    from telethon.sessions import StringSession

                    api_id, api_hash = os.getenv("TGX_API_ID"), os.getenv("TGX_API_HASH")
                    config_file = session.parent / "credentials.json"
                    if (not api_id or not api_hash) and config_file.exists():
                        configuration = json.loads(config_file.read_text(encoding="utf-8"))
                        api_id, api_hash = configuration["api_id"], configuration["api_hash"]
                    if not api_id or not api_hash:
                        raise AuthenticationRequired("Configure TGX_API_ID and TGX_API_HASH")
                    encrypted = session.with_suffix(".session.enc")
                    master_key = os.getenv("TGX_MASTER_KEY")
                    cipher = None
                    session_value = str(session)
                    if master_key:
                        from cryptography.fernet import Fernet

                        if session.exists():
                            raise ValueError(
                                "Plaintext session exists; move it offline "
                                "before encrypted server login"
                            )
                        cipher = Fernet(master_key.encode())
                        session_value = StringSession(
                            cipher.decrypt(encrypted.read_bytes()).decode()
                            if encrypted.exists()
                            else ""
                        )
                    elif encrypted.exists():
                        raise AuthenticationRequired(
                            "TGX_MASTER_KEY is required for this encrypted profile"
                        )
                    transport = TelethonTransport(
                        TelegramClient(
                            session_value, int(api_id), api_hash, flood_sleep_threshold=0
                        ),
                        account,
                    )
                try:
                    await transport.connect()
                    is_authorized = await transport.is_authorized()
                    if authorized and not is_authorized:
                        raise AuthenticationRequired("Authorize this profile using tgx auth login")
                    if session.exists():
                        session.chmod(0o600)
                    self.active_authorized[account] = is_authorized
                    yield transport
                finally:
                    self.active_authorized.pop(account, None)
                    if not self.factory and cipher is not None:
                        saved = transport.client.session.save()
                        if saved:
                            temporary = encrypted.with_suffix(".tmp")
                            temporary.write_bytes(cipher.encrypt(saved.encode()))
                            temporary.chmod(0o600)
                            temporary.replace(encrypted)
                    await transport.disconnect()


class ExportService:
    """Durable queue shared by the HTTP, CLI and bot management surfaces.

    One bounded worker owns Telegram sessions; clients never own an export task.
    """

    def __init__(self, root: Path | None = None, session_manager: SessionManager | None = None):
        self.root = root or data_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "tgx.sqlite3"
        self.sessions = session_manager or SessionManager()
        self.store = SQLiteStore(self.database)
        self.store.connection.execute("""
            CREATE TABLE IF NOT EXISTS requests(job_id TEXT PRIMARY KEY, account TEXT,
                peer TEXT, options TEXT, output TEXT, status TEXT, error TEXT)
        """)
        self.store.connection.commit()
        self.store.connection.execute("""CREATE TABLE IF NOT EXISTS dialogs_metadata (
            account_id TEXT,peer_id INTEGER,title TEXT,username TEXT,kind TEXT,
            content_protected INTEGER,PRIMARY KEY(account_id,peer_id))""")
        self.store.connection.commit()
        columns = {r[1] for r in self.store.connection.execute("PRAGMA table_info(requests)")}
        if "created_at" not in columns:
            self.store.connection.execute("ALTER TABLE requests ADD COLUMN created_at TEXT")
            self.store.connection.execute(
                "UPDATE requests SET created_at=(SELECT created_at FROM jobs "
                "WHERE jobs.job_id=requests.job_id) WHERE created_at IS NULL"
            )
            self.store.connection.commit()
        self.task: asyncio.Task | None = None
        self.pending_auth: dict[str, tuple[object, str, str, object, object]] = {}
        self.auth_expiry: dict[str, asyncio.Task] = {}
        self.auth_states: dict[str, str] = {}
        self.auth_delivery: dict[str, dict] = {}
        self.qr_tasks: dict[str, asyncio.Task] = {}

    def create(self, account: str, peer: str, options: ExportOptions) -> str:
        profile_paths(account)
        job_id = uuid.uuid4().hex
        output = self.root / "exports" / job_id / "messages.jsonl"
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO requests(job_id,account,peer,options,output,status,error,created_at) "
                "VALUES (?,?,?,?,?,'queued',NULL,?)",
                (job_id, account, peer, options.model_dump_json(), str(output), utcnow()),
            )
        return job_id

    def status(self, job_id: str) -> dict:
        request = self.store.connection.execute(
            "SELECT * FROM requests WHERE job_id=?", (job_id,)
        ).fetchone()
        job = self.store.get_job(job_id)
        if request is None and job is None:
            raise KeyError(job_id)
        result = dict(job) if job is not None else {"job_id": job_id, "status": request["status"]}
        if request is not None:
            result["account_id"] = request["account"]
            result.setdefault("created_at", request["created_at"])
            result.setdefault(
                "title",
                self._dialog_title(request["account"], request["peer"])
                or (
                    "Выгрузка не запущена"
                    if request["status"] == "cancelled"
                    else "Определяем канал…"
                ),
            )
            if request["status"] == "deleting":
                result["status"] = "deleting"
            if request["error"] and not result.get("last_error"):
                result["last_error"] = request["error"]
        return result

    def delete(self, job_id: str) -> dict:
        state = self.status(job_id)
        job = self.store.get_job(job_id)
        if job:
            self.store.update_job(job_id, control="delete")
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE requests SET status='deleting' WHERE job_id=?", (job_id,)
            )
        if state["status"] not in ("running", "downloading", "writing"):
            self._remove_job(job_id)
            return {"status": "deleted"}
        return {"status": "deleting"}

    def _remove_job(self, job_id: str) -> None:
        import shutil

        root = self.root.resolve()
        directory = (root / "exports" / job_id).resolve()
        if not directory.is_relative_to(root / "exports") or directory == root / "exports":
            raise ValueError("Invalid job artifact directory")
        if directory.exists():
            shutil.rmtree(directory)
        with self.store.connection:
            self.store.connection.execute("DROP TABLE IF EXISTS temp.removed_messages")
            self.store.connection.execute(
                "CREATE TEMP TABLE removed_messages AS SELECT account_id,peer_id,message_id "
                "FROM job_messages WHERE job_id=?",
                (job_id,),
            )
            self.store.connection.execute("DELETE FROM job_messages WHERE job_id=?", (job_id,))
            self.store.connection.execute("DELETE FROM diagnostics WHERE job_id=?", (job_id,))
            self.store.connection.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            self.store.connection.execute("DELETE FROM requests WHERE job_id=?", (job_id,))
            for table in ("message_versions", "media_files", "messages"):
                self.store.connection.execute(f"""DELETE FROM {table} WHERE EXISTS (
                    SELECT 1 FROM removed_messages r WHERE r.account_id={table}.account_id
                    AND r.peer_id={table}.peer_id AND r.message_id={table}.message_id)
                    AND NOT EXISTS (SELECT 1 FROM job_messages j
                    WHERE j.account_id={table}.account_id AND j.peer_id={table}.peer_id
                    AND j.message_id={table}.message_id)""")
            self.store.connection.execute("DROP TABLE temp.removed_messages")

    def list_jobs(self) -> list[dict]:
        ids = [row[0] for row in self.store.connection.execute("SELECT job_id FROM requests")]
        known = set(ids)
        ids.extend(row["job_id"] for row in self.store.list_jobs() if row["job_id"] not in known)
        return [self.status(job_id) for job_id in ids]

    def control(self, job_id: str, action: str) -> None:
        state = self.status(job_id)
        if action not in ("pause", "resume", "cancel"):
            raise ValueError("Unknown action")
        if action == "resume" and state["status"] in (
            "running",
            "downloading",
            "writing",
            "cancelled",
        ):
            raise ValueError("Job cannot be resumed in its current state")
        if self.store.get_job(job_id):
            self.store.update_job(job_id, control=None if action == "resume" else action)
            if action == "resume":
                self.store.update_job(job_id, last_error=None)
                self.store.finish_job(job_id, "queued", utcnow())
            elif state["status"] not in ("running", "downloading", "writing"):
                self.store.finish_job(
                    job_id, "paused" if action == "pause" else "cancelled", utcnow()
                )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE requests SET status=?,error=NULL WHERE job_id=?",
                (
                    "queued"
                    if action == "resume"
                    else "paused"
                    if action == "pause"
                    else "cancelled",
                    job_id,
                ),
            )

    async def start(self) -> None:
        # OS guard also prevents accidentally running two workers on the same queue.
        self.guard = SessionGuard(self.root / "worker.lock")
        self.guard.__enter__()
        self.task = asyncio.create_task(self._worker())

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.guard.__exit__(None, None, None)
        for account in list(self.pending_auth):
            await self._close_auth(account)
        self.store.close()

    async def _worker(self) -> None:
        # Requests left running after a crash are eligible to resume from their checkpoint.
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE requests SET status='queued' WHERE status='running'"
            )
        while True:
            for row in self.store.connection.execute(
                "SELECT job_id FROM requests WHERE status='deleting'"
            ).fetchall():
                job = self.store.get_job(row[0])
                if job is None or job["status"] not in ("running", "writing", "downloading"):
                    self._remove_job(row[0])
            rows = self.store.connection.execute(
                "SELECT r.* FROM requests r LEFT JOIN jobs j ON j.job_id=r.job_id "
                "WHERE r.status IN ('queued','waiting_rate_limit') "
                "AND (j.retry_after IS NULL OR julianday(j.retry_after)<=julianday('now')) "
                "ORDER BY r.rowid LIMIT 1"
            ).fetchall()
            if not rows:
                await asyncio.sleep(0.3)
                continue
            request = rows[0]
            job_id = request["job_id"]
            checkpoint = self.store.get_job(job_id)
            if checkpoint and checkpoint["retry_after"]:
                if datetime.fromisoformat(checkpoint["retry_after"]) > datetime.now(UTC):
                    await asyncio.sleep(1)
                    continue
            with self.store.connection:
                self.store.connection.execute(
                    "UPDATE requests SET status='running' WHERE job_id=?", (job_id,)
                )
            store = SQLiteStore(self.database)
            try:
                async with self.sessions.session(request["account"]) as transport:
                    queued_state = self.store.connection.execute(
                        "SELECT status FROM requests WHERE job_id=?", (job_id,)
                    ).fetchone()[0]
                    if queued_state in ("paused", "cancelled", "deleting"):
                        continue
                    output = Path(request["output"])
                    if not store.get_job(job_id):
                        from .files import archive_name

                        dialog = await transport.resolve_peer(request["peer"])
                        output = output.with_name(
                            archive_name(dialog.title, request["created_at"]) + ".jsonl"
                        )
                        with self.store.connection:
                            self.store.connection.execute(
                                "UPDATE requests SET output=? WHERE job_id=?", (str(output), job_id)
                            )
                    engine = ExportEngine(transport, store, request["account"])
                    await engine.export(
                        request["peer"],
                        output,
                        job_id=job_id,
                        options=ExportOptions.model_validate_json(request["options"]),
                    )
                state = store.get_job(job_id)["status"]
                options = ExportOptions.model_validate_json(request["options"])
                if state in ("completed", "completed_with_warnings") and (
                    options.media in ("full", "selected") or options.format == "html"
                ):
                    from .package import package_archive

                    store.finish_job(job_id, "packaging", utcnow())
                    await blocking(package_archive, output, options.format)
                    control = store.get_job(job_id)["control"]
                    if control in ("pause", "cancel"):
                        state = "paused" if control == "pause" else "cancelled"
                    store.finish_job(job_id, state, utcnow())
                deletion = self.store.connection.execute(
                    "SELECT status FROM requests WHERE job_id=?", (job_id,)
                ).fetchone()
                if deletion and deletion[0] == "deleting":
                    self._remove_job(job_id)
                    continue
                with self.store.connection:
                    self.store.connection.execute(
                        "UPDATE requests SET status=?,error=NULL WHERE job_id=?", (state, job_id)
                    )
            except asyncio.CancelledError:
                # Retain request as running so next service startup can recover it.
                raise
            except Exception as error:
                job = store.get_job(job_id)
                state = (
                    job["status"]
                    if job
                    else "waiting_auth"
                    if isinstance(error, AuthenticationRequired)
                    else "failed"
                )
                if state in ("running", "writing", "downloading", "packaging", "queued"):
                    state = "failed"
                    if job:
                        store.update_job(job_id, last_error=f"{type(error).__name__}: {error}")
                        store.finish_job(job_id, state, utcnow())
                with self.store.connection:
                    self.store.connection.execute(
                        "UPDATE requests SET status=?,error=? WHERE job_id=?",
                        (state, f"{type(error).__name__}: {error}", job_id),
                    )
            finally:
                if store is not None:
                    store.close()

    async def dialogs(self, account: str) -> list[dict]:
        if self.sessions.active_authorized.get(account):
            cached = [
                {
                    "peer_id": row["peer_id"],
                    "title": row["title"],
                    "username": row["username"],
                    "kind": row["kind"],
                    "protected_content": bool(row["content_protected"]),
                }
                for row in self.store.connection.execute(
                    "SELECT * FROM dialogs_metadata WHERE account_id=?", (account,)
                )
            ]
            if cached:
                return cached
        async with self.sessions.session(account) as transport:
            result = [asdict(item) for item in await transport.dialogs()]
            with self.store.connection:
                self.store.connection.executemany(
                    "INSERT INTO dialogs_metadata VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(account_id,peer_id) DO UPDATE SET title=excluded.title, "
                    "username=excluded.username,kind=excluded.kind,content_protected=excluded.content_protected",
                    [
                        (
                            account,
                            item["peer_id"],
                            item["title"],
                            item["username"],
                            item["kind"],
                            item["protected_content"],
                        )
                        for item in result
                    ],
                )
            return result

    def _dialog_title(self, account: str, peer: str) -> str | None:
        from telethon.utils import resolve_id

        from .resolver import parse_peer

        try:
            reference = parse_peer(peer)
        except ValueError:
            return None
        matches = []
        for row in self.store.connection.execute(
            "SELECT * FROM dialogs_metadata WHERE account_id=?", (account,)
        ):
            if reference.kind == "id":
                if (
                    row["peer_id"] == reference.value
                    or resolve_id(row["peer_id"])[0] == reference.value
                ):
                    matches.append(row["title"])
            elif str(reference.value).casefold() in (
                str(row["username"]).casefold(),
                row["title"].casefold(),
            ):
                matches.append(row["title"])
        return matches[0] if len(matches) == 1 else None

    async def inspect(self, account: str, peer: str) -> dict:
        from .recovery import NoForwardAwareScanner

        async with self.sessions.session(account) as transport:
            return await NoForwardAwareScanner(
                ExportEngine(transport, self.store, account)
            ).inspect(peer)

    def accounts(self) -> list[str]:
        return sorted(
            {path.stem for path in self.root.glob("*.session")}
            | {path.name.removesuffix(".session.enc") for path in self.root.glob("*.session.enc")}
        )

    def artifacts(self, job_id: str) -> list[Path]:
        self.status(job_id)
        directory = self.root / "exports" / job_id
        return sorted(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix not in (".tmp", ".part")
        )

    async def login(self, account: str, phone: str) -> dict:
        from telethon.utils import parse_phone

        phone = parse_phone(phone)
        if not phone:
            raise ValueError("Invalid phone number; include the country code")
        if account in self.pending_auth:
            if (
                self.pending_auth[account][1] == phone
                and self.auth_states.get(account) == "waiting_code"
            ):
                return await self.resend_code(account)
            await self._close_auth(account)
        context = self.sessions.session(account, authorized=False)
        transport = await context.__aenter__()
        try:
            if await transport.is_authorized():
                await context.__aexit__(None, None, None)
                return {"status": "authorized"}
            sent = await transport.client.send_code_request(phone)
            self.pending_auth[account] = (
                transport,
                phone,
                sent.phone_code_hash,
                context,
                asyncio.get_running_loop().time(),
            )
            self.auth_states[account] = "waiting_code"
            self.auth_delivery[account] = self._delivery(sent)

            async def expire():
                await asyncio.sleep(300)
                if account in self.pending_auth:
                    await self._close_auth(account)

            self.auth_expiry[account] = asyncio.create_task(expire())
            return {"status": "waiting_code", "delivery": self.auth_delivery[account]}
        except BaseException:
            await context.__aexit__(None, None, None)
            raise

    @staticmethod
    def _delivery(sent) -> dict:
        kind = type(getattr(sent, "type", None)).__name__
        next_kind = type(getattr(sent, "next_type", None)).__name__
        timeout = getattr(sent, "timeout", None)
        return {
            "type": kind,
            "next_type": next_kind,
            "length": getattr(getattr(sent, "type", None), "length", None),
            "retry_at": (datetime.now(UTC).timestamp() + timeout if timeout else None),
            "can_resend": getattr(sent, "next_type", None) is not None,
        }

    async def resend_code(self, account: str) -> dict:
        from telethon.tl.functions.auth import ResendCodeRequest

        if account not in self.pending_auth or self.auth_states.get(account) != "waiting_code":
            raise ValueError("Request a code first")
        delivery = self.auth_delivery.get(account, {})
        if not delivery.get("can_resend"):
            raise ValueError("Telegram did not offer another delivery method; use QR login")
        if delivery.get("retry_at") and datetime.now(UTC).timestamp() < delivery["retry_at"]:
            seconds = int(delivery["retry_at"] - datetime.now(UTC).timestamp()) + 1
            raise ValueError(f"Telegram allows resending in {seconds} seconds")
        transport, phone, _, context, started = self.pending_auth[account]
        sent = await transport.client(ResendCodeRequest(phone, self.pending_auth[account][2]))
        self.pending_auth[account] = (transport, phone, sent.phone_code_hash, context, started)
        self.auth_delivery[account] = self._delivery(sent)
        return {"status": "waiting_code", "delivery": self.auth_delivery[account]}

    async def qr_login(self, account: str) -> dict:
        import base64
        import io

        import qrcode.image.svg
        from telethon.errors import SessionPasswordNeededError

        if account in self.pending_auth:
            await self._close_auth(account)
        context = self.sessions.session(account, authorized=False)
        transport = await context.__aenter__()
        try:
            if await transport.is_authorized():
                await context.__aexit__(None, None, None)
                return await self.auth_status(account)
            qr = await transport.client.qr_login()
            buffer = io.BytesIO()
            qrcode.make(qr.url, image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
            self.pending_auth[account] = (
                transport,
                "",
                "",
                context,
                asyncio.get_running_loop().time(),
            )
            self.auth_states[account] = "waiting_qr"

            async def wait_qr():
                try:
                    await qr.wait(timeout=120)
                    await self._close_auth(account)
                except SessionPasswordNeededError:
                    self.auth_states[account] = "waiting_password"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._close_auth(account)

            self.qr_tasks[account] = asyncio.create_task(wait_qr())
            return {
                "status": "waiting_qr",
                "qr_image": "data:image/svg+xml;base64,"
                + base64.b64encode(buffer.getvalue()).decode(),
                "expires_at": qr.expires.isoformat(),
            }
        except BaseException:
            await context.__aexit__(None, None, None)
            raise

    def configure_credentials(self, api_id: int, api_hash: str) -> None:
        if api_id <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ValueError(
                "api_id must be positive and api_hash must contain 32 hexadecimal characters"
            )
        path = self.root / "credentials.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    async def confirm_login(self, account: str, code: str | None, password: str | None) -> dict:
        from telethon.errors import SessionPasswordNeededError

        if account not in self.pending_auth:
            raise ValueError("Start login first")
        transport, phone, code_hash, _, started = self.pending_auth[account]
        if asyncio.get_running_loop().time() - started > 300:
            await self._close_auth(account)
            raise ValueError("Login expired; request a new code")
        try:
            if self.auth_states.get(account) != "waiting_password":
                if not code:
                    raise ValueError("Enter the Telegram confirmation code")
                try:
                    await transport.client.sign_in(
                        phone=phone, code=code, phone_code_hash=code_hash
                    )
                except SessionPasswordNeededError:
                    self.auth_states[account] = "waiting_password"
                    if not password:
                        return {"status": "waiting_password"}
            if self.auth_states.get(account) == "waiting_password":
                if not password:
                    return {"status": "waiting_password"}
                await transport.client.sign_in(password=password)
        except SessionPasswordNeededError:
            self.auth_states[account] = "waiting_password"
            return {"status": "waiting_password"}
        me = await transport.client.get_me()
        await self._close_auth(account)
        return {
            "status": "authorized",
            "user": {"id": me.id, "name": me.first_name, "username": me.username},
        }

    async def auth_status(self, account: str) -> dict:
        profile_paths(account)
        configured = bool(
            (os.getenv("TGX_API_ID") and os.getenv("TGX_API_HASH"))
            or (self.root / "credentials.json").exists()
        )
        if not configured and not self.sessions.factory:
            return {"status": "needs_api", "api_configured": False}
        if account in self.pending_auth:
            return {
                "status": self.auth_states.get(account, "waiting_code"),
                "delivery": self.auth_delivery.get(account),
            }

        if account in self.sessions.active_authorized:
            return {
                "status": "authorized"
                if self.sessions.active_authorized[account]
                else "not_authorized"
            }

        async with self.sessions.session(account, authorized=False) as transport:
            if not await transport.is_authorized():
                return {"status": "not_authorized"}
            me = await transport.client.get_me()
            return {
                "status": "authorized",
                "user": {"id": me.id, "name": me.first_name, "username": me.username},
            }

    async def logout(self, account: str, *, forget_credentials: bool = True) -> dict:
        if account in self.pending_auth:
            await self._close_auth(account)
        # Pause queued and current work before waiting for this profile's session manager.
        for job in self.store.list_jobs():
            if job["account_id"] == account and job["status"] in (
                "queued",
                "running",
                "downloading",
                "writing",
            ):
                self.store.update_job(job["job_id"], control="pause")
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE requests SET status='paused' WHERE account=? "
                "AND status IN ('queued','running')",
                (account,),
            )
        async with self.sessions.session(account, authorized=False) as transport:
            if await transport.is_authorized():
                await transport.client.log_out()
        session, _ = profile_paths(account)
        root = session.parent.resolve()
        for suffix in (
            ".session",
            ".session-journal",
            ".session-wal",
            ".session-shm",
            ".session.enc",
            ".session.tmp",
        ):
            path = root / (account + suffix)
            if not path.resolve().is_relative_to(root):
                raise ValueError("Invalid session path")
            path.unlink(missing_ok=True)
        if forget_credentials:
            for name in ("credentials.json", "credentials.tmp"):
                (root / name).unlink(missing_ok=True)
        with self.store.connection:
            self.store.connection.execute(
                "DELETE FROM dialogs_metadata WHERE account_id=?", (account,)
            )
        return {
            "status": "not_authorized",
            "credentials_removed": forget_credentials,
            "api_configured": bool(os.getenv("TGX_API_ID") and os.getenv("TGX_API_HASH")),
        }

    async def _close_auth(self, account: str) -> None:
        qr_task = self.qr_tasks.pop(account, None)
        if qr_task and qr_task is not asyncio.current_task():
            qr_task.cancel()
            await asyncio.gather(qr_task, return_exceptions=True)
        timer = self.auth_expiry.pop(account, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        _, _, _, context, _ = self.pending_auth.pop(account)
        self.auth_states.pop(account, None)
        self.auth_delivery.pop(account, None)
        await context.__aexit__(None, None, None)
