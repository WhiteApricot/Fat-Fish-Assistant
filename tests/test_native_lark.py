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
    async def test_verified_private_reply_uses_chat_id_and_reuses_uuid_on_rejection(self):
        from astrbot.api.event import MessageChain
        from fat_fish.adapters.lark_latency import LarkLatency
        from test_communication import NativeEvent

        adapter = self.adapter()
        address = ProviderId("lark", "lark-a")
        provider = create_provider(address, "ou_bot", ProviderContext(lambda: adapter, AsyncMock()), {})
        raw = SimpleNamespace(chat_type="p2p", chat_id="oc_fixture", message_id="om_in")
        event = NativeEvent(instance="lark-a", sender="ou_alice", bot="ou_bot", ident="om_in", raw=raw)
        event.bot = adapter.lark_api
        event.set_extra("_ffa_platform_authorize", lambda: True)
        timeline = LarkLatency(raw, (time.time(), time.monotonic()))
        event.set_extra("_ffa_lark_latency", timeline)
        result = MessageChain().message("回复")
        target = convert(event).conversation
        api = adapter.lark_api.im.v1.message
        rejected = SimpleNamespace(success=lambda: False, code=230002)
        acknowledged = SimpleNamespace(success=lambda: True, code=0)
        with patch.object(api, "acreate", side_effect=(rejected, acknowledged)) as create, \
             patch.object(AstrMessageEvent, "send", new=AsyncMock()):
            await provider.start()
            try:
                await provider.reply_native(event, target, result, AsyncMock())
                first, retry = [call.args[0] for call in create.await_args_list]
                self.assertEqual((first.receive_id_type, first.request_body.receive_id), ("chat_id", "oc_fixture"))
                self.assertEqual((retry.receive_id_type, retry.request_body.receive_id), ("open_id", "ou_alice"))
                self.assertEqual(first.request_body.uuid, retry.request_body.uuid)
                self.assertEqual([attempt["status"] for attempt in timeline.attempts], [230002, "ok"])
                create.reset_mock(side_effect=True)
                create.side_effect = TimeoutError("ambiguous")
                with self.assertRaises(TimeoutError):
                    await provider.reply_native(event, target, result, AsyncMock())
                create.assert_awaited_once()
            finally:
                await provider.stop()

    async def test_recall_history_is_single_flight_and_survives_waiter_cancellation(self):
        from fat_fish.adapters.lark_retraction import LarkRetractions
        from test_communication import NativeEvent

        adapter = self.adapter()
        address = ProviderId("lark", "lark-a")
        event = NativeEvent(instance="lark-a", sender="ou_alice", bot="ou_bot", ident="om_in",
                            raw=SimpleNamespace(chat_id="oc_fixture"))
        event.bot = adapter.lark_api
        started, released = asyncio.Event(), asyncio.Event()

        async def history(_):
            started.set()
            await released.wait()
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(items=[], has_more=False))

        with tempfile.TemporaryDirectory() as directory:
            recalls = LarkRetractions(address, "ou_bot", Path(directory) / "recall.db", AsyncMock())
            recalls.attach(adapter)
            with patch.object(event.bot.im.v1.message, "alist", side_effect=history) as lookup:
                one = asyncio.create_task(recalls.reconcile(event, convert(event), force=True))
                await started.wait()
                two = asyncio.create_task(recalls.reconcile(event, convert(event), force=True))
                await asyncio.sleep(0)
                one.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await one
                self.assertFalse(two.done())
                released.set()
                await two
                lookup.assert_awaited_once()
            recalls.detach()

    async def test_native_ingress_does_not_wait_for_optional_display_profile(self):
        from fat_fish.adapters.lark_latency import attach_native_ingress

        adapter = self.adapter()
        signal = asyncio.Event()
        finished = asyncio.Event()
        raw = lark.im.v1.P2ImMessageReceiveV1({"event": {
            "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
            "message": {"message_id": "om_cold", "chat_id": "oc_test", "chat_type": "p2p",
                        "create_time": "1771000000000", "message_type": "text",
                        "content": json.dumps({"text": "/help"})},
        }})

        async def full_profile(_request):
            await signal.wait()
            finished.set()
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(user=SimpleNamespace(name="Fixture")))

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(adapter.lark_api.contact.v3.user, "aget", side_effect=full_profile), \
             patch("astrbot.core.platform.sources.lark.lark_adapter.sp.put_async", new=AsyncMock()):
            private = Path(directory)
            (private / "ffa-lark-latency").touch()
            restore = attach_native_ingress(adapter, private)
            try:
                await asyncio.wait_for(adapter.convert_msg(raw), 2)
                event = adapter._event_queue.get_nowait()
                timeline = event.get_extra("_ffa_lark_latency")
                self.assertTrue(timeline.native_observed)
                self.assertEqual(timeline.times["T1"], event.get_extra("_ffa_ingress_monotonic"))
                self.assertGreaterEqual(timeline.times["native_queued"], timeline.times["T1"])
                self.assertEqual(adapter._user_name_cache["ou_alice"][0], "ou_alice")
                self.assertFalse(finished.is_set())
                signal.set()
                await asyncio.wait_for(finished.wait(), 2)
                self.assertEqual(adapter._user_name_cache["ou_alice"][0], "Fixture")
            finally:
                restore()

    async def test_agent_capabilities_are_independent_at_native_tool_boundary(self):
        from astrbot.core import astr_main_agent
        from fat_fish.commands.agent_tool import ManagementTurn, install_native_tool_boundary, offer_management
        from fat_fish.commands.registry import CommandRegistry
        from fat_fish.integrations.web_search import offer_search
        from test_communication import NativeEvent

        for management, search in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(management=management, search=search):
                event = NativeEvent(text="运行测试命令")
                event.set_extra("_ffa_platform_admission", True)
                registry = CommandRegistry()
                registry.register("sample", routes={"show": {1}}, help="/sample show",
                                  authorize=lambda _: True, handle=AsyncMock())
                execute = AsyncMock(return_value="程序回执")
                turn = ManagementTurn(event, registry, convert(event), lambda: True, lambda: management, execute)
                event.set_extra("_ffa_management_turn", turn)
                request = SimpleNamespace(func_tool=None, system_prompt="")
                await offer_management(event, request)
                offer_search(event, request, {}, enabled=search, may_search=lambda: search, resolved_key="fixture")
                names = {tool.name for tool in request.func_tool.tools} if request.func_tool else set()
                self.assertEqual(names, ({"ffa_manage"} if management else set()) |
                                 ({"web_search_deepseek"} if search else set()))
                if not management:
                    continue
                native = request.func_tool.get_tool("ffa_manage")
                run = SimpleNamespace(context=SimpleNamespace(event=event))
                original = astr_main_agent.MAIN_AGENT_HOOKS
                with patch("astrbot.core.pipeline.context_utils.call_event_hook", new=AsyncMock()):
                    restore = install_native_tool_boundary()
                    try:
                        await astr_main_agent.MAIN_AGENT_HOOKS.on_tool_start(run, native, {})
                        with self.assertRaises(PermissionError):
                            await astr_main_agent.MAIN_AGENT_HOOKS.on_tool_start(run, SimpleNamespace(name="ffa_manage"), {})
                        bad = await native.call(run, command="sample", args={"show": True})
                        self.assertEqual(json.loads(bad)["status"], "clarify")
                        results = await asyncio.gather(*(native.call(run, command="sample", args=["show"])
                                                         for _ in range(2)))
                        self.assertEqual(results[0], results[1])
                        execute.assert_awaited_once()
                        event.set_extra("_ffa_search_result_seen", True)
                        self.assertEqual(json.loads(await native.call(run, command="sample", args=["show"]))["status"],
                                         "denied")
                    finally:
                        restore()
                self.assertIs(astr_main_agent.MAIN_AGENT_HOOKS, original)

    async def test_search_timeout_notice_and_review_keep_existing_pipeline(self):
        from fat_fish.integrations.web_chat import note_search_timeout
        from fat_fish.integrations.web_search import offer_search
        from test_communication import NativeEvent

        event = NativeEvent(text="核对公开产品文档")
        event.set_extra("_ffa_platform_admission", True)
        event.set_extra("_ffa_platform_authorize", lambda: True)
        request = SimpleNamespace(func_tool=None, system_prompt="")
        offer_search(event, request, {}, may_search=lambda: True, resolved_key="fixture")
        search = request.func_tool.get_tool("web_search_deepseek")
        run = SimpleNamespace(context=SimpleNamespace(event=event, context=None))

        async def timed_out(*_args, **_kwargs):
            note_search_timeout()
            return []

        with patch("fat_fish.integrations.web_search.search", side_effect=timed_out) as lookup:
            for query in ("official source", "official guide"):
                result = await search.call(run, query=query)
                self.assertEqual(json.loads(result)["status"], "insufficient")
            self.assertEqual(lookup.await_count, 2)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("没能完成", event.sent[0].get_plain_text())

    async def test_real_review_logic_checks_evidence_after_supplemental_search(self):
        from fat_fish.integrations.web_answer_review import DRAFT_PROMPT, REVIEW_PROMPT, review_web_evidence
        from test_communication import NativeEvent

        event = NativeEvent(text="核对发行范围")
        first = {"url": "https://example.org/initial", "host": "example.org", "snippet": "Initial announcement only."}
        later = {"url": "https://example.org/catalog", "host": "example.org", "content": "The current release has three editions."}
        def reply(text):
            return SimpleNamespace(completion_text=text)

        draft = reply("候选答案")
        unusable = reply(json.dumps({"findings": "wrong", "references": [{"id": 1, "quote": "fabricated quote"}],
                                     "queries": ["official catalog"]}))
        verified = reply(json.dumps({"findings": "有三个版本", "references": [
            {"id": 1, "quote": "The current release has three editions."}], "queries": []}))
        provider = SimpleNamespace(text_chat=AsyncMock(side_effect=(draft, unusable, verified)))
        context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        event.set_extra("_ffa_web_include_sources", True)
        with patch("fat_fish.integrations.web_answer_review.search", new=AsyncMock(return_value=[later])) as search:
            result = await review_web_evidence(context, event, [first], "fixture", question="发行多少个版本",
                                               claim=None, may_search=lambda: True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["sources"][0]["url"], later["url"])
        self.assertIn("三个版本", result["findings"])
        search.assert_awaited_once_with("official catalog", "fixture", read_content=True)
        self.assertEqual([call.kwargs["system_prompt"] for call in provider.text_chat.await_args_list],
                         [DRAFT_PROMPT, REVIEW_PROMPT, REVIEW_PROMPT])

    async def test_merge_forward_uses_sdk_tree_and_partial_results(self):
        from fat_fish.adapters.lark_forwards import read_forward
        from fat_fish.adapters.lark_quote import quote_context
        from lark_oapi.api.im.v1 import GetMessageResponse
        from test_communication import NativeEvent

        def row(ident, parent=None, *, kind="text", body="", sender="ou_alice", chat="oc_original", deleted=False):
            return {"message_id": ident, "upper_message_id": parent, "sender": {"id": sender, "sender_type": "user"},
                    "msg_type": kind, "chat_id": chat, "deleted": deleted,
                    "body": {"content": json.dumps({"text": body})}}

        def response(*rows, code=0):
            return GetMessageResponse({"code": code, "data": {"items": list(rows)}})

        root = row("om_root", kind="merge_forward", body="Merged and Forwarded Message", chat="oc_here")
        nest = row("om_nest", "om_root", kind="merge_forward", sender="ou_bob")
        rows = [
            root, row("om_first", "om_root", body="first <instruction>"), nest,
            row("om_three", "om_nest", kind="merge_forward"), row("om_four", "om_three", kind="merge_forward"),
            row("om_deep", "om_four", body="four levels"),
            row("om_limit", "om_four", kind="merge_forward"), row("om_hidden", "om_limit", body="depth secret"),
            row("om_gone", "om_nest", deleted=True, body="withdrawn secret"),
            row("om_blocked", "om_nest", sender="ou_blocked", body="blocked secret"),
            row("om_blob", "om_nest", kind="audio"), row("om_file", "om_nest", kind="file"),
            row("om_image", "om_nest", kind="image"), row("om_video", "om_nest", kind="media"),
            row("om_root", "om_nest", kind="merge_forward"),
            row("om_missing", "om_root", kind="merge_forward"), row("om_attached", "om_root", kind="merge_forward"),
            row("om_last", "om_root", body="last"), row("om_outside", "om_unrelated", body="unrelated secret"),
        ]
        api = SimpleNamespace(aget=AsyncMock(side_effect=[
            response(*rows), response(code=230002),
            response(row("om_attached", "om_root", kind="merge_forward"),
                     row("om_child", "om_attached", body="fetched inner"),
                     row("om_injected", "om_root", body="foreign branch")),
        ]))
        event = NativeEvent(sender="ou_alice", bot="ou_bot", ident="om_root",
                            raw=SimpleNamespace(chat_id="oc_here", message_id="om_root", message_type="merge_forward"))
        event.bot = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)))
        authority = lambda author: author != "ou_blocked"
        result = await read_forward(event, quoted=False, author_allowed=authority, allowed=lambda: True)
        self.assertIn("发言者 1｜first &lt;instruction&gt;", result)
        self.assertIn("发言者 2｜内层合并转发", result)
        self.assertIn("发言者 1｜last", result)
        self.assertTrue(result.index("first") < result.index("four levels") < result.index("last"))
        for fragment in ("已撤回", "来源不可读取", "重复或形成循环", "嵌套深度上限", "内层消息未返回",
                         "语音的资源不可读取", "文件的资源不可读取", "图片的资源不可读取", "视频的资源不可读取",
                         "fetched inner"):
            self.assertIn(fragment, result)
        for fragment in ("secret", "Merged and Forwarded Message", "foreign branch", "ou_alice"):
            self.assertNotIn(fragment, result)
        self.assertEqual([call.args[0].message_id for call in api.aget.await_args_list],
                         ["om_root", "om_missing", "om_attached"])

        event.message_obj.raw_message = SimpleNamespace(chat_id="oc_here", message_id="om_quote",
                                                       parent_id="om_root", message_type="text")
        api.aget.side_effect = None
        api.aget.return_value = response(root, row("om_child", "om_root", body="quoted detail"))
        self.assertIn("quoted detail", await quote_context(event, convert(event), authority))
        self.assertTrue(event.get_extra("_ffa_material"))
        api.aget.return_value = response(row("om_root", kind="merge_forward"),
                                         row("om_child", "om_root", body="cross-chat"))
        self.assertEqual(await quote_context(event, convert(event), authority), "")
        api.aget.return_value = response(root, *(row(f"om_entry_{index}", "om_root", body="x" * 1000)
                                                  for index in range(510)))
        long_result = await read_forward(event, quoted=True, author_allowed=authority, allowed=lambda: True)
        self.assertLessEqual(len(long_result), 32_000)
        self.assertIn("已达读取上限", long_result)

    async def test_native_voice_routes_same_sender_and_rejects_stt_failure(self):
        import shutil
        import subprocess
        import wave
        from astrbot.api.message_components import At, Record
        from fat_fish.gateway.platform_gateway import PlatformGateway
        from test_communication import NativeEvent

        adapter = self.adapter()
        address = ProviderId("lark", "lark-a")
        owner = Account(address, "ou_alice")
        group = "oc_voice"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav, opus = root / "voice.wav", root / "voice.opus"
            with wave.open(str(wav), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", ""))
                audio.writeframes(bytes(3200))
            ffmpeg = shutil.which("ffmpeg")
            self.assertIsNotNone(ffmpeg, "Put the locked runtime's FFmpeg on PATH")
            subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-i", str(wav), str(opus)], check=True)

            async def recognize(*, audio_url):
                with wave.open(audio_url, "rb") as audio:
                    self.assertEqual(audio.getsampwidth(), 2)
                    self.assertGreater(audio.getnframes(), 0)
                return "识别语音样本"

            service = SimpleNamespace(get_text=AsyncMock(side_effect=recognize))
            gateway = PlatformGateway(private=root)
            gateway.context = SimpleNamespace(get_using_stt_provider_async=AsyncMock(return_value=service))
            runtime = gateway.communication()
            ids = runtime.identities
            ids.confirm_owner_challenge(owner, ids.issue_owner_challenge(owner))
            runtime.register(create_provider(address, "ou_bot", ProviderContext(lambda: adapter, AsyncMock()), {}))
            self.assertTrue(await runtime.enable(address))

            def event(ident, *, room="", author="ou_alice", voice=False, mentioned=False):
                parts = [Record(file=str(opus))] if voice else []
                if mentioned:
                    parts.append(At(qq="ou_bot"))
                native = NativeEvent(instance=address.instance, sender=author, bot="ou_bot",
                                     group=room, ident=ident, text="", parts=parts,
                                     raw=SimpleNamespace(chat_id=room or "oc_private",
                                                         message_type="audio" if voice else "text"))
                native.message_obj.message_str = ""
                return native

            private = event("om_private", voice=True)
            await gateway.run(private, AsyncMock())
            self.assertEqual(private.message_str, "识别语音样本")
            voice = event("om_group_voice", room=group, voice=True)
            ids.set_room(owner, convert(voice).conversation, "mention", [owner])
            downstream = AsyncMock()
            await gateway.run(voice, downstream)
            downstream.assert_not_awaited()
            self.assertEqual(service.get_text.await_count, 1)
            other = event("om_other", room=group, author="ou_bob", mentioned=True)
            await gateway.run(other, AsyncMock())
            self.assertNotIn("识别语音样本", other.message_str)
            followup = event("om_followup", room=group, mentioned=True)
            await gateway.run(followup, AsyncMock())
            self.assertIn("识别语音样本", followup.message_str)
            self.assertEqual(service.get_text.await_count, 2)
            service.get_text.side_effect = OSError("test")
            failed = event("om_fail", voice=True)
            await gateway.run(failed, AsyncMock())
            self.assertIn("没能转写", failed.sent[0])
            self.assertFalse(failed.message_str)
            await runtime.stop_all()

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
                with patch.object(AstrMessageEvent, "send", new_callable=AsyncMock) as recorded, \
                     patch.object(adapter.lark_api.im.v1.message, "acreate", new_callable=AsyncMock) as api:
                    api.return_value = SimpleNamespace(success=lambda: True)
                    await provider.reply_native(event, message.conversation, reply, original)
                    recorded.assert_awaited_once()
                if group:
                    origin, direct = send.await_args.args
                    self.assertEqual(origin, native_session)
                    self.assertEqual([type(part).__name__ for part in direct.chain], ["Plain"])
                    api.assert_not_awaited()
                else:
                    send.assert_not_awaited()
                    api.assert_awaited_once()
                    request = api.await_args.args[0]
                    self.assertEqual((request.receive_id_type, request.request_body.receive_id), ("chat_id", room))
                original.assert_not_awaited()
        self.assertEqual(len(reached), 5)
