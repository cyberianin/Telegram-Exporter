from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .errors import TGXError
from .options import ExportOptions
from .service import ExportService


class CreateExport(BaseModel):
    account: str = "main"
    peer: str
    options: ExportOptions = Field(default_factory=ExportOptions)


class LoginRequest(BaseModel):
    account: str = "main"
    phone: str | None = None
    code: str | None = None
    password: str | None = None
    api_id: int | None = None
    api_hash: str | None = None


class LogoutRequest(BaseModel):
    account: str = "main"
    forget_credentials: bool = True


def create_app(service: ExportService | None = None, token: str | None = None) -> FastAPI:
    token = token or os.getenv("TGX_OWNER_TOKEN")
    if not token or len(token) < 24:
        raise ValueError("Set TGX_OWNER_TOKEN to a random secret of at least 24 characters")
    service = service or ExportService()

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="TGX", lifespan=lifespan, docs_url=None, redoc_url=None)

    async def owner(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "Owner token required")

    secure = [Depends(owner)]
    artifact_locks: dict[str, asyncio.Lock] = {}

    def friendly_filename(job_id: str, name: str) -> str:
        from .files import archive_name

        state = service.status(job_id)
        if name.startswith("messages") and state.get("title"):
            return archive_name(state["title"], state["created_at"]) + name[len("messages") :]
        return name

    def ticket(job_id: str, name: str) -> str:
        payload = (
            base64.urlsafe_b64encode(json.dumps([job_id, name, int(time.time()) + 900]).encode())
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + signature

    @app.get("/download/{signed}")
    async def temporary_download(signed: str):
        try:
            payload, signature = signed.rsplit(".", 1)
            expected = hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid signature")
            job_id, name, expires = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            if expires < time.time():
                raise ValueError("Download link expired")
            root = (service.root / "exports" / job_id).resolve()
            path = (root / name).resolve()
            if not path.is_relative_to(root) or path not in [
                p.resolve() for p in service.artifacts(job_id)
            ]:
                raise ValueError("Artifact unavailable")
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(403, "Invalid or expired download link") from error
        return FileResponse(path, filename=friendly_filename(job_id, path.name))

    @app.exception_handler(KeyError)
    async def missing(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Job or artifact not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(TGXError)
    async def telegram_state(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": f"{type(error).__name__}: {error}"}, status_code=409)

    from telethon.errors import RPCError

    @app.exception_handler(RPCError)
    async def telegram_rpc(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": f"{type(error).__name__}: {error}"}, status_code=502)

    @app.get("/", response_class=HTMLResponse)
    async def web():
        return Path(__file__).with_name("web.html").read_text(encoding="utf-8")

    @app.get("/assets/{name}")
    async def web_asset(name: str):
        if name not in ("web.css", "web.js"):
            raise HTTPException(404, "Asset not found")
        return FileResponse(Path(__file__).with_name(name))

    @app.post("/api/v1/exports", dependencies=secure, status_code=202)
    async def create(body: CreateExport):
        return {"job_id": service.create(body.account, body.peer, body.options)}

    @app.get("/api/v1/exports", dependencies=secure)
    async def jobs():
        return service.list_jobs()

    @app.get("/api/v1/exports/{job_id}", dependencies=secure)
    async def status(job_id: str):
        return service.status(job_id)

    @app.delete("/api/v1/exports/{job_id}", dependencies=secure)
    async def delete_job(job_id: str):
        return service.delete(job_id)

    @app.post("/api/v1/exports/{job_id}/{action}", dependencies=secure)
    async def control(job_id: str, action: str):
        service.control(job_id, action)
        return service.status(job_id)

    @app.get("/api/v1/exports/{job_id}/artifacts", dependencies=secure)
    async def artifacts(job_id: str):
        state = service.status(job_id)
        options = json.loads(state.get("options") or "{}")
        format = options.get("format", "jsonl")
        root = service.root / "exports" / job_id
        output = Path(state.get("output_path", ""))
        if state.get("status") in ("completed", "completed_with_warnings") and output.is_file():
            from .engine import blocking
            from .files import archive_name
            from .formats import convert_archive
            from .package import package_archive

            async with artifact_locks.setdefault(job_id, asyncio.Lock()):
                if options.get("media") in ("full", "selected") or format == "html":
                    folder_name = archive_name(state["title"], state["created_at"])
                    folder_manifest = output.parent / folder_name / "manifest.json"
                    version = (
                        json.loads(folder_manifest.read_text(encoding="utf-8")).get(
                            "render_version"
                        )
                        if folder_manifest.exists()
                        else None
                    )
                    if version != 2 or not output.with_suffix(".zip").is_file():
                        await blocking(package_archive, output, format, name=folder_name)
                elif format != "jsonl":
                    target = output.with_suffix("." + format)
                    manifest_path = Path(f"{target}.manifest.json")
                    version = (
                        json.loads(manifest_path.read_text(encoding="utf-8")).get("render_version")
                        if manifest_path.exists()
                        else None
                    )
                    if version != 2:
                        await blocking(convert_archive, output, target, format)
        paths = [path for path in service.artifacts(job_id) if path.parent == root]
        primary = (
            output.with_suffix(".zip")
            if options.get("media") in ("full", "selected") or format == "html"
            else output.with_suffix("." + format)
        )
        return [
            {
                "name": friendly_filename(job_id, path.relative_to(root).as_posix()),
                "size": path.stat().st_size,
                "primary": path == primary,
                "url": f"/api/v1/exports/{job_id}/files/{path.relative_to(root).as_posix()}",
                "download_url": "/download/" + ticket(job_id, path.relative_to(root).as_posix()),
            }
            for path in paths
        ]

    @app.get("/api/v1/exports/{job_id}/files/{name:path}", dependencies=secure)
    async def download(job_id: str, name: str):
        root = (service.root / "exports" / job_id).resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path not in [
            p.resolve() for p in service.artifacts(job_id)
        ]:
            raise HTTPException(404, "Artifact not found")
        return FileResponse(path, filename=friendly_filename(job_id, path.name))

    @app.get("/api/v1/exports/{job_id}/events", dependencies=secure)
    async def events(job_id: str):
        service.status(job_id)

        async def stream():
            previous = None
            while True:
                state = json.dumps(service.status(job_id), ensure_ascii=False)
                if state != previous:
                    yield f"data: {state}\n\n"
                    previous = state
                else:
                    yield ": keepalive\n\n"
                if json.loads(state)["status"] in ("completed", "failed", "cancelled", "paused"):
                    break
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/v1/accounts", dependencies=secure)
    async def accounts():
        return service.accounts()

    @app.get("/api/v1/dialogs", dependencies=secure)
    async def dialogs(account: str = "main"):
        return await service.dialogs(account)

    @app.get("/api/v1/dialogs/{peer}/inspect", dependencies=secure)
    async def inspect(peer: str, account: str = "main"):
        return await service.inspect(account, peer)

    @app.get("/api/v1/inspect", dependencies=secure)
    async def inspect_reference(peer: str, account: str = "main"):
        return await service.inspect(account, peer)

    @app.post("/api/v1/auth/login", dependencies=secure)
    async def login(body: LoginRequest):
        if body.api_id is not None or body.api_hash is not None:
            if not body.api_id or not body.api_hash:
                raise ValueError("Provide both api_id and api_hash")
            service.configure_credentials(body.api_id, body.api_hash)
        if body.phone:
            return await service.login(body.account, body.phone)
        return await service.confirm_login(body.account, body.code, body.password)

    @app.get("/api/v1/auth/status", dependencies=secure)
    async def auth_status(account: str = "main"):
        return await service.auth_status(account)

    @app.post("/api/v1/auth/resend", dependencies=secure)
    async def resend(body: LoginRequest):
        return await service.resend_code(body.account)

    @app.post("/api/v1/auth/logout", dependencies=secure)
    async def logout(body: LogoutRequest):
        return await service.logout(body.account, forget_credentials=body.forget_credentials)

    @app.post("/api/v1/auth/qr", dependencies=secure)
    async def qr_login(body: LoginRequest):
        if body.api_id and body.api_hash:
            service.configure_credentials(body.api_id, body.api_hash)
        return await service.qr_login(body.account)

    return app
