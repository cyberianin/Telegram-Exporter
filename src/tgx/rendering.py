from __future__ import annotations

import html
from datetime import datetime
from urllib.parse import urlparse


def author(record: dict, *, username: bool = True) -> str:
    if username and record.get("sender_username"):
        return "@" + record["sender_username"]
    return (
        record.get("sender_name")
        or record.get("post_author")
        or (
            f"ID {record['sender_id']}"
            if record.get("sender_id") is not None
            else "Автор не указан"
        )
    )


def peer_label(value: dict | None) -> str | None:
    if not value:
        return None
    for kind in ("user", "channel", "chat"):
        if value.get(kind + "_id") is not None:
            return kind + str(value[kind + "_id"])
    return None


def forwarded(record: dict) -> str | None:
    value = record.get("forward")
    if not value:
        return None
    return (
        "@" + value["from_username"]
        if value.get("from_username")
        else value.get("from_name")
        or value.get("post_author")
        or peer_label(value.get("from_id"))
        or "Источник скрыт Telegram"
    )


def rich_text(record: dict) -> str:
    """Telegram entity offsets are UTF-16 code units, not Python character offsets."""
    text = record.get("text") or ""
    encoded = text.encode("utf-16-le")
    starts, ends = {}, {}
    tags = {
        "MessageEntityBold": ("<strong>", "</strong>"),
        "MessageEntityItalic": ("<em>", "</em>"),
        "MessageEntityCode": ("<code>", "</code>"),
        "MessageEntityPre": ("<pre>", "</pre>"),
        "MessageEntityUnderline": ("<u>", "</u>"),
        "MessageEntityStrike": ("<s>", "</s>"),
        "MessageEntityBlockquote": ("<blockquote>", "</blockquote>"),
    }
    for entity in record.get("entities") or []:
        start, length = entity.get("offset", 0), entity.get("length", 0)
        stop = start + length
        if start < 0 or length <= 0 or stop * 2 > len(encoded):
            continue
        kind = entity.get("type") or entity.get("_")
        tag = tags.get(kind)
        if kind in ("MessageEntityUrl", "MessageEntityTextUrl"):
            url = entity.get("url") or encoded[start * 2 : stop * 2].decode(
                "utf-16-le", errors="replace"
            )
            if urlparse(url).scheme.lower() in ("http", "https", "tg", "mailto"):
                tag = (f'<a href="{html.escape(url, quote=True)}">', "</a>")
        if tag:
            starts.setdefault(start, []).append((stop, tag[0]))
            ends.setdefault(stop, []).append((start, tag[1]))
    output = []
    offset = 0
    for char in text:
        output.extend(tag for _, tag in sorted(ends.get(offset, []), reverse=True))
        output.extend(tag for _, tag in sorted(starts.get(offset, []), reverse=True))
        output.append(html.escape(char))
        offset += len(char.encode("utf-16-le")) // 2
    output.extend(tag for _, tag in sorted(ends.get(offset, []), reverse=True))
    return "".join(output)


def desktop_record(record: dict) -> dict:
    result = {
        "id": record["message_id"],
        "type": "service" if record.get("action") else "message",
        "date": record.get("date"),
        "from": author(record, username=False),
        "text": record.get("text", ""),
    }
    date = record.get("date")
    if date:
        result["date_unixtime"] = str(int(datetime.fromisoformat(date).timestamp()))
    identity = record.get("sender_id")
    if identity is not None:
        result["from_id"] = (
            "channel" + str(-identity - 10**12)
            if identity < -(10**12)
            else "chat" + str(-identity)
            if identity < 0
            else "user" + str(identity)
        )
    if record.get("reply_to_id"):
        result["reply_to_message_id"] = record["reply_to_id"]
    if forwarded(record):
        result["forwarded_from"] = forwarded(record)
        origin = peer_label((record.get("forward") or {}).get("from_id"))
        if origin:
            result["forwarded_from_id"] = origin
    if record.get("post_author"):
        result["author"] = record["post_author"]
    if record.get("edit_date"):
        result["edited"] = record["edit_date"]
    if record.get("grouped_id"):
        result["grouped_id"] = str(record["grouped_id"])
    media = record.get("media_download") or {}
    if media.get("status") == "downloaded":
        photo = "photo" in (record.get("media") or {})
        result["photo" if photo else "file"] = media["path"]
        result["photo_file_size" if photo else "file_size"] = media.get("actual_size")
    text = record.get("text") or ""
    encoded = text.encode("utf-16-le")
    entities = []
    position = 0
    for entity in sorted(record.get("entities") or [], key=lambda value: value.get("offset", 0)):
        start = entity.get("offset", 0)
        stop = start + entity.get("length", 0)
        if start < position or stop * 2 > len(encoded) or stop <= start:
            continue
        if start > position:
            entities.append(
                {
                    "type": "plain",
                    "text": encoded[position * 2 : start * 2].decode("utf-16-le", errors="replace"),
                }
            )
        kind = (entity.get("type") or entity.get("_") or "").removeprefix("MessageEntity")
        names = {
            "TextUrl": "text_link",
            "Url": "link",
            "MentionName": "mention_name",
            "CustomEmoji": "custom_emoji",
            "BankCard": "bank_card",
        }
        item = {
            "type": names.get(kind, kind.lower()),
            "text": encoded[start * 2 : stop * 2].decode("utf-16-le", errors="replace"),
        }
        for source, target in (
            ("url", "href"),
            ("language", "language"),
            ("user_id", "user_id"),
            ("document_id", "document_id"),
        ):
            if entity.get(source) is not None:
                item[target] = entity[source]
        entities.append(item)
        position = stop
    if position * 2 < len(encoded):
        entities.append({"type": "plain", "text": encoded[position * 2 :].decode("utf-16-le")})
    result["text_entities"] = entities
    if any(item["type"] != "plain" for item in entities):
        result["text"] = [item["text"] if item["type"] == "plain" else item for item in entities]
    return result
