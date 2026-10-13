from __future__ import annotations

import unittest
from types import SimpleNamespace

from tgx.resolver import parse_peer
from tgx.transport import TelethonTransport


class Parser(unittest.TestCase):
    def test_link_types_without_specific_peer_hardcoding(self):
        cases = {
            "421": ("id", 421, None),
            "-1000000000421": ("id", -1000000000421, None),
            "tg://chat?id=421": ("id", 421, None),
            "tg://user?id=421": ("id", 421, "user"),
            "tg://channel?id=421": ("id", 421, "channel"),
            "tg://privatepost?channel=421&post=99": ("id", 421, "channel"),
            "tg://openmessage?user_id=421&message_id=99": ("id", 421, "user"),
            "https://t.me/c/421/99": ("id", 421, "channel"),
            "@sample_channel": ("username", "sample_channel", None),
            "tg://resolve?domain=sample_channel&post=99": ("username", "sample_channel", None),
            "t.me/sample_channel/99": ("username", "sample_channel", None),
            "https://t.me/s/sample_channel/99": ("username", "sample_channel", None),
            "https://telegram.me/sample_channel": ("username", "sample_channel", None),
            "https://t.me/+invite_hash": ("invite", "invite_hash", None),
            "https://t.me/joinchat/invite_hash": ("invite", "invite_hash", None),
            "tg://join?invite=invite_hash": ("invite", "invite_hash", None),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                result = parse_peer(value)
                self.assertEqual((result.kind, result.value, result.peer_type), expected)

    def test_non_dialog_links_are_not_treated_as_join_or_peer(self):
        for value in (
            "https://example.com/channel",
            "tg://proxy?server=example.com",
            "tg://chat?id=bad",
        ):
            with self.assertRaises(ValueError):
                parse_peer(value)


class Resolver(unittest.IsolatedAsyncioTestCase):
    def transport(self, with_user=False):
        entity = SimpleNamespace(
            id=421,
            broadcast=True,
            megagroup=False,
            username=None,
            noforwards=True,
            access_hash=98765,
        )
        dialogs = [SimpleNamespace(id=-1000000000421, entity=entity, name="Private test")]
        if with_user:
            dialogs.append(SimpleNamespace(id=421, entity=SimpleNamespace(id=421), name="User"))

        class Client:
            async def iter_dialogs(self):
                for item in dialogs:
                    yield item

        return TelethonTransport(Client(), "main")

    async def test_positive_channel_id_uses_accessible_entity_hash(self):
        for reference in (421, "tg://chat?id=421", "https://t.me/c/421/99"):
            transport = self.transport()
            resolved = await transport.resolve_peer(reference)
            self.assertEqual(resolved.peer_id, -1000000000421)
            self.assertEqual(transport._entity(resolved.peer_id).access_hash, 98765)

    async def test_ambiguous_raw_id_requires_explicit_peer_type(self):
        transport = self.transport(with_user=True)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            await transport.resolve_peer(421)
        self.assertEqual((await transport.resolve_peer("tg://user?id=421")).peer_id, 421)
        self.assertEqual(
            (await transport.resolve_peer("tg://channel?id=421")).peer_id, -1000000000421
        )
