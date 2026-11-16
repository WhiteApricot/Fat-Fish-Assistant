"""Bounded attachment download and turn-scoped DeepSeek file references."""
import asyncio
from contextvars import ContextVar
import ipaddress
from pathlib import Path, PureWindowsPath
from urllib.parse import urljoin, urlparse

from fat_fish.integrations.deepseek_chatbot import (
    DeepSeekChatBot, DeepSeekError, chat_provider, official_provider, provider_key,
)

from fat_fish.model_config import DEFAULT_CHAT_MODEL

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_FILES = 3
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
DATA_EXTENSIONS = {".xls", ".xlsx", ".epub", ".mobi"}
MARKER = "[FFA_FILE_MATERIAL]"
file_request = ContextVar("ffa_deepseek_file_request", default=None)


def allowed_url(url):
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if not host or port not in {None, 80, 443}:
        return False
    try:
        address = ipaddress.ip_address(host)
        return address.is_global and not address.is_multicast
    except ValueError:
        return any(host == domain or host.endswith("." + domain)
                   for domain in ("qq.com", "qq.com.cn", "qpic.cn", "gtimg.cn"))


async def fetch_bytes(url, limit):
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as client:
        for _ in range(4):
            if not allowed_url(url):
                raise ValueError("File URL is outside the QQ download boundary")
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                if response.content_length is not None and response.content_length > limit:
                    raise ValueError("Attachment exceeds the size limit")
                data = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("Attachment exceeds the size limit")
                return bytes(data)
        raise ValueError("Too many file redirects")


def read_materialized(path, root, limit):
    resolved = Path(path).resolve(strict=True)
    resolved.relative_to(Path(root).resolve(strict=True))
    if not resolved.is_file():
        raise ValueError("Attachment is not a regular file")
    with resolved.open("rb") as source:
        return source.read(limit + 1)


def decode_text(data):
    if b"\x00" in data:
        raise ValueError("Binary attachment")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            raise ValueError("Binary attachment")
        return text
    raise ValueError("Unknown text encoding")


async def upload_attachments(event, context, allowed):
    parts = event.get_extra("_ffa_document_parts", ())
    if not parts:
        return
    import aiohttp
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    from fat_fish.gateway.media_ingress import UnsupportedMedia

    provider = await chat_provider(context, event.unified_msg_origin)
    key = provider_key(provider)
    if not key or provider.get_model() != DEFAULT_CHAT_MODEL:
        raise UnsupportedMedia("当前主模型配置无法使用 DeepSeek 文件输入，请检查实例的官方 DeepSeek Flash 配置。")
    client = DeepSeekChatBot(key)
    file_ids = []
    event.set_extra("_ffa_document_uploads", (provider, client, file_ids))
    failures = max(0, len(parts) - MAX_FILES)
    remaining = MAX_BYTES
    temp_root = get_astrbot_temp_path()
    for part in parts[:MAX_FILES]:
        name = PureWindowsPath(str(getattr(part, "name", "") or "unnamed")).name[:120]
        extension = PureWindowsPath(name).suffix.lower()
        if extension not in TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS | DATA_EXTENSIONS or remaining <= 0:
            failures += 1
            continue
        limit = min(remaining, MAX_TEXT_BYTES) if extension in TEXT_EXTENSIONS else remaining
        if not await asyncio.to_thread(allowed):
            raise PermissionError("Attachment access revoked")
        try:
            path = getattr(part, "file_", None)
            if path and temp_root is not None:
                data = await asyncio.to_thread(read_materialized, path, temp_root, limit)
            else:
                data = await fetch_bytes(str(getattr(part, "url", "") or ""), limit)
            if not data or len(data) > limit:
                raise ValueError("Attachment is empty or exceeds size limit")
            remaining -= len(data)
            if extension in TEXT_EXTENSIONS:
                data = decode_text(data).encode("utf-8")
        except (aiohttp.ClientError, OSError, ValueError):
            failures += 1
            continue
        purpose = "text_recognition" if extension in DOCUMENT_EXTENSIONS else "user_data"
        try:
            file_ids.append(await client.upload(name, data, purpose, allowed=allowed))
        except DeepSeekError:
            failures += 1
    if not file_ids:
        raise UnsupportedMedia("没能读取附件。请检查文件类型、大小、访问权限或稍后重试。")
    if failures:
        event.set_extra("_ffa_document_failures", failures)


async def release_attachments(event):
    uploads = event.get_extra("_ffa_document_uploads")
    event.set_extra("_ffa_document_uploads", None)
    event.set_extra("_ffa_document_parts", ())
    if uploads:
        _, client, ids = uploads
        await asyncio.gather(*(client.delete_file(file_id) for file_id in ids), return_exceptions=True)


def install_deepseek_files():
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    original = ProviderOpenAIOfficial._prepare_chat_payload

    async def with_files(provider, *args, **kwargs):
        payload, messages = await original(provider, *args, **kwargs)
        active = file_request.get()
        if active is None or active[0] is not provider or not official_provider(provider):
            return payload, messages
        _, ids, allowed = active
        if not await asyncio.to_thread(allowed):
            raise asyncio.CancelledError("Attachment access revoked")
        if payload["model"] != DEFAULT_CHAT_MODEL:
            raise DeepSeekError("file input requires the active DeepSeek Flash model")
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content") or ""
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
            if any(part.get("type") == "text" and MARKER in part.get("text", "") for part in parts):
                message["content"] = parts + [{"type": "file", "file_id": file_id} for file_id in ids]
                return payload, messages
            break
        raise DeepSeekError("current file message missing from provider context")

    ProviderOpenAIOfficial._prepare_chat_payload = with_files

    def restore():
        if ProviderOpenAIOfficial._prepare_chat_payload is with_files:
            ProviderOpenAIOfficial._prepare_chat_payload = original

    return restore
