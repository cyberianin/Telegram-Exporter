from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Annotated

import typer

from .archive import verify_jsonl
from .doctor import doctor_report
from .engine import ExportEngine
from .errors import AuthenticationRequired, EmptyHistory
from .options import ExportOptions
from .service import profile_paths
from .session_lock import SessionGuard
from .storage import SQLiteStore
from .transport import TelethonTransport

app = typer.Typer(no_args_is_help=True, help="Resumable Telegram history exporter")
auth_app = typer.Typer(no_args_is_help=True)
jobs_app = typer.Typer(no_args_is_help=True)
archive_app = typer.Typer(no_args_is_help=True)
media_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(jobs_app, name="jobs")
app.add_typer(archive_app, name="archive")
app.add_typer(media_app, name="media")


def _paths(account: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account):
        raise typer.BadParameter("Account profile may contain only letters, digits, '_' and '-'")
    return profile_paths(account)


def _credentials(*, interactive: bool = True) -> tuple[int, str]:
    api_id = os.getenv("TGX_API_ID")
    api_hash = os.getenv("TGX_API_HASH")
    saved = profile_paths("main")[0].parent / "credentials.json"
    if (not api_id or not api_hash) and saved.exists():
        credentials = json.loads(saved.read_text(encoding="utf-8"))
        api_id, api_hash = str(credentials["api_id"]), credentials["api_hash"]
    if not interactive and (not api_id or not api_hash):
        raise typer.BadParameter("Set TGX_API_ID and TGX_API_HASH when using --non-interactive")
    if not api_id:
        api_id = typer.prompt("Telegram api_id", hide_input=False)
    if not api_hash:
        api_hash = typer.prompt("Telegram api_hash", hide_input=True)
    try:
        return int(api_id), api_hash
    except ValueError as error:
        raise typer.BadParameter("TGX_API_ID must be an integer") from error


def _client(account: str, *, interactive: bool = True):
    try:
        from telethon import TelegramClient
    except ImportError as error:
        raise typer.BadParameter(
            "Install the Telegram exporter package with: pip install telegram-exporter"
        ) from error
    api_id, api_hash = _credentials(interactive=interactive)
    session, _ = _paths(account)
    return TelegramClient(str(session), api_id, api_hash, flood_sleep_threshold=0)


async def _run_export(
    peer: str,
    output: Path,
    account: str,
    page_size: int,
    job_id: str | None = None,
    json_progress: bool = False,
    non_interactive: bool = False,
    options: ExportOptions | None = None,
) -> tuple[str, int]:
    client = _client(account, interactive=not non_interactive)
    session, database = _paths(account)
    store = SQLiteStore(database)
    transport = TelethonTransport(client, account)
    try:
        with SessionGuard(session.with_suffix(".lock")):
            try:
                await transport.connect()
                if not await transport.is_authorized():
                    raise AuthenticationRequired("Run `tgx auth login` before exporting")

                def report_progress(values: dict[str, int | float | str]) -> None:
                    if json_progress:
                        typer.echo(json.dumps(values), err=True)
                    else:
                        typer.echo(
                            f"pages={values['pages']} messages={values['messages']} "
                            f"current_id={values['current_message_id']} "
                            f"rate={values['messages_per_second']}/s",
                            err=True,
                        )

                engine = ExportEngine(transport, store, account, report_progress)
                result = await engine.export(
                    peer, output, page_size=page_size, job_id=job_id, options=options
                )
                row = store.get_job(result)
                return result, int(row["messages_saved"])
            finally:
                await transport.disconnect()
    finally:
        store.close()


@auth_app.command("login")
def auth_login(
    account: Annotated[str, typer.Option("--account", help="Local profile name")] = "main",
) -> None:
    async def login() -> None:
        client = _client(account)
        with SessionGuard(_paths(account)[0].with_suffix(".lock")):
            try:
                await client.start(
                    phone=lambda: typer.prompt("Telegram phone"),
                    code_callback=lambda: typer.prompt("Telegram login code", hide_input=True),
                    password=lambda: typer.prompt("Telegram two-step password", hide_input=True),
                )
                me = await client.get_me()
                typer.echo(f"Authorized profile {account} as user id {me.id}")
            finally:
                await client.disconnect()

    asyncio.run(login())


@auth_app.command("status")
def auth_status(
    account: Annotated[str, typer.Option("--account")] = "main",
) -> None:
    async def status() -> None:
        client = _client(account)
        with SessionGuard(_paths(account)[0].with_suffix(".lock")):
            try:
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    typer.echo(f"authorized profile={account} user_id={me.id}")
                else:
                    typer.echo(f"not authorized profile={account}")
            finally:
                await client.disconnect()

    asyncio.run(status())


@auth_app.command("qr")
def auth_qr(
    account: Annotated[str, typer.Option("--account")] = "main",
) -> None:
    async def login_qr() -> None:
        import qrcode
        from telethon.errors import SessionPasswordNeededError

        client = _client(account)
        with SessionGuard(_paths(account)[0].with_suffix(".lock")):
            try:
                await client.connect()
                login = await client.qr_login()
                code = qrcode.QRCode(border=1)
                code.add_data(login.url)
                code.print_ascii(invert=True)
                try:
                    await login.wait()
                except SessionPasswordNeededError:
                    await client.sign_in(
                        password=typer.prompt(
                            "Telegram two-step verification password", hide_input=True
                        )
                    )
                me = await client.get_me()
                typer.echo(f"Authorized profile {account} as user id {me.id}")
            finally:
                await client.disconnect()

    asyncio.run(login_qr())


@auth_app.command("logout")
def auth_logout(
    account: Annotated[str, typer.Option("--account")] = "main",
) -> None:
    async def logout() -> None:
        client = _client(account)
        with SessionGuard(_paths(account)[0].with_suffix(".lock")):
            try:
                await client.connect()
                if await client.is_user_authorized():
                    await client.log_out()
            finally:
                await client.disconnect()
        typer.echo(f"Logged out profile {account}")

    asyncio.run(logout())


@app.command("accounts")
def accounts() -> None:
    root = _paths("main")[0].parent
    for session in sorted(root.glob("*.session")):
        typer.echo(session.stem)


@app.command("dialogs")
def dialogs(account: Annotated[str, typer.Option("--account")] = "main") -> None:
    async def run() -> None:
        client = _client(account)
        transport = TelethonTransport(client, account)
        with SessionGuard(_paths(account)[0].with_suffix(".lock")):
            await transport.connect()
            try:
                if not await transport.is_authorized():
                    raise AuthenticationRequired("Run `tgx auth login` first")
                for dialog in await transport.dialogs():
                    typer.echo(
                        json.dumps(
                            {
                                "peer_id": dialog.peer_id,
                                "title": dialog.title,
                                "username": dialog.username,
                                "kind": dialog.kind,
                                "protected_content": dialog.protected_content,
                            },
                            ensure_ascii=False,
                        )
                    )
            finally:
                await transport.disconnect()

    asyncio.run(run())


@app.command("inspect")
def inspect(
    peer: str,
    account: Annotated[str, typer.Option("--account")] = "main",
) -> None:
    async def run() -> None:
        client = _client(account)
        transport = TelethonTransport(client, account)
        with SessionGuard(_paths(account)[0].with_suffix(".lock")):
            await transport.connect()
            try:
                if not await transport.is_authorized():
                    raise AuthenticationRequired("Run `tgx auth login` first")
                store = SQLiteStore(_paths(account)[1])
                try:
                    result = await ExportEngine(transport, store, account).inspect(peer)
                    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
                finally:
                    store.close()
            finally:
                await transport.disconnect()

    asyncio.run(run())


@app.command("export")
def export(
    peer: str,
    output: Annotated[Path, typer.Option("--output", "-o")],
    account: Annotated[str, typer.Option("--account")] = "main",
    page_size: Annotated[int, typer.Option("--page-size", min=1, max=100)] = 100,
    json_progress: Annotated[bool, typer.Option("--json-progress")] = False,
    non_interactive: Annotated[bool, typer.Option("--non-interactive")] = False,
    mode: Annotated[str, typer.Option("--mode")] = "text",
    media: Annotated[str, typer.Option("--media")] = "metadata",
    format: Annotated[str, typer.Option("--format")] = "jsonl",
    strategy: Annotated[str, typer.Option("--strategy")] = "direct",
    min_id: Annotated[int, typer.Option("--min-id", min=0)] = 0,
    max_id: Annotated[int | None, typer.Option("--max-id", min=1)] = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    media_concurrency: Annotated[int, typer.Option("--media-concurrency", min=1, max=16)] = 3,
) -> None:
    try:
        job_id, count = asyncio.run(
            _run_export(
                peer,
                output,
                account,
                page_size,
                json_progress=json_progress,
                non_interactive=non_interactive,
                options=ExportOptions(
                    mode=mode,
                    media=media,
                    format=format,
                    strategy=strategy,
                    min_id=min_id,
                    max_id=max_id,
                    since=since,
                    until=until,
                    page_size=page_size,
                    media_concurrency=media_concurrency,
                ),
            )
        )
    except EmptyHistory as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    except Exception as error:
        typer.echo(f"{type(error).__name__}: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"job_id={job_id} messages={count} output={output}")


@jobs_app.command("list")
def jobs_list(account: Annotated[str, typer.Option("--account")] = "main") -> None:
    store = SQLiteStore(_paths(account)[1])
    try:
        for job in store.list_jobs():
            typer.echo(json.dumps(dict(job), ensure_ascii=False))
    finally:
        store.close()


@jobs_app.command("status")
def jobs_status(job_id: str, account: Annotated[str, typer.Option("--account")] = "main") -> None:
    store = SQLiteStore(_paths(account)[1])
    try:
        job = store.get_job(job_id)
        if job is None:
            raise typer.BadParameter(f"Unknown job: {job_id}")
        typer.echo(json.dumps(dict(job), ensure_ascii=False, indent=2))
    finally:
        store.close()


@jobs_app.command("resume")
def jobs_resume(
    job_id: str,
    account: Annotated[str, typer.Option("--account")] = "main",
    page_size: Annotated[int, typer.Option("--page-size", min=1, max=100)] = 100,
    json_progress: Annotated[bool, typer.Option("--json-progress")] = False,
    non_interactive: Annotated[bool, typer.Option("--non-interactive")] = False,
) -> None:
    store = SQLiteStore(_paths(account)[1])
    job = store.get_job(job_id)
    store.close()
    if job is None:
        raise typer.BadParameter(f"Unknown job: {job_id}")
    if job["account_id"] != account:
        raise typer.BadParameter("Job belongs to another account")
    control_store = SQLiteStore(_paths(account)[1])
    control_store.update_job(job_id, control=None)
    control_store.close()
    asyncio.run(
        _run_export(
            str(job["peer_id"]),
            Path(job["output_path"]),
            account,
            page_size,
            job_id,
            json_progress,
            non_interactive,
        )
    )
    typer.echo(f"Resumed job {job_id}")


@archive_app.command("verify")
def archive_verify(path: Path) -> None:
    try:
        result = verify_jsonl(path)
    except (OSError, ValueError) as error:
        typer.echo(f"invalid: {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("doctor")
def doctor() -> None:
    result = doctor_report()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ready"]:
        raise typer.Exit(code=1)


@archive_app.command("convert")
def archive_convert(
    path: Path,
    output: Annotated[Path, typer.Option("--output", "-o")],
    format: Annotated[str, typer.Option("--format")] = "html",
) -> None:
    from .formats import convert_archive

    result = convert_archive(path, output, format)
    typer.echo(json.dumps(result, ensure_ascii=False))


@archive_app.command("import")
def archive_import(
    path: Path, peer_id: int, account: Annotated[str, typer.Option("--account")] = "main"
) -> None:
    from .importer import import_archive

    store = SQLiteStore(_paths(account)[1])
    try:
        typer.echo(import_archive(store, path, account, peer_id))
    finally:
        store.close()


def _control(job_id: str, account: str, action: str) -> None:
    store = SQLiteStore(_paths(account)[1])
    try:
        job = store.get_job(job_id)
        if job is None or job["account_id"] != account:
            raise typer.BadParameter(f"Unknown job for this account: {job_id}")
        store.update_job(job_id, control=action)
        if job["status"] not in ("running", "downloading", "writing"):
            from .engine import utcnow

            store.finish_job(job_id, "paused" if action == "pause" else "cancelled", utcnow())
        typer.echo(f"{action} requested: {job_id}")
    finally:
        store.close()


@jobs_app.command("pause")
def jobs_pause(job_id: str, account: Annotated[str, typer.Option("--account")] = "main") -> None:
    _control(job_id, account, "pause")


@jobs_app.command("cancel")
def jobs_cancel(job_id: str, account: Annotated[str, typer.Option("--account")] = "main") -> None:
    _control(job_id, account, "cancel")


@media_app.command("retry")
def media_retry(job_id: str, account: Annotated[str, typer.Option("--account")] = "main") -> None:
    from .media import MediaWorker

    async def run():
        store = SQLiteStore(_paths(account)[1])
        transport = TelethonTransport(_client(account), account)
        try:
            job = store.get_job(job_id)
            if job is None or job["account_id"] != account:
                raise typer.BadParameter("Unknown job for this account")
            with SessionGuard(_paths(account)[0].with_suffix(".lock")):
                await transport.connect()
                await transport.resolve_peer(job["peer_id"])
                options = ExportOptions.model_validate_json(job["options"] or "{}")
                options = options.model_copy(update={"media": "full"})
                output = Path(job["output_path"])
                result = await MediaWorker(transport, store).run(
                    job_id, output.parent / f"{output.stem}-media", options, retry=True
                )
                typer.echo(json.dumps(result))
                from .formats import write_format

                metadata = json.loads(Path(f"{output}.manifest.json").read_text(encoding="utf-8"))
                write_format(
                    (json.loads(body) for body in store.stream_job(job_id)),
                    output,
                    "jsonl",
                    metadata,
                )
        finally:
            await transport.disconnect()
            store.close()

    asyncio.run(run())


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    import secrets

    import uvicorn

    from .api import create_app

    token = os.getenv("TGX_OWNER_TOKEN")
    if not token and host in ("127.0.0.1", "localhost", "::1"):
        token_path = _paths("main")[0].parent / "owner.token"
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(32)
            token_path.write_text(token, encoding="utf-8")
            token_path.chmod(0o600)
        typer.echo(f"Open local panel: http://127.0.0.1:{port}/#token={token}")
    uvicorn.run(create_app(token=token), host=host, port=port, access_log=False)


@app.command("bot")
def bot() -> None:
    from .bot import run_bot

    asyncio.run(run_bot())


if __name__ == "__main__":
    app()
