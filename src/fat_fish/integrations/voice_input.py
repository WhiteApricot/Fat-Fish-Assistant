"""Normalize admitted voice attachments and send them to DeepSeek Speech to Text."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave

from fat_fish.integrations.deepseek_chatbot import ASR_MAX_BYTES, DeepSeekChatBot, session_application_key

VOICE_UNAVAILABLE = "这条语音没能转写。请检查 DeepSeek 语音服务配置，或尝试发送 120 秒以内的语音。"
WAV_SAMPLE_RATES = {8000, 16000, 22050, 24000, 32000, 44100, 48000}


def _pcm_wav(path):
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            if frames > audio.getframerate() * 120:
                raise ValueError("Voice exceeds 120 seconds")
            return bool(frames and audio.getsampwidth() == 2 and audio.getnchannels() in (1, 2)
                        and audio.getframerate() in WAV_SAMPLE_RATES and Path(path).stat().st_size <= ASR_MAX_BYTES)
    except (wave.Error, EOFError):
        return False


def _audio_kind(path):
    with Path(path).open("rb") as audio:
        header = audio.read(64)
    if header.startswith(b"OggS") and b"OpusHead" in header:
        return "opus"
    if header.lstrip(b"\x02").startswith(b"#!SILK_V3"):
        return "silk"
    return None


@asynccontextmanager
async def _normalized(path):
    if await asyncio.to_thread(_pcm_wav, path):
        yield str(path)
        return
    with TemporaryDirectory(prefix="ffa-voice-") as directory:
        wav = Path(directory) / "voice.wav"
        # For WAV containers with an unsupported sample format, the native
        # extension/magic check would otherwise reuse the original file.
        from imageio_ffmpeg import get_ffmpeg_exe

        process = await asyncio.create_subprocess_exec(
            get_ffmpeg_exe(), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "wav,ogg,amr,mp3,aac,flac,mov", "-i", str(path), "-t", "121",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}),
        )
        try:
            if await process.wait() or not await asyncio.to_thread(_pcm_wav, wav):
                raise ValueError("Unsupported voice format")
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        yield str(wav)


async def transcribe_record(record, context, event, allowed):
    from astrbot.core.utils.media_utils import MediaResolver

    if not allowed():
        return ""
    try:
        config = getattr(context, "get_config", None)
        settings = config(umo=event.unified_msg_origin).get("provider_settings", {}) if config else {}
        key = await session_application_key(context, event.unified_msg_origin, settings)
        if not key:
            return ""
        async with asyncio.timeout(45):
            source = await record._resolve_file_source()
            if not source or not allowed():
                return ""
            async with MediaResolver(source, default_suffix=".wav").as_path() as media:
                source = media.path
                kind = await asyncio.to_thread(_audio_kind, source)
                client = DeepSeekChatBot(key)
                if kind == "opus":
                    text = await client.transcribe(source, allowed=allowed)
                elif kind == "silk":
                    # Use the AstrBot SILK decoder on the already localized audio.
                    async with MediaResolver(str(source), media_type="audio").as_path(target_format="wav") as decoded:
                        async with _normalized(decoded.path) as path:
                            text = await client.transcribe(path, allowed=allowed)
                else:
                    async with _normalized(source) as path:
                        text = await client.transcribe(path, allowed=allowed)
    except Exception:
        # The STT boundary can fail with SDK-specific exceptions. The caller
        # reports a voice error and never passes raw audio or error data to chat.
        return ""
    return text.strip()[:4000] if allowed() and isinstance(text, str) else ""
