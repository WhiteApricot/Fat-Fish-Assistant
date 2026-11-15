"""Lark-specific outbound capabilities over the pinned AstrBot adapter."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from fat_fish.adapters.astrbot_communication import AstrBotCommunication
from fat_fish.adapters.lark_retraction import LarkRetractions
from fat_fish.communication.model import Capability, ChatKind, MessageRetraction, NotSubmitted


@contextmanager
def guard_stream(event, authorize):
    """Distinguish a prepared CardKit entity from an IM submission."""
    if not all(callable(getattr(event, name, None)) for name in (
        "_create_streaming_card", "_send_card_message", "_update_streaming_text", "_close_streaming_mode",
    )):
        yield
        return

    create = event._create_streaming_card
    send = event._send_card_message
    update = event._update_streaming_text
    stale = False

    async def checked_create():
        authorize(False)
        return await create()

    async def checked_send(*args, **kwargs):
        nonlocal stale
        authorize()
        if not await send(*args, **kwargs):
            # The native fallback sends a second IM message. The first submission
            # may already be visible, so only card creation can trigger fallback.
            raise ConnectionError("CardKit message submission failed")
        stale = False
        return True

    async def checked_update(*args, **kwargs):
        nonlocal stale
        authorize()
        stale = not await update(*args, **kwargs)
        return not stale

    async def checked_close(card_id, sequence):
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        # AstrBot's close method logs SDK errors and always returns None.
        request = SettingsCardRequest.builder().card_id(card_id).request_body(
            SettingsCardRequestBody.builder()
            .settings(json.dumps({"config": {"streaming_mode": False}}))
            .sequence(sequence).uuid(str(uuid4())).build()
        ).build()
        authorize()
        response = await event.bot.cardkit.v1.card.asettings(request)
        if not response.success() or stale:
            raise ConnectionError("CardKit stream did not finish successfully")

    hooks = {
        "_create_streaming_card": checked_create,
        "_send_card_message": checked_send,
        "_update_streaming_text": checked_update,
        "_close_streaming_mode": checked_close,
    }
    previous = {name: vars(event)[name] for name in hooks if name in vars(event)}
    try:
        for name, hook in hooks.items():
            setattr(event, name, hook)
        yield
    finally:
        for name in hooks:
            if name in previous:
                setattr(event, name, previous[name])
            else:
                delattr(event, name)


class LarkCommunication(AstrBotCommunication):
    capabilities = AstrBotCommunication.capabilities | {
        Capability.MEDIA, Capability.IMAGE, Capability.FILE, Capability.RETRACT,
    }
    guard_stream = staticmethod(guard_stream)

    def __init__(self, address, bot, context):
        super().__init__(address, bot, context)
        self._next_bot_probe = 0
        self.retractions = (LarkRetractions(address, bot, context.private_dir / "ffa-lark-retractions.sqlite3",
                                            context.retract_native)
                            if context.private_dir is not None and context.retract_native is not None else None)

    async def start(self):
        adapter = self.context.platform_instance()
        refresh = getattr(adapter, "_refresh_bot_info", None)
        now = time.monotonic()
        if callable(refresh) and not getattr(adapter, "bot_open_id", None) and now >= self._next_bot_probe:
            # AstrBot may have attempted this before connectivity was available.
            # A reminder-only session can recover without waiting for ingress.
            self._next_bot_probe = now + 30
            async with asyncio.timeout(5):
                await refresh()
        await super().start()
        if self.retractions is not None:
            self.retractions.attach(self.context.platform_instance())

    async def stop(self):
        if self.retractions is not None:
            self.retractions.detach()
        await super().stop()

    def track_inbound(self, event, message):
        if self.retractions is None:
            return True
        self.retractions.attach(self.context.platform_instance())
        return self.retractions.track(event, message)

    def convert_retraction(self, notice):
        if not isinstance(notice, MessageRetraction):
            raise ValueError("Expected a correlated native recall")
        return notice

    async def verify_retraction(self, notice):
        return bool(self.retractions and notice in self.retractions.verified and await self.healthy())

    def outbound_origin(self, target):
        if target.provider != self.address or target.bot != self.bot or target.thread != target.peer:
            return None
        # Both native session IDs are canonical and already part of persisted
        # reminder scopes. Reject delimiters and unrecognized address types.
        if target.kind is ChatKind.PRIVATE and re.fullmatch(r"ou_[A-Za-z0-9_-]+", target.peer):
            return f"{self.address.instance}:FriendMessage:{target.peer}"
        if target.kind is ChatKind.GROUP and re.fullmatch(r"oc_[A-Za-z0-9_-]+", target.peer):
            return f"{self.address.instance}:GroupMessage:{target.peer}"
        return None

    async def ready_for_push(self, target):
        return await self.healthy()

    async def send_checked(self, target, message, allowed):
        if not message.media:
            return await super().send_checked(target, message, allowed)

        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import At, Image, Plain
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if self.outbound_origin(target) is None or not await self.healthy():
            raise NotSubmitted("Invalid or unavailable Lark route")
        paths = []
        for media in message.media:
            if media.kind not in {"image", "file"} or not isinstance(media.reference, str):
                raise NotSubmitted("Lark supports outbound local images and files")
            path = Path(media.reference)
            if not path.is_absolute() or not path.is_file():
                raise NotSubmitted("Lark attachment must be a readable local file")
            paths.append(path)

        adapter = self.context.platform_instance()
        client = adapter.lark_api
        if any(media.kind == "file" for media in message.media):
            if len(paths) != 1 or message.text or message.mentions:
                raise NotSubmitted("Send one Lark file as a standalone message")
            file_key = await LarkMessageEvent._upload_lark_file(client, path=str(paths[0]), file_type="stream")
            if not file_key:
                raise NotSubmitted("Lark file upload failed")
            msg_type, content = "file", {"file_key": file_key}
        else:
            chain = MessageChain()
            chain.chain = [At(qq=subject) for subject in message.mentions]
            if message.text:
                chain.chain.append(Plain(message.text))
            chain.chain.extend(Image(file=str(path)) for path in paths)
            parts = await LarkMessageEvent._convert_to_lark(chain, client)
            if sum(bool(part.get("image_key")) for line in parts for part in line) != len(paths):
                raise NotSubmitted("Lark image upload failed")
            msg_type, content = "post", {"zh_cn": {"title": "", "content": parts}}

        if self.context.platform_instance() is not adapter or not await self.healthy() or not allowed():
            raise NotSubmitted("Lark target authorization or adapter revoked")
        if not await LarkMessageEvent._send_im_message(
            client, content=json.dumps(content), msg_type=msg_type, receive_id=target.peer,
            receive_id_type="open_id" if target.kind is ChatKind.PRIVATE else "chat_id",
        ):
            raise ConnectionError("Lark media submission failed")
