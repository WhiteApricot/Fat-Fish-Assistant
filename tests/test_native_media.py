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
                        speech = SimpleNamespace(get_text=AsyncMock(return_value="离线转写"))
                        stage = preprocess.PreProcessStage()
                        stage.config, stage.platform_settings, stage.stt_settings = {}, {}, {"enable": True}
                        stage.plugin_manager = SimpleNamespace(context=SimpleNamespace(
                            get_using_stt_provider_async=AsyncMock(return_value=speech)))
                        config = SimpleNamespace(provider_wake_prefix="", provider_settings={})
                        with patch("astrbot.core.utils.astrbot_path.get_astrbot_temp_path", return_value=str(root)), patch.object(
                            astr_main_agent, "_get_session_conv", new=AsyncMock(return_value=SimpleNamespace(history="[]"))
                        ):
                            await prepare_media(native, lambda: True, forward_reader=ForwardReader(),
                                                author_allowed=lambda _: True)
                            await stage.process(native)
                            req, _ = await astr_main_agent.collect_initial_request(native, None, config)
                        if kind == "record":
                            speech.get_text.assert_awaited_once()
                            with wave.open(speech.get_text.await_args.kwargs["audio_url"], "rb") as decoded:
                                self.assertGreater(decoded.getnframes(), 0)
                        results.append((req.prompt, req.image_urls, [part.text for part in req.extra_user_content_parts]))
                self.assertEqual(len(results), 2)
                self.assertEqual(results[0], results[1])
                prompt, images, extras = results[0]
                if kind in {"image", "reply"}:
                    self.assertEqual(images, [str(root / "pic.png")])
                elif kind == "record":
                    self.assertIn("离线转写", prompt)
                elif kind == "video":
                    self.assertTrue(any("movie.mp4" in text for text in extras))
                elif kind == "file":
                    self.assertIn('"正文": "fixture"', prompt)
                elif kind == "forward":
                    self.assertIn("转发正文", prompt)
