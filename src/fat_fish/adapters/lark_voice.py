"""Transcribe native Lark voice bars through the currently selected AstrBot STT."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave

VOICE_UNAVAILABLE = "这条飞书语音没能转写。请确认本机语音识别服务正在运行，或试着发送更短的语音。"


def _pcm_wav(path):
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            if frames > audio.getframerate() * 120:
                raise ValueError("Voice exceeds 120 seconds")
            return bool(frames and audio.getsampwidth() == 2 and audio.getnchannels() in (1, 2))
    except (wave.Error, EOFError):
        return False


@asynccontextmanager
async def _normalized(path):
    if await asyncio.to_thread(_pcm_wav, path):
        yield str(path)
        return
    with TemporaryDirectory(prefix="lark-voice-") as directory:
        wav = Path(directory) / "voice.wav"
        # For WAV containers with an unsupported sample format, the native
        # extension/magic check would otherwise reuse the original file.
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", "121",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}),
        )
        try:
            if await process.wait() or not await asyncio.to_thread(_pcm_wav, wav):
                raise ValueError("Unsupported native Lark voice format")
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        yield str(wav)


async def transcribe(record, provider, allowed):
    from astrbot.core.utils.media_utils import MediaResolver

    source = record.path or record.url or record.file
    if not source or not allowed():
        return ""
    try:
        async with asyncio.timeout(25):
            async with MediaResolver(source, media_type="audio", default_suffix=".wav").as_path() as media:
                async with _normalized(media.path) as path:
                    if not allowed():
                        return ""
                    text = await provider.get_text(audio_url=path)
    except Exception:
        # The STT boundary can fail with SDK-specific exceptions. The caller
        # reports a voice error and never passes raw audio or error data to chat.
        return ""
    return text.strip()[:4000] if allowed() and isinstance(text, str) else ""
