from __future__ import annotations

import csv
import html
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from .media import sha256_file
from .rendering import author, desktop_record, forwarded, rich_text


def records(path: Path) -> Iterator[dict]:
    if path.suffix == ".zst":
        import io

        import zstandard

        with path.open("rb") as source:
            with zstandard.ZstdDecompressor().stream_reader(source) as reader:
                with io.TextIOWrapper(reader, encoding="utf-8") as text:
                    for line in text:
                        yield json.loads(line)
    else:
        with path.open(encoding="utf-8") as source:
            for line in source:
                yield json.loads(line)


def _safe_csv(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def convert_archive(source: Path, output: Path, format: str, *, page_size: int = 1000) -> dict:
    from .archive import verify_jsonl

    verify_jsonl(source)
    manifest = json.loads(Path(f"{source}.manifest.json").read_text(encoding="utf-8"))
    import shutil

    def portable_records():
        for record in records(source):
            download = record.get("media_download") or {}
            if download.get("status") == "downloaded" and download.get("path"):
                media = Path(download["path"])
                if not media.is_absolute():
                    media = source.parent / media
                base = output if format == "html" else output.parent
                destination = base / "media" / media.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if media.resolve() != destination.resolve():
                    shutil.copyfile(media, destination)
                download["path"] = str(destination.resolve())
            yield record

    result = write_format(portable_records(), output, format, manifest, page_size=page_size)
    return result


def write_format(
    items: Iterable[dict], output: Path, format: str, metadata: dict, *, page_size: int = 1000
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    if format not in ("jsonl", "json", "txt", "csv", "sqlite", "html", "jsonl.zst"):
        raise ValueError(f"Unsupported format: {format}")
    root = output if format == "html" else output.parent
    root.mkdir(parents=True, exist_ok=True)
    media_index = root / (
        "media-index.jsonl" if format == "html" else output.name + ".media-index.jsonl"
    )
    media_handle = media_index.open("w", encoding="utf-8")
    original_items = items

    def indexed_items():
        try:
            for record in original_items:
                if record.get("sender_id") == metadata.get("peer_id") and not record.get(
                    "sender_name"
                ):
                    record["sender_name"] = metadata.get("title")
                download = record.get("media_download") or {}
                if download.get("status") == "downloaded" and download.get("path"):
                    path = Path(download["path"])
                    if path.is_absolute():
                        download["path"] = os.path.relpath(path, root).replace("\\", "/")
                    media_handle.write(json.dumps(download, ensure_ascii=False) + "\n")
                yield record
        finally:
            media_handle.close()

    items = indexed_items()
    temporary = output.with_name(output.name + ".tmp")
    count = 0
    files: list[Path] = []
    if format == "sqlite":
        temporary.unlink(missing_ok=True)
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript("""
                CREATE TABLE messages(account_id TEXT,peer_id INTEGER,message_id INTEGER,
                    date TEXT,text TEXT,body TEXT,PRIMARY KEY(account_id,peer_id,message_id));
                CREATE INDEX messages_date ON messages(date);
                CREATE TABLE metadata(body TEXT);
            """)
            connection.execute("INSERT INTO metadata VALUES (?)", (json.dumps(metadata),))
            for record in items:
                connection.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                    (
                        record["account_id"],
                        record["peer_id"],
                        record["message_id"],
                        record.get("date"),
                        record.get("text", ""),
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
                count += 1
                if count % 500 == 0:
                    connection.commit()
            connection.commit()
        finally:
            connection.close()
        temporary.replace(output)
        files.append(output)
    elif format == "jsonl.zst":
        import zstandard

        with temporary.open("wb") as handle:
            with zstandard.ZstdCompressor().stream_writer(handle, closefd=False) as writer:
                for record in items:
                    writer.write((json.dumps(record, ensure_ascii=False) + "\n").encode())
                    count += 1
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)
        files.append(output)
    elif format == "html":
        # Bounded pages; raw Telegram text is always escaped, including link markup.
        output.mkdir(parents=True, exist_ok=True)
        page: list[dict] = []
        number = 0
        link_database = output / ".message-links.sqlite.tmp"
        link_database.unlink(missing_ok=True)
        links = sqlite3.connect(link_database)
        links.execute("CREATE TABLE links(message_id INTEGER PRIMARY KEY,page INTEGER)")

        def flush() -> None:
            nonlocal number
            number += 1
            path = output / ("messages.html" if number == 1 else f"messages{number}.html")
            header = (
                '<!doctype html><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width">'
            )
            css = (
                "<style>body{font:14px Arial,sans-serif;background:#fff;color:#222;"
                "max-width:760px;margin:0 auto;padding:20px}nav{border-bottom:1px solid #eee;"
                "padding:20px 0}.message{padding:16px 0;display:flex;gap:14px}"
                ".userpic{width:42px;height:42px;border-radius:50%;background:#5799cf;"
                "color:white;display:grid;place-items:center;flex-shrink:0;font-weight:bold}"
                ".body{flex:1;min-width:0}.from_name{color:#3387bd;font-weight:bold}"
                ".text{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0}"
                ".date{float:right}.details{color:#777;font-size:12px}.forwarded{"
                "border-left:2px solid #8db7ce;padding-left:12px;margin:8px 0}"
                ".reply_to{margin:6px 0}a{color:#3387bd;text-decoration:none}"
                "a:hover{text-decoration:underline}.message:target{background:#fff5d6}"
                ".album{border-left:2px solid #dceaf3;padding-left:12px}"
                "@media(max-width:500px){body{padding:12px}.userpic{width:32px;height:32px}}"
                "</style>"
            )
            text = (
                header
                + css
                + '<nav><a href="index.html">'
                + html.escape(str(metadata.get("title", "Архив Telegram")))
                + "</a></nav>"
            )
            for record in page:
                identity = int(record["message_id"])
                reply = record.get("reply_to_id")
                date = html.escape(str(record.get("date") or ""))
                sender = author(record, username=False)
                initials = "".join(word[0] for word in sender.split()[:2])
                album = " album" if record.get("grouped_id") else ""
                group = html.escape(str(record.get("grouped_id") or ""))
                text += (
                    f'<div class="message default clearfix{album}" id="message{identity}" '
                    f'data-grouped-id="{group}">'
                    f'<div class="userpic">{html.escape(initials)}</div><div class="body">'
                    f'<div class="date details" title="{date}">{date}</div>'
                    f'<div class="from_name">{html.escape(sender)}</div>'
                )
                origin = forwarded(record)
                if origin:
                    text += (
                        '<div class="forwarded"><div class="details">Переслано от</div>'
                        '<div class="from_name">' + html.escape(origin) + "</div></div>"
                    )
                if reply:
                    reply_peer = (record.get("reply") or {}).get("reply_to_peer_id")
                    if reply_peer:
                        text += (
                            '<div class="reply_to details">'
                            f"Ответ на #{int(reply)} в другом чате</div>"
                        )
                    else:
                        text += (
                            '<div class="reply_to details">Ответ на '
                            f'<a href="__reply_{int(reply)}__">сообщение #{int(reply)}</a></div>'
                        )
                text += f'<div class="text" dir="auto">{rich_text(record)}</div>'
                download = record.get("media_download") or {}
                if download.get("status") == "downloaded" and download.get("path"):
                    media_path = Path(download["path"])
                    if not media_path.is_absolute():
                        media_path = output / media_path
                    relative = os.path.relpath(media_path, output).replace("\\", "/")
                    from urllib.parse import quote

                    link = html.escape(quote(relative))
                    suffix = media_path.suffix.lower()
                    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                        text += (
                            f'<a href="{link}"><img loading="lazy" src="{link}" alt="Фото" '
                            'style="max-width:100%;max-height:600px"></a>'
                        )
                    elif suffix in (".mp4", ".webm", ".mov"):
                        text += (
                            f'<video controls preload="none" src="{link}" '
                            'style="max-width:100%"></video>'
                        )
                    elif suffix in (".ogg", ".mp3", ".m4a", ".wav", ".oga"):
                        text += f'<audio controls preload="none" src="{link}"></audio>'
                    text += f'<p><a href="{link}">Открыть файл</a></p>'
                text += "</div></div>"
            path.write_text(text, encoding="utf-8")
            files.append(path)
            page.clear()

        for record in items:
            links.execute("INSERT INTO links VALUES (?,?)", (record["message_id"], number + 1))
            page.append(record)
            count += 1
            if len(page) >= page_size:
                flush()
        if page:
            flush()
        import re

        def reply_link(match):
            identity = int(match.group(1))
            row = links.execute("SELECT page FROM links WHERE message_id=?", (identity,)).fetchone()
            if not row:
                return f"#missing-message{identity}"
            name = "messages.html" if row[0] == 1 else f"messages{row[0]}.html"
            return f"{name}#message{identity}"

        for path in files:
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"__reply_(\d+)__", reply_link, text)
            text = re.sub(
                r'<a href="#missing-message\d+">(сообщение #\d+)</a>',
                r"\1 (не вошло в выгрузку)",
                text,
            )
            path.write_text(text, encoding="utf-8")
        links.close()
        link_database.unlink()
        index = output / "index.html"
        index.write_text(
            '<!doctype html><meta charset="utf-8"><h1>'
            + html.escape(str(metadata.get("title", "Telegram archive")))
            + "</h1>"
            + "".join(
                f'<p><a href="{path.name}">Page {i}</a></p>' for i, path in enumerate(files, 1)
            ),
            encoding="utf-8",
        )
        files.append(index)
    else:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            csv_writer = None
            if format == "json":
                handle.write(
                    '{"name":'
                    + json.dumps(metadata.get("title", "Telegram"), ensure_ascii=False)
                    + ',"type":'
                    + json.dumps(
                        {
                            "channel": "private_channel",
                            "group": "private_group",
                            "user": "personal_chat",
                        }.get(metadata.get("kind"), "private_channel")
                    )
                    + ',"id":'
                    + str(abs(metadata.get("peer_id", 0)) % 10**12)
                    + ',"messages":['
                )
            elif format == "csv":
                csv_writer = csv.writer(handle)
                csv_writer.writerow(["message_id", "date", "sender_id", "text", "reply_to_id"])
            elif format not in ("jsonl", "txt"):
                raise ValueError(f"Unsupported format: {format}")
            for record in items:
                if format == "json":
                    if count:
                        handle.write(",")
                    handle.write(json.dumps(desktop_record(record), ensure_ascii=False))
                elif csv_writer:
                    csv_writer.writerow(
                        [
                            _safe_csv(record.get(field))
                            for field in ("message_id", "date", "sender_id", "text", "reply_to_id")
                        ]
                    )
                elif format == "txt":
                    reply = f" -> #{record['reply_to_id']}" if record.get("reply_to_id") else ""
                    origin = forwarded(record)
                    handle.write(
                        f"[{record.get('date') or ''}] #{record['message_id']}{reply}\n"
                        f"{author(record)}:\n"
                        + (f"Переслано от {origin}\n" if origin else "")
                        + f"{record.get('text', '')}\n"
                    )
                    media = record.get("media_download") or {}
                    if media.get("status") == "downloaded" and media.get("path"):
                        handle.write(f"[Файл: {media['path']}]\n")
                    handle.write("\n---\n\n")
                else:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
            if format == "json":
                handle.write("]}\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)
        files.append(output)
    files.append(media_index)
    manifest = {
        **metadata,
        "message_count": count,
        "output_format": format,
        "render_version": 2,
        "media_index": media_index.name,
        "files": [
            {
                "path": os.path.relpath(path, output if output.is_dir() else output.parent).replace(
                    "\\", "/"
                ),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
            for path in files
        ],
    }
    if output.is_file():
        manifest["sha256"] = sha256_file(output)
    manifest_path = output / "manifest.json" if output.is_dir() else Path(f"{output}.manifest.json")
    manifest_tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_tmp.replace(manifest_path)
    return manifest
