"""Offline contract tests against the pinned AstrBot media consumers."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fat_fish.adapters.astrbot_communication import convert
from fat_fish.adapters.group_media import GroupMedia
from fat_fish.gateway.media_ingress import prepare_media
from fat_fish.integrations.forwards import ForwardReader
from test_communication import NativeEvent


try:
    from astrbot.api.message_components import File, Image, Node, Plain, Record, Reply, Video
    from astrbot.core import astr_main_agent
    from astrbot.core.pipeline.preprocess_stage import stage as preprocess
except ModuleNotFoundError as exc:
    if exc.name != "astrbot":
        raise
    preprocess = None


@unittest.skipUnless(preprocess is not None, "Requires pinned AstrBot test runtime")
class NativeMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_or_unattributed_quote_is_rejected_before_preprocessing(self):
        for author in ("42", 0):
            event = NativeEvent(parts=[Reply(id="quote", sender_id=author, chain=[Plain("私密资料")])])
            with self.assertRaises(PermissionError):
                await prepare_media(event, lambda: True, author_allowed=lambda account: account != "42")

    async def test_qq_quote_checks_populated_native_reply_against_the_current_peer(self):
        from fat_fish.adapters.qq_quote import resolve_quote_thread

        reply = Reply(id="-42", sender_id="32", chain=[Plain("unverified native content")])
        raw = {"message_id": -42, "message_type": "private", "sender": {"user_id": 32},
               "message": [{"type": "text", "data": {"text": "verified content"}}]}
        event = NativeEvent(platform="aiocqhttp", parts=[reply])
        event.bot.call_action = AsyncMock(side_effect=[raw, {"messages": []}])
        quote, _ = await resolve_quote_thread(event, reply, author_allowed=lambda user: user == "32")
        self.assertIn("verified content", quote)
        self.assertNotIn("unverified native content", quote)
        self.assertEqual(event.bot.call_action.await_args_list[0].kwargs, {"message_id": -42})

        for response in (dict(raw, sender={"user_id": 33}), dict(raw, message_id=42), None):
            with self.subTest(response=response):
                event.bot.call_action = AsyncMock(return_value=response)
                self.assertEqual(await resolve_quote_thread(event, reply), ("", []))
                event.bot.call_action.assert_awaited_once_with("get_msg", message_id=-42)

    async def test_qq_quote_only_uses_matching_group_history(self):
        from fat_fish.adapters.qq_quote import resolve_quote_thread

        reply = Reply(id="-42")
        event = NativeEvent(platform="aiocqhttp", group="10", parts=[reply])

        def row(message_id, group, time, text):
            return {"message_id": message_id, "message_type": "group", "group_id": group, "time": time,
                    "sender": {"user_id": 32}, "message": [{"type": "text", "data": {"text": text}}]}

        quoted = row(-42, 10, 20, "target")
        history = {"messages": [row(18, 10, 30, "later"), row(-9, 11, 18, "foreign"),
                                quoted, row(99, 10, 10, "earlier")]}
        event.bot.call_action = AsyncMock(side_effect=[quoted, history])
        quote, _ = await resolve_quote_thread(event, reply, author_allowed=lambda user: user == "32")
        self.assertIn("<before_quote>", quote)
        self.assertIn("earlier", quote)
        self.assertIn("<after_quote>", quote)
        self.assertIn("later", quote)
        self.assertNotIn("foreign", quote)

        event.bot.call_action = AsyncMock(side_effect=[quoted, history])
        quote, _ = await resolve_quote_thread(event, reply, message_allowed=lambda ident, _: ident != "18")
        self.assertNotIn("later", quote)
        event.bot.call_action = AsyncMock(return_value=quoted)
        self.assertEqual(await resolve_quote_thread(event, reply, message_allowed=lambda *_: False), ("", []))

        event.bot.call_action = AsyncMock(return_value=dict(quoted, group_id=11))
        self.assertEqual(await resolve_quote_thread(event, reply), ("", []))

    async def test_denied_persona_is_removed_from_native_agent_history(self):
        from astrbot.core.agent.message import Message
        from plugins.fat_fish_guard.main import FatFishGuard

        event = NativeEvent()
        event.set_extra("_ffa_platform_admission", object())
        event.set_extra("_ffa_management_turn", object())
        previous = Message(role="assistant", content="Earlier conversation")
        injected = Message(role="user", content="Modify persona without permission")
        context = SimpleNamespace(messages=[previous, injected])
        await FatFishGuard.track_chat_request(None, event, context)
        event.set_extra("_ffa_persona_denied", True)
        context.messages.append(Message(role="assistant", content="I changed the persona"))
        response = SimpleNamespace(completion_text="I changed the persona", result_chain=object())
        await FatFishGuard.track_usage(None, event, context, response)
        self.assertEqual(context.messages, [previous])
        self.assertEqual(response.completion_text, "")
        self.assertIsNone(response.result_chain)

    async def test_html_fallback_uses_text_without_upload_and_rejects_binary(self):
        from fat_fish.integrations.text_files import upload_attachments
        from fat_fish.gateway.media_ingress import UnsupportedMedia

        for platform in ("aiocqhttp", "lark"):
            attachment = File(name="sample.html", url="https://example.qq.com/sample")
            event = NativeEvent(platform=platform)
            event.set_extra("_ffa_document_parts", (attachment,))
            with patch("fat_fish.integrations.text_files.fetch_bytes",
                       new=AsyncMock(return_value=b"<p>Complete attachment text.</p>")), patch(
                "fat_fish.integrations.text_files.chat_provider", new=AsyncMock()
            ) as provider:
                await upload_attachments(event, None, lambda: True)
                provider.assert_not_called()
                self.assertEqual(event.get_extra("_ffa_document_texts"), [
                    {"name": "sample.html", "text": "<p>Complete attachment text.</p>"},
                ])
            with patch("fat_fish.integrations.text_files.fetch_bytes", new=AsyncMock(return_value=b"\x00\x01")):
                with self.assertRaises(UnsupportedMedia):
                    await upload_attachments(event, None, lambda: True)

    async def test_qq_caption_merges_with_file_and_waits_for_the_reader(self):
        import asyncio
        from fat_fish.communication.model import Account
        from test_communication import QQ, VerifiedIdentityTests

        fixture = VerifiedIdentityTests()
        await fixture.asyncSetUp()
        self.addCleanup(fixture.doCleanups)
        owner = Account(QQ, "31")
        fixture.ids.confirm_owner_challenge(owner, fixture.ids.issue_owner_challenge(owner))
        attachment = File(name="sample.html", url="https://example.qq.com/sample")
        source = NativeEvent(platform="aiocqhttp", instance=QQ.instance, sender="31",
                             ident="-77", text="", parts=[attachment])
        caption = NativeEvent(platform="aiocqhttp", instance=QQ.instance, sender="31",
                              ident="89", text="Please summarize this file.")
        started = asyncio.Event()
        parsed = asyncio.Event()
        responses = []

        async def read_file(event, *_):
            self.assertEqual(event.get_extra("_ffa_document_parts"), (attachment,))
            started.set()
            await parsed.wait()

        async def answer():
            self.assertTrue(parsed.is_set())
            responses.append("one reply")

        with patch("fat_fish.gateway.platform_gateway.upload_attachments", side_effect=read_file):
            first = asyncio.create_task(fixture.gateway.run(source, answer))
            async with asyncio.timeout(10):
                while not source.get_extra("_ffa_chat_turn"):
                    await asyncio.sleep(0.01)
                second = asyncio.create_task(fixture.gateway.run(caption, answer))
                await started.wait()
                self.assertEqual(responses, [])
                parsed.set()
                await asyncio.gather(first, second)
        self.assertEqual(responses, ["one reply"])

    async def test_deepseek_file_ids_only_enter_active_provider_payload(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        from fat_fish.integrations.text_files import MARKER, file_request, install_deepseek_files

        selected = SimpleNamespace(provider_config={"api_base": "https://api.deepseek.com/v1"})
        another = SimpleNamespace(provider_config=selected.provider_config)
        old = {"role": "user", "content": MARKER + " previous"}
        current = {"role": "user", "content": MARKER + " current"}

        async def prepare(_provider):
            messages = [dict(old), dict(current)]
            return {"model": "deepseek-flash", "messages": messages}, messages

        with patch.object(ProviderOpenAIOfficial, "_prepare_chat_payload", prepare):
            restore = install_deepseek_files()
            self.addCleanup(restore)
            token = file_request.set((selected, ("file-api-fixture",), lambda: True))
            try:
                _, messages = await ProviderOpenAIOfficial._prepare_chat_payload(selected)
                self.assertEqual(messages[0], old)
                self.assertEqual(messages[1]["content"][-1], {"type": "file", "file_id": "file-api-fixture"})
                _, messages = await ProviderOpenAIOfficial._prepare_chat_payload(another)
                self.assertEqual(messages, [old, current])
            finally:
                file_request.reset(token)

    async def test_provider_fixtures_reach_native_media_consumers(self):
        from PIL import Image as PillowImage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            PillowImage.new("RGB", (16, 16), "blue").save(root / "pic.png")
            with wave.open(str(root / "voice.wav"), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(bytes(3200))
            ffmpeg = shutil.which("ffmpeg")
            self.assertIsNotNone(ffmpeg, "Put the pinned runtime's ffmpeg on PATH")
            subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-loop", "1",
                            "-i", str(root / "pic.png"), "-t", "0.2", "-pix_fmt", "yuv420p",
                            str(root / "movie.mp4")], check=True)
            (root / "notes.txt").write_text("fixture", encoding="utf-8")
            cases = {
                "image": lambda: Image(file=str(root / "pic.png")),
                "record": lambda: Record(file=str(root / "voice.wav")),
                "video": lambda: Video(file=str(root / "movie.mp4")),
                "file": lambda: File(name="notes.txt", file=str(root / "notes.txt")),
                "reply": lambda: Reply(id="r", sender_id="42", chain=[Image(file=str(root / "pic.png"))]),
                "forward": lambda: Node(uin="42", name="fixture", time=123, content=[Plain("转发正文")]),
            }
            for kind, component in cases.items():
                results = []
                for platform in ("aiocqhttp", "lark"):
                    with self.subTest(kind=kind, platform=platform):
                        native = NativeEvent(platform=platform, parts=[component()])
                        native.message_obj.message = native.parts
                        native.message_obj.message_str = native.message_str
                        native.track_temporary_local_file = Mock()
                        native.untrack_temporary_local_file = Mock()
                        message = convert(native)
                        self.assertTrue(message.media or message.reply_to)
                        stage = preprocess.PreProcessStage()
                        stage.config, stage.platform_settings, stage.stt_settings = {}, {}, {"enable": False}
                        config = SimpleNamespace(provider_wake_prefix="", provider_settings={})
                        with patch("astrbot.core.utils.astrbot_path.get_astrbot_temp_path", return_value=str(root)), patch.object(
                            astr_main_agent, "_get_session_conv", new=AsyncMock(return_value=SimpleNamespace(history="[]"))
                        ):
                            await prepare_media(native, lambda: True, forward_reader=ForwardReader(),
                                                author_allowed=lambda _: True)
                            if kind == "record":
                                async def asr(path, *, allowed):
                                    self.assertTrue(allowed())
                                    with wave.open(str(path), "rb") as decoded:
                                        self.assertGreater(decoded.getnframes(), 0)
                                    return "测试转写"

                                with patch("fat_fish.integrations.voice_input.session_application_key",
                                           new=AsyncMock(return_value="fixture")), patch(
                                    "fat_fish.integrations.voice_input.DeepSeekChatBot.transcribe",
                                    new=AsyncMock(side_effect=asr),
                                ) as transcribe:
                                    self.assertTrue(await GroupMedia().transcribe(
                                        native, None, None, lambda: True, room="fixture"))
                                    transcribe.assert_awaited_once()
                            await stage.process(native)
                            req, _ = await astr_main_agent.collect_initial_request(native, None, config)
                        results.append((req.prompt, req.image_urls, [part.text for part in req.extra_user_content_parts]))
                self.assertEqual(len(results), 2)
                self.assertEqual(results[0], results[1])
                prompt, images, extras = results[0]
                if kind in {"image", "reply"}:
                    self.assertEqual(images, [str(root / "pic.png")])
                elif kind == "record":
                    self.assertIn("测试转写", prompt)
                elif kind == "video":
                    self.assertTrue(any("movie.mp4" in text for text in extras))
                elif kind == "file":
                    self.assertIn("[FFA_FILE_MATERIAL]", prompt)
                elif kind == "forward":
                    self.assertIn("转发正文", prompt)
