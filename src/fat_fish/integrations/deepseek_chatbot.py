"""Public DeepSeek application APIs used by the shared FFA chat pipeline."""
import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from fat_fish.model_config import DEFAULT_CHAT_MODEL

BASE_URL = "https://api.deepseek.com"

ASR_MAX_BYTES = 8 * 1024 * 1024
logger = logging.getLogger(__name__)


class DeepSeekError(RuntimeError):
    pass


def official_provider(provider):
    config = getattr(provider, "provider_config", {})
    try:
        url = urlsplit(config.get("api_base") or "")
        return url.scheme == "https" and url.netloc.lower() in {"api.deepseek.com", "api.deepseek.com:443"}
    except (TypeError, ValueError):
        return False


async def chat_provider(context, origin):
    lookup = getattr(context, "get_using_provider_async", None)
    return await lookup(origin) if lookup else None


def provider_key(provider):
    if official_provider(provider):
        value = provider.get_current_key()
        if isinstance(value, str) and value.strip() and not value.lstrip().startswith("$"):
            return value.strip()
    return ""


def application_key(settings):
    keys = settings.get("websearch_deepseek_key", [])
    if isinstance(keys, str):
        keys = [keys]
    for value in keys or ():
        if isinstance(value, str) and value.strip() and not value.lstrip().startswith("$"):
            return value.strip()
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


async def session_application_key(context, origin, settings):
    """Reuse credentials only from the active official DeepSeek provider."""
    try:
        key = provider_key(await chat_provider(context, origin))
    except (AttributeError, LookupError, TypeError, ValueError):
        key = ""
    return key or application_key(settings)


class DeepSeekChatBot:
    def __init__(self, key):
        self.key = key

    async def _request(self, method, path, *, allowed, timeout, json_body=None, fields=None):
        import aiohttp

        if not self.key:
            raise DeepSeekError("credential unavailable")
        started = time.monotonic()
        status = None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True) as client:
                for attempt in range(2):
                    if not await asyncio.to_thread(allowed):
                        raise PermissionError("DeepSeek capability access revoked")
                    form = None
                    if fields:
                        form = aiohttp.FormData()
                        for name, value, options in fields:
                            form.add_field(name, value, **options)
                    async with client.request(method, BASE_URL + path, json=json_body, data=form,
                                              headers={"Authorization": "Bearer " + self.key},
                                              allow_redirects=False) as response:
                        status = response.status
                        if status == 429 and attempt == 0:
                            # A rejected rate-limited request can be resubmitted. Do not
                            # repeat an ambiguous timeout or a partially billed generation.
                            pass
                        elif status != 200:
                            raise DeepSeekError(f"HTTP {status}")
                        else:
                            data = await response.json()
                            if not await asyncio.to_thread(allowed):
                                raise PermissionError("DeepSeek capability access revoked")
                            if not isinstance(data, dict) or data.get("error"):
                                raise DeepSeekError("incomplete API response")
                            return data
                    await asyncio.sleep(1)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise DeepSeekError(type(exc).__name__) from exc
        finally:
            operation = "/files/{id}" if method == "DELETE" else path
            logger.info("DeepSeek %s %s status=%s elapsed_ms=%d", method, operation, status,
                        (time.monotonic() - started) * 1000)

    async def web(self, messages, *, read_url, allowed):
        tool = "deepseek_read_url" if read_url else "deepseek_web_search"
        data = await self._request("POST", "/chat/completions", allowed=allowed, timeout=45, json_body={
            "model": DEFAULT_CHAT_MODEL, "stream": False, "thinking": {"type": "disabled"},
            "safety_check": {"level": "high"}, "tools": [{"type": tool}], "messages": messages,
            "max_tokens": 1800,
        })
        usage = data.get("usage") or {}
        logger.info("DeepSeek native %s input=%s cached=%s output=%s tools=%s", tool, usage.get("prompt_tokens"),
                    usage.get("prompt_cache_hit_tokens"), usage.get("completion_tokens"), usage.get("server_tool_use"))
        choice = next(iter(data.get("choices") or ()), {})
        message = choice.get("message") or {}
        if (choice.get("finish_reason") != "stop" or not isinstance(message.get("content"), str)
                or not message["content"].strip()):
            raise DeepSeekError("web completion did not finish")
        return message

    async def upload(self, filename, data, purpose, *, allowed):
        result = await self._request("POST", "/files", allowed=allowed, timeout=135, fields=[
            ("purpose", purpose, {}), ("expires_after[anchor]", "created_at", {}),
            ("expires_after[seconds]", "3600", {}),
            ("file", data, {"filename": filename, "content_type": "application/octet-stream"}),
        ])
        file_id = result.get("id")
        if not isinstance(file_id, str) or not file_id.startswith("file-api-"):
            raise DeepSeekError("missing file ID")
        return file_id

    async def delete_file(self, file_id):
        await self._request("DELETE", "/files/" + quote(file_id, safe=""), allowed=lambda: True, timeout=8)

    async def transcribe(self, path, *, allowed):
        def read_audio():
            with Path(path).open("rb") as source:
                return source.read(ASR_MAX_BYTES + 1)

        audio = await asyncio.to_thread(read_audio)
        if not audio or len(audio) > ASR_MAX_BYTES:
            raise DeepSeekError("audio exceeds size limit or is empty")
        ogg = audio.startswith(b"OggS")
        result = await self._request("POST", "/v1/tools/asr", allowed=allowed, timeout=25, fields=[
            ("file", audio, {"filename": "voice.ogg" if ogg else "voice.wav",
                             "content_type": "audio/ogg" if ogg else "audio/wav"}),
        ])
        logger.info("DeepSeek ASR duration_ms=%s", result.get("duration_ms"))
        text = result.get("text")
        return text.strip() if isinstance(text, str) else ""
