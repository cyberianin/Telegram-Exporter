from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from .archive import verify_jsonl
from .formats import records, write_format
from .media import sha256_file


def media_folder(descriptor: dict) -> str:
    if "photo" in descriptor:
        return "photos"
    document = descriptor.get("document") or {}
    attributes = document.get("attributes") or []
    for attribute in attributes:
        kind = attribute.get("_") or attribute.get("type") or ""
        if "Sticker" in kind:
            return "stickers"
        if "Animated" in kind:
            return "animations"
    for attribute in attributes:
        kind = attribute.get("_") or attribute.get("type") or ""
        if "Video" in kind:
            return "round_video_messages" if attribute.get("round_message") else "video_files"
        if "Audio" in kind:
            return "voice_messages" if attribute.get("voice") else "files"
    return "video_files" if document.get("mime_type", "").startswith("video/") else "files"


def package_archive(source: Path, format: str, *, name: str | None = None) -> Path:
    """Portable folder with bounded history conversion and streamed ZIP creation."""
    verify_jsonl(source)
    metadata = json.loads(Path(f"{source}.manifest.json").read_text(encoding="utf-8"))
    folder = source.parent / (name or source.stem)
    folder.mkdir(parents=True, exist_ok=True)

    def portable_records():
        for record in records(source):
            download = record.get("media_download") or {}
            if download.get("status") == "downloaded" and download.get("path"):
                original = Path(download["path"])
                if not original.is_absolute():
                    original = source.parent / original
                relative = Path(media_folder(record.get("media") or {})) / original.name
                destination = folder / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists() or sha256_file(destination) != download.get("sha256"):
                    shutil.copyfile(original, destination)
                if sha256_file(destination) != download.get("sha256"):
                    raise ValueError(f"Media checksum mismatch: {relative}")
                download["path"] = relative.as_posix()
            yield record

    canonical = folder / "messages.jsonl"
    write_format(portable_records(), canonical, "jsonl", metadata)
    write_format(records(canonical), folder, "html", metadata)
    if format not in ("html", "jsonl"):
        write_format(records(canonical), folder / ("messages." + format), format, metadata)
    archive = source.with_suffix(".zip")
    temporary = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as bundle:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.name.endswith((".tmp", ".part")):
                bundle.write(path, (Path(folder.name) / path.relative_to(folder)).as_posix())
    temporary.replace(archive)
    return archive
