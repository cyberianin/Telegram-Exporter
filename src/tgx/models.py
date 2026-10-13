from __future__ import annotations

from base64 import b64encode
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class MessageRecord:
    account_id: str
    peer_id: int
    message_id: int
    date: str | None
    text: str
    sender_id: int | None
    grouped_id: int | None
    reply_to_id: int | None
    views: int | None
    forwards: int | None
    entities: list[dict[str, Any]]
    media: dict[str, Any] | None
    raw_type: str
    post_author: str | None = None
    edit_date: str | None = None
    forward: dict[str, Any] | None = None
    reactions: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    reply: dict[str, Any] | None = None
    source: str = "mtproto"
    sender_name: str | None = None
    sender_username: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    messages: list[MessageRecord]
    has_more: bool


@dataclass(frozen=True, slots=True)
class DialogRecord:
    peer_id: int
    title: str
    username: str | None
    kind: str
    protected_content: bool


def isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return safe_value(to_dict())
    return {"type": type(value).__name__, "value": str(value)}
