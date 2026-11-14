"""Exercise the pinned Lark adapter without a live Feishu connection."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from fat_fish.adapters.astrbot_communication import create_provider, convert
from fat_fish.communication.model import ChatKind, Outgoing, ProviderId
from fat_fish.communication.registry import ProviderContext

try:
    from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter, lark
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.api.message_components import Reply
except ModuleNotFoundError as exc:
    if exc.name != "astrbot":
        raise
    LarkPlatformAdapter = None


@unittest.skipIf(LarkPlatformAdapter is None, "Requires pinned AstrBot test runtime")
class NativeLarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_uses_parent_id_even_without_native_reply_component(self):
        from fat_fish.adapters.lark_quote import quote_context
        from test_communication import NativeEvent

        chat = "oc_team"
        def row(ident, second, text, *, chat_id=chat, sender="ou_alice"):
            return SimpleNamespace(message_id=ident, chat_id=chat_id, create_time=str(second * 1000), msg_type="text",
                                   sender=SimpleNamespace(id=sender, sender_type="user"),
                                   body=SimpleNamespace(content=json.dumps({"text": text})))

        def response(*rows):
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(items=rows))

        event = NativeEvent(group=chat, ident="om_current")
        event.message_obj.raw_message = SimpleNamespace(chat_id=chat, parent_id="om_target")
        event.message_obj.timestamp = 200
        parent = row("om_target", 100, "explicit reference")
        parent.parent_id = "om_ancestor"
        ancestor = row("om_ancestor", 70, "previous reference")
        ancestor.parent_id = "om_foreign_parent"
        foreign = row("om_foreign_parent", 60, "foreign ancestor", chat_id="oc_other")
        api = SimpleNamespace(aget=AsyncMock(side_effect=[response(parent), response(ancestor), response(foreign)]),
                              alist=AsyncMock(side_effect=[
            response(*(row(f"om_before_{i}", 80 + i, f"before-{i}") for i in range(8)),
                     row("om_blocked", 95, "blocked", sender="ou_blocked")),
            response(*(row(f"om_after_{i}", 110 + i, f"after-{i}") for i in range(8)),
                     row("om_foreign", 115, "other chat", chat_id="oc_other")),
        ]))
        event.bot = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)))
        message = convert(event)
        self.assertEqual(message.reply_to, "om_target")
        context = await quote_context(event, message, lambda author: author != "ou_blocked")
        self.assertIn("<explicit_quote>用户：explicit reference</explicit_quote>", context)
        self.assertIn("previous reference", context)
        self.assertIn("上游引用未完整取得", context)
        self.assertNotIn("foreign ancestor", context)
        self.assertIn("before-2", context)
        self.assertIn("after-5", context)
        self.assertNotIn("before-1", context)
        self.assertNotIn("after-6", context)
        self.assertNotIn("blocked", context)
        self.assertNotIn("other chat", context)
        api.aget.side_effect = None
        api.aget.return_value = response(row("om_target", 100, "foreign target", chat_id="oc_other"))
        self.assertEqual(await quote_context(event, message, lambda _: True), "")

    async def test_native_events_and_outgoing_routes(self):
        reached = set()
        for instance, chat_type, sender, room in (
            ("lark-a", "p2p", "ou_alice", "oc_private_a"),
            ("lark-a", "p2p", "ou_bob", "oc_private_b"),
            ("lark-a", "group", "ou_alice", "oc_group_a"),
            ("lark-a", "group", "ou_alice", "oc_group_b"),
            ("lark-b", "group", "ou_alice", "oc_group_a"),
        ):
            with self.subTest(instance=instance, room=room):
                queue = asyncio.Queue()
                adapter = LarkPlatformAdapter({"id": instance, "app_id": "fixture", "app_secret": "fixture"}, {}, queue)
                adapter.bot_open_id = "ou_bot"
                group = chat_type == "group"
                raw = lark.im.v1.P2ImMessageReceiveV1({"event": {
                    "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
                    "message": {"message_id": "om_sample", "chat_id": room, "chat_type": chat_type,
                                "create_time": "1771000000000", "message_type": "text",
                                "content": json.dumps({"text": "@_user_1 hello" if group else "hello"}),
                                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "bot"}] if group else []},
                }})
                with patch.object(adapter.lark_api.contact.v3.user, "aget", new=AsyncMock(return_value=None)), \
                     patch("astrbot.core.platform.sources.lark.lark_adapter.sp.put_async", new=AsyncMock()):
                    await adapter.convert_msg(raw)
                event = queue.get_nowait()
                message = convert(event)
                address = ProviderId("lark", instance)
                self.assertEqual(message.sender.subject, sender)
                self.assertEqual(message.sender.provider, address)
                self.assertEqual(message.message_id, "om_sample")
                self.assertEqual(message.timestamp, 1771000000)
                self.assertEqual(message.text, "hello")
                self.assertEqual(message.mentions, ("ou_bot",) if group else ())
                self.assertEqual(message.conversation.kind, ChatKind.GROUP if group else ChatKind.PRIVATE)
                self.assertEqual(message.conversation.peer, room if group else sender)
                self.assertEqual(message.conversation.thread, message.conversation.peer)

                send = AsyncMock(return_value=True)
                provider = create_provider(address, "ou_bot", ProviderContext(lambda: adapter, send), {})
                await provider.start()
                provider.observe(message.conversation, event.unified_msg_origin)
                await provider.send(message.conversation, Outgoing("first\nsecond", mentions=(sender,) if group else ()))
                origin, chain = send.await_args.args
                native_session = f'{instance}:{"GroupMessage" if group else "FriendMessage"}:{room if group else sender}'
                self.assertEqual(origin, native_session)
                reached.add(origin)
                parts = await LarkMessageEvent._convert_to_lark(chain, adapter.lark_api)
                self.assertEqual(parts[0][-1]["text"], "first\nsecond")
                if group:
                    self.assertEqual(parts[0][0]["user_id"], sender)
                send.reset_mock()
                original = AsyncMock()
                reply = event.plain_result("ordinary reply")
                reply.chain.insert(0, Reply(id="om_sample"))
                with patch.object(AstrMessageEvent, "send", new_callable=AsyncMock) as recorded:
                    await provider.reply_native(event, message.conversation, reply, original)
                    recorded.assert_awaited_once()
                origin, direct = send.await_args.args
                self.assertEqual(origin, native_session)
                self.assertEqual([type(part).__name__ for part in direct.chain], ["Plain"])
                original.assert_not_awaited()
        self.assertEqual(len(reached), 5)
