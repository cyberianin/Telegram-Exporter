from __future__ import annotations

import hashlib
import json
from pathlib import Path


def verify_jsonl(path: Path) -> dict[str, int | bool | str]:
    if path.is_dir() or path.suffix in (".json", ".csv", ".txt", ".sqlite", ".zst"):
        return verify_artifact(path)
    digest = hashlib.sha256()
    count = 0
    previous_id: dict[tuple[str, int], int] = {}
    with path.open("rb") as file:
        for line_number, line in enumerate(file, start=1):
            digest.update(line)
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError(f"Invalid JSONL object on line {line_number}: {error}") from error
            if not isinstance(record, dict):
                raise ValueError(f"Line {line_number} is not a JSON object")
            try:
                key = (str(record["account_id"]), int(record["peer_id"]))
                message_id = int(record["message_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Line {line_number} lacks a valid message identity") from error
            if key in previous_id and message_id <= previous_id[key]:
                raise ValueError(f"Message IDs are not strictly increasing on line {line_number}")
            previous_id[key] = message_id
            _verify_media(record, path.parent)
            count += 1
    manifest_path = path.with_name(f"{path.name}.manifest.json")
    if not manifest_path.exists():
        raise ValueError(f"Manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"Invalid manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if manifest.get("message_count") != count:
        raise ValueError("Manifest message_count does not match the JSONL archive")
    if manifest.get("sha256") != digest.hexdigest():
        raise ValueError("Manifest SHA-256 does not match the JSONL archive")
    return {"valid": True, "messages": count, "sha256": digest.hexdigest()}


def _verify_media(record: dict, root: Path) -> None:
    from .media import sha256_file

    download = record.get("media_download") or {}
    if download.get("status") != "downloaded":
        return
    path = Path(download.get("path", ""))
    if not path.is_absolute():
        path = root / path
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Media path escapes archive directory")
    if not path.is_file() or path.stat().st_size != download.get("actual_size"):
        raise ValueError(f"Media missing or size mismatch: {path.name}")
    if sha256_file(path) != download.get("sha256"):
        raise ValueError(f"Media hash mismatch: {path.name}")


def verify_artifact(path: Path) -> dict:
    from .media import sha256_file

    root = path if path.is_dir() else path.parent
    manifest_path = path / "manifest.json" if path.is_dir() else Path(f"{path}.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not manifest.get("files"):
        raise ValueError("Manifest has no output files")
    for item in manifest["files"]:
        file = (root / item["path"]).resolve()
        if not file.is_relative_to(root.resolve()):
            raise ValueError("Manifest path escapes archive")
        if not file.is_file() or file.stat().st_size != item["size"]:
            raise ValueError(f"Missing file or size mismatch: {file.name}")
        if sha256_file(file) != item["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {file.name}")
    if manifest.get("media_index"):
        index = (root / manifest["media_index"]).resolve()
        if not index.is_relative_to(root.resolve()):
            raise ValueError("Media index escapes archive")
        with index.open(encoding="utf-8") as handle:
            for line in handle:
                _verify_media({"media_download": json.loads(line)}, root)
    if path.suffix == ".zst":
        from .formats import records

        count = 0
        previous = {}
        for record in records(path):
            key = (record["account_id"], record["peer_id"])
            identity = int(record["message_id"])
            if identity <= previous.get(key, 0):
                raise ValueError("Compressed JSONL has duplicate or unordered IDs")
            previous[key] = identity
            _verify_media(record, root)
            count += 1
        if count != manifest["message_count"]:
            raise ValueError("Compressed JSONL count mismatch")
    return {"valid": True, "messages": manifest["message_count"], "files": len(manifest["files"])}
