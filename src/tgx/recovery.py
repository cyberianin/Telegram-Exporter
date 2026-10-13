from __future__ import annotations

from .models import HistoryPage
from .transport import TelegramTransport


class RecoveryEngine:
    def __init__(self, transport: TelegramTransport):
        self.transport = transport

    async def recover(
        self, peer_id: int, cursor: int, limit: int, known_ids: list[int] | None = None
    ) -> HistoryPage:
        found = {}
        if hasattr(self.transport, "compatibility_page"):
            page = await self.transport.compatibility_page(peer_id, max_id=cursor, limit=limit)
            found.update((m.message_id, m) for m in page.messages if 0 < m.message_id < cursor)
        if known_ids and hasattr(self.transport, "get_messages"):
            records = await self.transport.get_messages(peer_id, known_ids[:100])
            found.update((m.message_id, m) for m in records if 0 < m.message_id < cursor)
        messages = sorted(found.values(), key=lambda m: m.message_id, reverse=True)
        return HistoryPage(messages, bool(messages))


class NoForwardAwareScanner:
    """Protection is a diagnostic property, never a reason to suppress history reads."""

    def __init__(self, engine):
        self.engine = engine

    async def inspect(self, peer: str | int) -> dict:
        result = await self.engine.inspect(peer)
        result["desktop_export_limitation"] = "not_tested"
        result["text_access"] = "readable" if result["history_readable"] else "not_confirmed"
        result["media_access"] = result["media_probe_status"]
        return result
