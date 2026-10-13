from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .models import DialogRecord, HistoryPage, MessageRecord, safe_value
from .resolver import parse_peer


class TelegramTransport(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_authorized(self) -> bool: ...

    async def dialogs(self) -> list[DialogRecord]: ...

    async def resolve_peer(self, value: str | int) -> DialogRecord: ...

    async def latest_message_id(self, peer_id: int) -> int: ...

    async def history_page(self, peer_id: int, *, max_id: int, limit: int) -> HistoryPage: ...

    async def compatibility_page(self, peer_id: int, *, max_id: int, limit: int) -> HistoryPage: ...

    async def get_messages(self, peer_id: int, ids: list[int]) -> list[MessageRecord]: ...

    async def download_media(self, peer_id: int, message_id: int, path: Path) -> int | None: ...


class TelethonTransport:
    def __init__(self, client: Any, account_id: str) -> None:
        self.client = client
        self.account_id = account_id
        self._peers: dict[int, Any] = {}

    async def connect(self) -> None:
        await self.client.connect()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def is_authorized(self) -> bool:
        return bool(await self.client.is_user_authorized())

    async def dialogs(self) -> list[DialogRecord]:
        result: list[DialogRecord] = []
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            peer_id = int(dialog.id)
            self._peers[peer_id] = entity
            kind = (
                "channel"
                if getattr(entity, "broadcast", False)
                else ("supergroup" if getattr(entity, "megagroup", False) else "chat")
            )
            result.append(
                DialogRecord(
                    peer_id=peer_id,
                    title=str(dialog.name),
                    username=getattr(entity, "username", None),
                    kind=kind,
                    protected_content=bool(getattr(entity, "noforwards", False)),
                )
            )
        return result

    async def resolve_peer(self, value: str | int) -> DialogRecord:
        reference = parse_peer(value)
        if reference.kind == "invite":
            from telethon.tl.functions.messages import CheckChatInviteRequest

            checked = await self.client(CheckChatInviteRequest(str(reference.value)))
            entity = getattr(checked, "chat", None)
            if entity is None:
                raise PermissionError("INVITE_NOT_JOINED: joining requires explicit consent")
            from telethon.utils import get_peer_id

            return await self.resolve_peer(int(get_peer_id(entity)))
        if reference.kind in ("id", "name"):
            from telethon.utils import resolve_id

            available = await self.dialogs()
            matches = []
            for item in available:
                if reference.kind == "name":
                    if item.title.casefold() == str(reference.value).casefold():
                        matches.append(item)
                    continue
                wanted = int(reference.value)
                raw_id, peer_type = resolve_id(item.peer_id)
                type_matches = (
                    reference.peer_type is None
                    or {"PeerChannel": "channel", "PeerUser": "user", "PeerChat": "chat"}.get(
                        peer_type.__name__
                    )
                    == reference.peer_type
                )
                if type_matches and (item.peer_id == wanted or (wanted > 0 and raw_id == wanted)):
                    matches.append(item)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError(
                    "Ambiguous dialog reference; select its canonical ID from the dialog list"
                )
            # The entity cache may retain an accessible peer absent from the current dialog list.
            if reference.kind == "id":
                try:
                    if reference.peer_type:
                        from telethon.tl.types import PeerChannel, PeerChat, PeerUser

                        peer_class = {"channel": PeerChannel, "chat": PeerChat, "user": PeerUser}[
                            reference.peer_type
                        ]
                        cached = await self.client.get_input_entity(
                            peer_class(resolve_id(int(reference.value))[0])
                        )
                    else:
                        cached = await self.client.get_input_entity(int(reference.value))
                    entity = await self.client.get_entity(cached)
                except (ValueError, TypeError) as error:
                    raise ValueError(
                        "Dialog was not resolved from this profile's dialogs or entity cache; "
                        "select it from the list or use its canonical peer ID"
                    ) from error
            else:
                raise ValueError("No dialog with that exact name; select it from the list")
        else:
            entity = await self.client.get_entity(str(reference.value))
        from telethon.utils import get_peer_id

        peer_id = int(get_peer_id(entity))
        self._peers[peer_id] = entity
        kind = (
            "channel"
            if getattr(entity, "broadcast", False)
            else ("supergroup" if getattr(entity, "megagroup", False) else "chat")
        )
        return DialogRecord(
            peer_id=peer_id,
            title=str(getattr(entity, "title", None) or getattr(entity, "first_name", "")),
            username=getattr(entity, "username", None),
            kind=kind,
            protected_content=bool(getattr(entity, "noforwards", False)),
        )

    def _entity(self, peer_id: int) -> Any:
        entity = self._peers.get(peer_id)
        if entity is None:
            raise ValueError(f"Peer {peer_id} has not been resolved")
        return entity

    async def latest_message_id(self, peer_id: int) -> int:
        from telethon.tl.functions.messages import GetHistoryRequest

        result = await self.client(
            GetHistoryRequest(
                peer=self._entity(peer_id),
                offset_id=0,
                offset_date=None,
                add_offset=0,
                limit=1,
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        return int(result.messages[0].id) if result.messages else 0

    async def history_page(self, peer_id: int, *, max_id: int, limit: int) -> HistoryPage:
        from telethon.tl.functions.messages import GetHistoryRequest

        entity = self._entity(peer_id)
        result = await self.client(
            GetHistoryRequest(
                peer=entity,
                offset_id=max_id,
                offset_date=None,
                add_offset=0,
                limit=limit,
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        from telethon.utils import get_peer_id

        people = {get_peer_id(item): item for item in [*result.users, *result.chats]}
        records = [self._normalize(message, peer_id, people) for message in result.messages]
        records.sort(key=lambda message: message.message_id, reverse=True)
        return HistoryPage(messages=records, has_more=len(records) == limit)

    def _normalize(self, message: Any, peer_id: int, people: dict | None = None) -> MessageRecord:
        from telethon.utils import get_peer_id

        people = people or {}
        sender_id = getattr(message, "sender_id", None)
        if sender_id is None and getattr(message, "from_id", None):
            sender_id = get_peer_id(message.from_id)
        if sender_id is None and getattr(message, "post", False):
            sender_id = peer_id
        sender = people.get(sender_id) or getattr(message, "sender", None)

        def name(entity):
            if entity is None:
                return None
            return (
                getattr(entity, "title", None)
                or " ".join(
                    part
                    for part in (
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    )
                    if part
                )
                or None
            )

        entities = []
        for item in getattr(message, "entities", None) or []:
            values = safe_value(item.to_dict())
            values["type"] = values.pop("_", type(item).__name__)
            entities.append(values)
        media = None
        raw_media = getattr(message, "media", None)
        if raw_media is not None:
            media = safe_value(raw_media.to_dict())
        reply = getattr(message, "reply_to", None)
        raw_forward = getattr(message, "fwd_from", None)
        forward = safe_value(raw_forward)
        if raw_forward and isinstance(forward, dict):
            origin = getattr(raw_forward, "from_id", None)
            origin_entity = people.get(get_peer_id(origin)) if origin else None
            if origin_entity is None:
                origin_entity = getattr(getattr(message, "forward", None), "sender", None)
            forward["from_name"] = getattr(raw_forward, "from_name", None) or name(origin_entity)
            forward["from_username"] = getattr(origin_entity, "username", None)
        return MessageRecord(
            account_id=self.account_id,
            peer_id=peer_id,
            message_id=int(message.id),
            date=message.date.isoformat() if getattr(message, "date", None) else None,
            text=str(getattr(message, "message", None) or ""),
            sender_id=sender_id,
            grouped_id=getattr(message, "grouped_id", None),
            reply_to_id=getattr(reply, "reply_to_msg_id", None),
            views=getattr(message, "views", None),
            forwards=getattr(message, "forwards", None),
            entities=entities,
            media=media,
            raw_type=type(message).__name__,
            post_author=getattr(message, "post_author", None),
            edit_date=(
                message.edit_date.isoformat() if getattr(message, "edit_date", None) else None
            ),
            forward=forward,
            reactions=safe_value(getattr(message, "reactions", None)),
            action=safe_value(getattr(message, "action", None)),
            reply=safe_value(reply),
            sender_name=name(sender),
            sender_username=getattr(sender, "username", None),
        )

    async def compatibility_page(self, peer_id: int, *, max_id: int, limit: int) -> HistoryPage:
        records = [
            self._normalize(item, peer_id)
            async for item in self.client.iter_messages(
                self._entity(peer_id), offset_id=max_id, limit=limit
            )
        ]
        return HistoryPage(records, len(records) == limit)

    async def get_messages(self, peer_id: int, ids: list[int]) -> list[MessageRecord]:
        result = await self.client.get_messages(self._entity(peer_id), ids=ids)
        return [self._normalize(item, peer_id) for item in result if item is not None]

    async def download_media(self, peer_id: int, message_id: int, path: Path) -> int | None:
        from telethon.errors import FileReferenceExpiredError

        for attempt in range(2):
            message = await self.client.get_messages(self._entity(peer_id), ids=message_id)
            if message is None or message.media is None:
                raise ValueError("Message has no accessible media descriptor")
            expected = getattr(getattr(message, "file", None), "size", None)
            try:
                # A file handle prevents the library from adding an extension to the temp path.
                with path.open("wb") as handle:
                    result = await self.client.download_media(message, file=handle)
                if result is None:
                    raise ValueError("Server returned no downloadable file")
                return expected
            except FileReferenceExpiredError:
                if attempt:
                    raise
        return None

    async def probe_media(self, peer_id: int, message_id: int) -> None:
        message = await self.client.get_messages(self._entity(peer_id), ids=message_id)
        if message is None or message.media is None:
            raise ValueError("Media descriptor unavailable")
        stream = self.client.iter_download(message.media, request_size=4096)
        try:
            chunk = await stream.__anext__()
            if not chunk:
                raise ValueError("Server returned an empty media sample")
        finally:
            await stream.close()
