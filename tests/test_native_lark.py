"""Exercise the pinned Lark adapter without a live Feishu connection."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from fat_fish.adapters.astrbot_communication import create_provider, convert
from fat_fish.communication.model import ChatKind, Outgoing, ProviderId
from fat_fish.communication.registry import ProviderContext

try:
    from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter, lark
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
except ModuleNotFoundError as exc:
    if exc.name != "astrbot":
        raise
    LarkPlatformAdapter = None


@unittest.skipIf(LarkPlatformAdapter is None, "Requires pinned AstrBot test runtime")
class NativeLarkTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(len(reached), 5)
