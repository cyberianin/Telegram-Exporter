from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, unquote, urlparse


@dataclass(frozen=True)
class PeerReference:
    kind: Literal["id", "username", "invite", "name"]
    value: str | int
    peer_type: Literal["channel", "user", "chat"] | None = None


def _integer(value: str) -> int:
    if not re.fullmatch(r"-?\d+", value):
        raise ValueError("Telegram peer ID must be numeric")
    return int(value)


def parse_peer(value: str | int) -> PeerReference:
    if isinstance(value, int):
        return PeerReference("id", value)
    value = value.strip()
    if not value:
        raise ValueError("Enter a Telegram dialog or link")
    if re.fullmatch(r"-?\d+", value):
        return PeerReference("id", int(value))
    if value.startswith("@"):
        return PeerReference("username", value[1:])
    if value in ("me", "self"):
        return PeerReference("username", value)
    if value.startswith("tg://"):
        url = urlparse(value)
        query = parse_qs(url.query)

        def first(name: str) -> str:
            return query.get(name, [""])[0]

        action = url.netloc.lower()
        if action == "resolve" and first("domain"):
            return PeerReference("username", first("domain"))
        if action == "join" and first("invite"):
            return PeerReference("invite", first("invite"))
        if action in ("chat", "user", "channel") and first("id"):
            kind = None if action == "chat" else action
            return PeerReference("id", _integer(first("id")), kind)
        if action == "privatepost" and first("channel"):
            return PeerReference("id", _integer(first("channel")), "channel")
        if action == "openmessage":
            for name, kind in (("user_id", "user"), ("chat_id", None), ("channel_id", "channel")):
                if first(name):
                    return PeerReference("id", _integer(first(name)), kind)
        raise ValueError("This tg:// link does not identify a supported Telegram dialog")
    if "://" in value or re.match(r"(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", value):
        url = urlparse(value if "://" in value else "https://" + value)
        if url.hostname not in (
            "t.me",
            "www.t.me",
            "telegram.me",
            "www.telegram.me",
            "telegram.dog",
        ):
            raise ValueError("Use a Telegram link, username or peer ID")
        parts = [unquote(part) for part in url.path.split("/") if part]
        if parts and parts[0] == "s":
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] == "c":
            return PeerReference("id", _integer(parts[1]), "channel")
        if len(parts) >= 2 and parts[0] == "joinchat":
            return PeerReference("invite", parts[1])
        if parts and parts[0].startswith("+"):
            return PeerReference("invite", parts[0][1:])
        if parts and re.fullmatch(r"[A-Za-z0-9_]+", parts[0]):
            return PeerReference("username", parts[0])
        raise ValueError("Telegram link does not contain a dialog")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return PeerReference("username", value)
    return PeerReference("name", value)
