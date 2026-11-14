"""Convert authenticated AstrBot messages and wrap the native send surface."""
import time
from contextlib import contextmanager

from fat_fish.communication.model import (
    Account, AdapterManifest, Capability, ChatKind, Conversation, Media, MessageEvent, NotSubmitted, ProviderId,
)

QQ = ProviderId("qq", "feiyu-onebot")
ONEBOT_NAME = "aiocqhttp"
ADAPTER_ID = "astrbot"


def filter_native_lark_chat_log(record):
    """Suppress the native event outline, which includes private commands and IDs."""
    return not (getattr(record, "category", None) == "user_chat"
                and getattr(record, "platform_type", None) == "lark")


@contextmanager
def guard_lark_stream(event, check_submission):
    """Guard the submission primitives in the pinned AstrBot CardKit adapter."""
    originals = {}
    try:
        for name in ("_create_streaming_card", "_send_card_message", "_update_streaming_text", "_close_streaming_mode"):
            call = getattr(event, name, None)
            if call is None:
                continue
            originals[name] = call

            async def checked(*args, call=call, **kwargs):
                check_submission()
                return await call(*args, **kwargs)

            setattr(event, name, checked)
        yield
    finally:
        for name, call in originals.items():
            setattr(event, name, call)


def provider_id(event):
    platform = event.get_platform_name()
    return ProviderId("qq" if platform == ONEBOT_NAME else platform, event.get_platform_id())


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Missing message identity")
    return str(value)


def _components(event, legacy):
    mentions, media = [], []
    reply = quoted_sender = None
    for part in event.get_messages():
        kind = type(part).__name__
        if kind == "At":
            target = getattr(part, "qq", None)
            if isinstance(target, (str, int)) and not isinstance(target, bool) and target != "":
                mentions.append(str(target))
        elif kind == "Reply":
            target = getattr(part, "id", None)
            if isinstance(target, (str, int)) and not isinstance(target, bool) and target != "":
                reply = str(target)
                author = getattr(part, "sender_id", None)
                quoted_sender = str(author) if isinstance(author, (str, int)) and not isinstance(author, bool) and author != "" else None
        elif kind in {"Image", "Record", "Video", "File", "Forward", "Node", "Nodes"}:
            # AstrBot File.file may download the attachment. Read its stored reference.
            fields = ("id", "url", "file_") if kind == "File" else ("id", "url", "file")
            reference = next((value for name in fields
                              if isinstance(value := getattr(part, name, None), (str, int))
                              and not isinstance(value, bool) and value != ""), None)
            content_type = getattr(part, "mime_type", None)
            filename = getattr(part, "name", None) or getattr(part, "filename", None)
            media.append(Media(kind.lower(), str(reference) if reference is not None else None,
                               content_type if isinstance(content_type, str) else None,
                               filename if isinstance(filename, str) else None))
    if legacy:
        raw = getattr(event.message_obj, "raw_message", None)
        parts = raw.get("message") if isinstance(raw, dict) else None
        for part in parts if isinstance(parts, list) else ():
            if isinstance(part, dict) and isinstance(part.get("data"), dict):
                if part.get("type") == "at":
                    value = str(part["data"].get("qq", ""))
                    if value.isascii() and value.isdecimal():
                        mentions.append(value)
                if part.get("type") == "reply" and reply is None:
                    value = str(part["data"].get("id", ""))
                    reply = value or None
    return tuple(dict.fromkeys(mentions)), reply, tuple(media), quoted_sender


def convert(event):
    provider = provider_id(event)
    sender = Account(provider, _id(event.get_sender_id()))
    kind = ChatKind.PRIVATE if event.is_private_chat() else ChatKind.GROUP
    peer = sender.subject if kind is ChatKind.PRIVATE else _id(event.get_group_id())
    native_session = event.get_session_id() if hasattr(event, "get_session_id") else getattr(event, "session_id", peer)
    conversation = Conversation(provider, _id(event.get_self_id()), kind, peer, _id(native_session or peer))
    legacy = event.get_platform_name() == ONEBOT_NAME
    mentions, reply, media, quoted_sender = _components(event, legacy)
    if provider.platform == "lark" and not reply:
        parent = getattr(getattr(event.message_obj, "raw_message", None), "parent_id", None)
        if isinstance(parent, str) and parent:
            reply = parent
    timestamp = float(getattr(event.message_obj, "timestamp", time.time()))
    text = event.message_str
    if provider.platform == "lark" and mentions:
        parts = event.get_messages()
        if all(type(part).__name__ in {"Plain", "At"} for part in parts):
            command = "".join(part.text for part in parts if type(part).__name__ == "Plain").strip()
            if command.startswith(("/blacklist ", "/role moderator-add", "/role moderator-remove")):
                text = command
    return MessageEvent(conversation, sender, _id(event.message_obj.message_id),
                        timestamp, text, mentions, reply, media,
                        quoted_sender=quoted_sender, adapter_id=ADAPTER_ID)


def create_provider(address, bot, context, config):
    if address.platform == "qq":
        from fat_fish.adapters.onebot_communication import OneBotCommunication

        return OneBotCommunication(address, bot, context)
    return AstrBotCommunication(address, bot, context)


class AstrBotCommunication:
    """Host-owned wrapper. The platform adapter keeps its native connection."""

    capabilities = frozenset({Capability.TEXT, Capability.PUSH, Capability.DIRECT_MESSAGE, Capability.GROUP_MESSAGE})
    verification_capabilities = frozenset({"private-message"})
    guard_stream = staticmethod(guard_lark_stream)
    native_platform = None

    def __init__(self, address, bot, context):
        self.address = address
        self.manifest = AdapterManifest(ADAPTER_ID, "1", 1, address, self.capabilities)
        self.bot = bot
        self.context = context
        self.routes = {}
        self.origins = {}

    async def start(self):
        if not await self.healthy():
            raise ConnectionError("Native adapter unavailable")

    async def stop(self):
        # The FFA gate stops dispatch; AstrBot owns the physical connection.
        self.routes.clear()
        self.origins.clear()

    async def healthy(self):
        adapter = self.context.platform_instance()
        platform = self.native_platform or self.address.platform
        if adapter is None or adapter.meta().name != platform:
            return False
        if platform == "lark":
            # AstrBot may put a display name in events until it fetches the real bot ID.
            return getattr(adapter, "bot_open_id", None) == self.bot
        return True

    async def verify_account(self, message):
        """Accept a proof only from the authenticated direct-message adapter ingress."""
        room = message.conversation
        if (room.provider == self.address and room.bot == self.bot and room.kind is ChatKind.PRIVATE
                and message.sender.subject != self.bot and not message.reply_to and not message.media
                and await self.healthy()):
            return message.sender
        return None

    def observe(self, conversation, origin):
        expected = "FriendMessage" if conversation.kind is ChatKind.PRIVATE else "GroupMessage"
        if origin != f"{self.address.instance}:{expected}:{conversation.thread}":
            raise ValueError("Native conversation mismatch")
        known = self.origins.get(origin)
        if known is not None and known != conversation:
            raise ValueError("Native conversation collision")
        self.origins[origin] = conversation
        self.routes[conversation] = origin

    async def send(self, target, message):
        return await self.send_checked(target, message, lambda: True)

    async def prepare_media(self, event, message, allowed, author_allowed):
        if self.address.platform != "lark" or not message.reply_to:
            return
        from fat_fish.adapters.lark_quote import quote_context

        quote = await quote_context(event, message, author_allowed)
        if not allowed():
            raise PermissionError("Quote authorization revoked")
        if not quote:
            parts = event.get_messages()
            parts[:] = [part for part in parts if type(part).__name__ != "Reply"]
        event.set_extra("_ffa_quoted_context", quote or "用户明确引用了一条消息，但未能核实其内容。请勿猜测引用原文。")

    async def reply_native(self, event, target, result, original_send):
        if self.address.platform != "lark" or not hasattr(result, "chain") or not result.chain:
            return await original_send(result)
        if any(type(part).__name__ not in {"Plain", "At", "Reply"} for part in result.chain):
            return await original_send(result)
        if any(type(part).__name__ == "Reply" and str(part.id) != str(event.message_obj.message_id)
               for part in result.chain):
            return await original_send(result)
        from astrbot.core.platform.astr_message_event import AstrMessageEvent

        result = result.derive([part for part in result.chain if type(part).__name__ != "Reply"])
        if result.chain:
            if not await self.context.send_native(self.routes[target], result):
                raise ConnectionError("Native adapter rejected delivery")
            await AstrMessageEvent.send(event, result)

    async def send_checked(self, target, message, allowed):
        origin = self.routes.get(target)
        if origin is None:
            raise NotSubmitted("Native conversation has not been verified")

        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import At, Plain

        chain = MessageChain()
        chain.chain = [*(At(qq=subject) for subject in message.mentions), Plain(message.text)]
        if not allowed():
            raise NotSubmitted("Target authorization revoked")
        if not await self.context.send_native(origin, chain):
            raise ConnectionError("Native adapter rejected delivery")
