import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import Message, MessageFwdHeader, PeerUser, User

from tgx.formats import write_format
from tgx.rendering import desktop_record, rich_text
from tgx.transport import TelethonTransport


class RenderingTests(unittest.TestCase):
    def record(self, identity, **extra):
        return {
            "account_id": "main",
            "peer_id": 42,
            "message_id": identity,
            "date": "2023-05-14T09:30:21+00:00",
            "text": "Пример",
            "sender_id": 1,
            "sender_name": "Имя",
            "sender_username": "Nickname",
            **extra,
        }

    def test_txt_author_reply_and_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.txt"
            write_format(
                [self.record(59, reply_to_id=58, forward={"from_name": "Источник"})],
                path,
                "txt",
                {},
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("[2023-05-14T09:30:21+00:00] #59 -> #58\n@Nickname:\n", text)
            self.assertIn("Переслано от Источник", text)

    def test_html_cross_page_and_missing_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat"
            write_format(
                [self.record(58), self.record(59, reply_to_id=58), self.record(60, reply_to_id=12)],
                path,
                "html",
                {},
                page_size=1,
            )
            text = (path / "messages2.html").read_text(encoding="utf-8")
            self.assertIn('href="messages.html#message58"', text)
            self.assertIn('class="from_name">Имя', text)
            missing = (path / "messages3.html").read_text(encoding="utf-8")
            self.assertIn("не вошло в выгрузку", missing)
            self.assertNotIn("__reply_", missing)

    def test_utf16_formatting_and_safe_links(self):
        record = self.record(
            58,
            text="😀bold <script>",
            entities=[
                {"type": "MessageEntityBold", "offset": 2, "length": 4},
                {
                    "type": "MessageEntityTextUrl",
                    "offset": 7,
                    "length": 8,
                    "url": "javascript:alert(1)",
                },
            ],
        )
        self.assertEqual(rich_text(record), "😀<strong>bold</strong> &lt;script&gt;")
        converted = desktop_record(record)
        self.assertEqual(converted["id"], 58)
        self.assertEqual(converted["from_id"], "user1")
        self.assertIn({"type": "bold", "text": "bold"}, converted["text_entities"])

    def test_transport_resolves_sender_and_hidden_forward_from_page(self):
        transport = TelethonTransport(SimpleNamespace(), "main")
        message = Message(
            id=58,
            peer_id=PeerUser(1),
            from_id=PeerUser(1),
            message="Пример",
            fwd_from=MessageFwdHeader(date=None, from_name="Скрытый автор"),
        )
        record = transport._normalize(
            message, 42, {1: User(id=1, first_name="Имя", username="Nickname")}
        )
        self.assertEqual(record.sender_name, "Имя")
        self.assertEqual(record.sender_username, "Nickname")
        self.assertEqual(record.forward["from_name"], "Скрытый автор")
