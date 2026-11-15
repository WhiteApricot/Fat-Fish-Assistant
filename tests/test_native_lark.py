"""Exercise the pinned Lark adapter without a live Feishu connection."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from fat_fish.adapters.astrbot_communication import create_provider, convert
from fat_fish.communication.model import Account, Capability, ChatKind, Conversation, Media, NotSubmitted, Outgoing, ProviderId
from fat_fish.communication.registry import ProviderContext

try:
    from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter, lark
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Reply
except ModuleNotFoundError as exc:
    if exc.name != "astrbot":
        raise
    LarkPlatformAdapter = None


@unittest.skipIf(LarkPlatformAdapter is None, "Requires pinned AstrBot test runtime")
class NativeLarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_web_search_uses_current_admission(self):
        from fat_fish.integrations.web_search import clean_reply, offer_search, session_search_key
        from test_communication import NativeEvent

        self.assertEqual(clean_reply("答复\n<｜DSML｜function_calls>\n<｜DSML｜invoke>参数</｜DSML｜invoke>\n</｜DSML｜function_calls>"), "答复")
        for platform in ("lark", "aiocqhttp"):
            event = NativeEvent(platform=platform, text="上网核对 Python 文档")
            event.set_extra("_ffa_platform_admission", True)
            request = SimpleNamespace(func_tool=None, system_prompt="")
            permission = Mock(return_value=True)
            provider = SimpleNamespace(provider_config={"api_base": "https://api.deepseek.com/v1"},
                                       get_current_key=Mock(return_value="fixture"))
            context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
            key = await session_search_key(context, event.unified_msg_origin, {})
            context.get_using_provider_async.assert_awaited_once_with(event.unified_msg_origin)
            offer_search(event, request, {}, may_search=permission, resolved_key=key)
            tool = request.func_tool.get_tool("web_search_deepseek")
            usage = SimpleNamespace(context=SimpleNamespace(event=event, context=None))
            rows = [{"title": "Docs", "url": "https://docs.python.org", "host": "docs.python.org", "snippet": "official"}]
            with patch("fat_fish.integrations.web_search.search", new=AsyncMock(return_value=rows)) as search, \
                 patch("fat_fish.integrations.web_answer_review.review_web_evidence",
                       new=AsyncMock(return_value={"status": "verified"})):
                result = await tool.call(usage, query="python docs")
                self.assertEqual(json.loads(result)["status"], "verified")
                search.assert_awaited_once_with("python docs", "fixture", read_content=False)
                permission.return_value = False
                await tool.call(usage, query="second query")
                search.assert_awaited_once()

    async def test_search_rejects_nonofficial_chat_credential_sources(self):
        from fat_fish.integrations.web_search import session_search_key

        provider = SimpleNamespace(provider_config={}, get_current_key=Mock(return_value="unrelated"))
        context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        for endpoint in ("http://api.deepseek.com/v1", "https://api.deepseek.com.example.org/v1",
                         "https://api.deepseek.com@proxy.example.org/v1", "https://api.deepseek.com:8443/v1"):
            provider.provider_config["api_base"] = endpoint
            key = await session_search_key(context, "lark:FriendMessage:fixture",
                                           {"websearch_deepseek_key": ["dedicated"]})
            self.assertEqual(key, "dedicated")
        provider.get_current_key.assert_not_called()

    def adapter(self):
        adapter = LarkPlatformAdapter({"id": "lark-a", "app_id": "fixture", "app_secret": "fixture"},
                                      {}, asyncio.Queue())
        adapter.bot_open_id = "ou_bot"
        return adapter

    async def test_due_reminders_after_restart_without_inbound_sessions(self):
        from astrbot.core.platform.message_session import MessageSesion
        from fat_fish.gateway.platform_gateway import PlatformGateway
        from fat_fish.reminders.dispatch import ReminderAccess, ReminderDispatch
        from fat_fish.reminders.reminders import ReminderStore
        from test_communication import NativeEvent

        adapter = self.adapter()
        address = ProviderId("lark", "lark-a")
        owner = Account(address, "ou_alice")
        personal = Conversation(address, "ou_bot", ChatKind.PRIVATE, owner.subject, owner.subject)
        group = Conversation(address, "ou_bot", ChatKind.GROUP, "oc_group", "oc_group")
        unavailable = Conversation(address, "ou_bot", ChatKind.GROUP, "oc_revoked", "oc_revoked")

        async def send_native(origin, chain):
            await adapter.send_by_session(MessageSesion.from_str(origin), chain)
            return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = PlatformGateway(private=root).communication()
            ids = first.identities
            ids.confirm_owner_challenge(owner, ids.issue_owner_challenge(owner))
            store = ReminderStore(root / "reminders.sqlite3")
            for room in (personal, group, unavailable):
                if room.kind is ChatKind.GROUP:
                    ids.set_room(owner, room, "mention")
                event = NativeEvent(instance=address.instance, sender=owner.subject,
                                    group=room.peer if room.kind is ChatKind.GROUP else "")
                response = store.command(event, ["add", "十秒之后", room.peer],
                                         ReminderAccess(first, room), structured=True)
                self.assertIn("已创建提醒", response)
            ids.set_room(owner, unavailable, None)
            interrupted = next(row for row in store.rows() if row["scope"] == group.storage_key)
            with closing(store.connect()) as db, db:
                db.execute("INSERT INTO deliveries VALUES (?, ?, 'sending', '', ?)",
                           (interrupted["id"], interrupted["due"], time.time()))

            runtime = PlatformGateway(private=root).communication()
            provider = create_provider(address, "ou_bot", ProviderContext(lambda: adapter, send_native), {})
            runtime.register(provider)
            adapter.bot_open_id = ""
            adapter._refresh_bot_info = AsyncMock()
            self.assertFalse(await runtime.enable(address))
            adapter._refresh_bot_info.assert_awaited_once()
            recovered = ReminderStore(store.path)
            dispatch = ReminderDispatch(recovered, runtime)
            due = time.time() + 20
            with patch.object(adapter.lark_api.im.v1.message, "acreate", new_callable=AsyncMock) as send, \
                 patch("astrbot.core.platform.sources.lark.lark_adapter.sp.get_async", new=AsyncMock(return_value=None)), \
                 patch("astrbot.core.platform.platform.Metric.upload", new_callable=AsyncMock):
                send.return_value = SimpleNamespace(success=lambda: True)
                await recovered.tick(dispatch.authorize, dispatch.send, now=due, ready=dispatch.ready,
                                     delivery_state=dispatch.delivery_state)
                send.assert_not_awaited()
                self.assertEqual(sum(row["state"] == "active" for row in recovered.rows()), 2)
                adapter.bot_open_id = "ou_bot"
                for _ in range(3):
                    await recovered.tick(dispatch.authorize, dispatch.send, now=due, ready=dispatch.ready,
                                         delivery_state=dispatch.delivery_state)
            endpoints = {(call.args[0].receive_id_type, call.args[0].request_body.receive_id)
                         for call in send.await_args_list}
            self.assertEqual(endpoints, {("open_id", "ou_alice"), ("chat_id", "oc_group")})
            self.assertEqual(send.await_count, 2)
            self.assertEqual(sorted(row["state"] for row in recovered.rows()), ["done", "done", "revoked"])
            self.assertEqual(provider.routes, {})

    async def test_outbound_media_uses_native_upload_and_rechecks_before_submission(self):
        adapter = self.adapter()
        address = ProviderId("lark", "lark-a")
        room = Conversation(address, "ou_bot", ChatKind.GROUP, "oc_team", "oc_team")
        provider = create_provider(address, room.bot, ProviderContext(lambda: adapter, AsyncMock()), {})
        self.assertTrue({Capability.MEDIA, Capability.IMAGE, Capability.FILE} <= provider.manifest.capabilities)
        upload = adapter.lark_api.im.v1
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(upload.image, "acreate", new_callable=AsyncMock) as image, \
             patch.object(upload.file, "acreate", new_callable=AsyncMock) as file, \
             patch.object(upload.message, "acreate", new_callable=AsyncMock) as send:
            path = Path(directory, "sample.png")
            path.write_bytes(b"attachment fixture")
            image.return_value = SimpleNamespace(success=lambda: True, data=SimpleNamespace(image_key="img_sample"))
            file.return_value = SimpleNamespace(success=lambda: True, data=SimpleNamespace(file_key="file_sample"))
            send.return_value = SimpleNamespace(success=lambda: True)
            outgoing = Outgoing("caption", media=(Media("image", str(path)),), mentions=("ou_alice",))
            await provider.send_checked(room, outgoing, lambda: True)
            self.assertEqual(send.await_args.args[0].request_body.receive_id, "oc_team")
            self.assertIn('"image_key": "img_sample"', send.await_args.args[0].request_body.content)
            send.reset_mock()
            await provider.send_checked(room, Outgoing("", (Media("file", str(path)),)), lambda: True)
            self.assertEqual(send.await_args.args[0].request_body.msg_type, "file")
            send.reset_mock()
            with self.assertRaises(NotSubmitted):
                await provider.send_checked(room, outgoing, lambda: False)
            image.return_value = SimpleNamespace(success=lambda: False, code=1, msg="fixture")
            with self.assertRaises(NotSubmitted):
                await provider.send_checked(room, outgoing, lambda: True)
            with self.assertRaises(NotSubmitted):
                await provider.send_checked(room, Outgoing("", (Media("video", str(path)),)), lambda: True)
            send.assert_not_awaited()

    async def test_cardkit_finalization_and_safe_fallback(self):
        adapter = self.adapter()
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from fat_fish.adapters.lark_communication import guard_stream

        fixture = AstrBotMessage()
        fixture.type = MessageType.FRIEND_MESSAGE
        fixture.self_id = "ou_bot"
        fixture.sender = MessageMember("ou_alice", "Alice")
        fixture.message_id = "om_current"
        fixture.message = MessageChain().message("hello").chain
        event = LarkMessageEvent("hello", fixture, adapter.meta(), "ou_alice", adapter.lark_api)
        cardkit = adapter.lark_api.cardkit.v1
        im = adapter.lark_api.im.v1
        success = SimpleNamespace(success=lambda: True)
        failed = SimpleNamespace(success=lambda: False, code=1, msg="fixture")

        async def chunks():
            yield MessageChain().message("hello")

        with patch.object(cardkit.card, "acreate", new_callable=AsyncMock) as create, \
             patch.object(cardkit.card_element, "acontent", new_callable=AsyncMock) as update, \
             patch.object(cardkit.card, "asettings", new_callable=AsyncMock) as close, \
             patch.object(im.message, "areply", new_callable=AsyncMock) as submit, \
             patch.object(event, "send", new_callable=AsyncMock) as fallback, \
             patch("astrbot.core.platform.sources.lark.lark_event.Metric.upload", new_callable=AsyncMock):
            create.return_value = SimpleNamespace(success=lambda: True, data=SimpleNamespace(card_id="card_1"))
            submit.return_value = update.return_value = close.return_value = success
            authorize = Mock()
            with guard_stream(event, authorize):
                await event.send_streaming(chunks())
            self.assertEqual(authorize.call_args_list[0].args, (False,))
            update.assert_awaited_once()
            self.assertEqual(close.await_args.args[0].request_body.sequence, 2)
            fallback.assert_not_awaited()

            close.return_value = failed
            with guard_stream(event, authorize), self.assertRaises(ConnectionError):
                await event.send_streaming(chunks())
            close.return_value = success
            update.return_value = failed
            with guard_stream(event, authorize), self.assertRaises(ConnectionError):
                await event.send_streaming(chunks())
            update.return_value = success
            submit.return_value = failed
            with guard_stream(event, authorize), self.assertRaises(ConnectionError):
                await event.send_streaming(chunks())
            fallback.assert_not_awaited()

            submit.return_value = success
            update.reset_mock()
            close.reset_mock()

            async def revoke_after_card(_):
                authorize.side_effect = asyncio.CancelledError
                return success

            submit.side_effect = revoke_after_card
            with guard_stream(event, authorize), self.assertRaises(asyncio.CancelledError):
                await event.send_streaming(chunks())
            update.assert_not_awaited()
            close.assert_not_awaited()

            authorize.side_effect = None
            create.return_value = failed
            with guard_stream(event, authorize):
                await event.send_streaming(chunks())
            fallback.assert_awaited_once()

    async def test_native_recall_dispatch_uses_original_author_after_restart(self):
        from fat_fish.adapters.lark_retraction import EVENT
        from test_communication import NativeEvent

        adapter = self.adapter()
        address = ProviderId("lark", "lark-a")
        native = NativeEvent(instance=address.instance, sender="ou_alice", ident="om_original")
        native.message_obj.raw_message = SimpleNamespace(chat_id="oc_private")
        message = convert(native)
        async def retract(received_address, notice):
            self.assertEqual(address, received_address)
            self.assertTrue(await provider.verify_retraction(notice))
            self.assertEqual(notice.actor, message.sender)
            self.assertEqual(notice.event_id, message.event_id)

        callback = AsyncMock(side_effect=retract)
        with tempfile.TemporaryDirectory() as directory:
            context = ProviderContext(lambda: adapter, AsyncMock(), private_dir=Path(directory), retract_native=callback)
            provider = create_provider(address, message.conversation.bot, context, {})
            adapter.bot_open_id = message.conversation.bot
            await provider.start()
            self.assertTrue(provider.track_inbound(native, message))
            await provider.stop()
            self.assertNotIn(EVENT, adapter.event_handler._processorMap)
            provider = create_provider(address, message.conversation.bot, context, {})
            await provider.start()
            processor = adapter.event_handler._processorMap[EVENT]
            for chat in ("oc_unrelated", "oc_private"):
                processor.do(processor.type()({"event": {"chat_id": chat, "message_id": "om_original"}}))
            await asyncio.gather(*provider.retractions.tasks)
            callback.assert_awaited_once()
            self.assertFalse(provider.track_inbound(native, message))
            self.assertFalse(await provider.verify_retraction(callback.await_args.args[1]))
            await provider.stop()

    async def test_memory_search_reconciles_a_missed_recall_from_native_history(self):
        from fat_fish.gateway.platform_gateway import PlatformGateway
        from test_communication import NativeEvent

        adapter = self.adapter()
        address = ProviderId("lark", "lark-a")
        account = Account(address, "ou_alice")
        chat = "oc_private"
        rows = [
            SimpleNamespace(chat_id=chat, message_id="om_recalled", deleted=False),
            SimpleNamespace(chat_id=chat, message_id="om_kept", deleted=False),
            SimpleNamespace(chat_id="oc_other", message_id="om_kept", deleted=True),
            SimpleNamespace(chat_id=chat, message_id="om_untracked", deleted=True),
        ]
        api = adapter.lark_api.im.v1.message

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = PlatformGateway(private=root)
            runtime = gateway.communication()
            ids = runtime.identities
            ids.confirm_owner_challenge(account, ids.issue_owner_challenge(account))
            context = ProviderContext(lambda: adapter, AsyncMock(), private_dir=root, retract_native=gateway.retract)
            provider = create_provider(address, "ou_bot", context, {})
            runtime.register(provider)
            self.assertTrue(await runtime.enable(address))

            async def dispatch(ident, text):
                event = NativeEvent(instance=address.instance, sender=account.subject, bot="ou_bot",
                                    ident=ident, text=text, raw=SimpleNamespace(chat_id=chat))
                event.bot = adapter.lark_api
                await gateway.run(event, AsyncMock())
                return event.sent[0]

            result = SimpleNamespace(success=lambda: True, data=SimpleNamespace(items=rows,
                                                                                 has_more=False, page_token=None))
            with patch.object(api, "alist", new=AsyncMock(return_value=result)) as history:
                await dispatch("om_recalled", "/memory remember recalled 钴蓝纸鹤放在台灯旁")
                await dispatch("om_kept", "/memory remember kept 钴蓝纸鹤旁边有一支笔")
                before = await dispatch("om_query", "/memory search 钴蓝纸鹤")
                self.assertIn("recalled", before)
                self.assertIn("kept", before)
                result.success = lambda: False
                rows[0].deleted = True
                unavailable = await dispatch("om_unavailable", "/memory search 钴蓝纸鹤")
                self.assertIn("recalled", unavailable)
                result.success = lambda: True
                after = await dispatch("om_recheck", "/memory search 钴蓝纸鹤")
                self.assertNotIn("recalled", after)
                self.assertIn("kept", after)
                self.assertEqual(history.await_count, 3)
            await runtime.stop_all()

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
