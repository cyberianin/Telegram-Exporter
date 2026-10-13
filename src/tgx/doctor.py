from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
from pathlib import Path


def doctor_report(data_dir: Path | None = None) -> dict[str, object]:
    if data_dir is None and os.getenv("TGX_DATA_DIR"):
        data_dir = Path(os.environ["TGX_DATA_DIR"])
    if data_dir is None:
        try:
            from platformdirs import user_data_dir

            root = Path(user_data_dir("tgx", "TGX"))
        except ImportError:
            root = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "tgx"
    else:
        root = data_dir
    checks: list[dict[str, str]] = []

    checks.append(
        {
            "name": "python",
            "status": "ok" if sys.version_info >= (3, 12) else "error",
            "detail": sys.version.split()[0],
        }
    )
    for distribution in ("telethon", "typer", "platformdirs"):
        try:
            version = importlib.metadata.version(distribution)
            status, detail = "ok", version
        except importlib.metadata.PackageNotFoundError:
            status, detail = "error", "not installed"
        checks.append({"name": distribution, "status": status, "detail": detail})

    configured = bool(os.getenv("TGX_API_ID") and os.getenv("TGX_API_HASH"))
    checks.append(
        {
            "name": "telegram_api_credentials",
            "status": "ok" if configured else "info",
            "detail": "configured" if configured else "interactive setup available",
        }
    )

    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".tgx-write-check"
        probe.write_bytes(b"")
        probe.unlink()
        free_bytes = shutil.disk_usage(root).free
        checks.append({"name": "data_directory", "status": "ok", "detail": str(root)})
        checks.append({"name": "free_disk_bytes", "status": "ok", "detail": str(free_bytes)})
    except OSError as error:
        checks.append({"name": "data_directory", "status": "error", "detail": str(error)})

    return {
        "ready": all(check["status"] != "error" for check in checks),
        "checks": checks,
    }
