"""Feishu capabilities over the pinned AstrBot Lark adapter."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
from uuid import uuid4

from fat_fish.adapters.astrbot_communication import AstrBotCommunication
from fat_fish.adapters.lark_forwards import message_type, read_forward
from fat_fish.adapters.lark_latency import attach_native_ingress, ensure_trace, submitted, trace
from fat_fish.adapters.lark_retraction import LarkRetractions
from fat_fish.communication.model import Capability, ChatKind, MessageRetraction, NotSubmitted
from fat_fish.gateway.reply_schedule import WEB_PROGRESS_TYPE


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
        wait = event.get_extra("_ffa_wait_reply")
        if wait is not None:
            await wait()
        authorize()
        with submitted(event, streaming=True):
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

    def ingress_trace(self, event, arrived):
        return ensure_trace(event, self.context.private_dir, arrived)

    async def reply_native(self, event, target, result, original_send):
        timeline = trace(event)
        if timeline is not None:
            timeline.mark("route_start")
        chat = self._trusted_private_chat(event, target, result)
        if timeline is not None:
            timeline.route = "native_chat_id" if chat else "native_route"
            timeline.mark("route_end")
        with submitted(event, progress=getattr(result, "type", None) == WEB_PROGRESS_TYPE):
            if chat:
                return await self._reply_to_private_chat(event, target, result, chat)
            return await super().reply_native(event, target, result, original_send)

    def _trusted_private_chat(self, event, target, result):
        if (target.kind is not ChatKind.PRIVATE or target.provider != self.address or target.bot != self.bot
                or target.peer != event.get_sender_id() or target.peer != target.thread
                or not hasattr(result, "chain") or not result.chain):
            return None
        obj = event.message_obj
        raw = getattr(obj, "raw_message", None)
        chat = getattr(raw, "chat_id", None)
        adapter = self.context.platform_instance()
        if (not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat)
                or getattr(raw, "chat_type", None) != "p2p" or getattr(raw, "message_id", None) != obj.message_id
                or event.bot is not getattr(adapter, "lark_api", None)
                or not all(type(part).__name__ in {"Plain", "At"} or
                           type(part).__name__ == "Reply" and str(part.id) == str(obj.message_id)
                           for part in result.chain)):
            return None
        return chat

    @staticmethod
    async def _reply_to_private_chat(event, target, result, chat):
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        result = result.derive([part for part in result.chain if type(part).__name__ != "Reply"])
        if not result.chain:
            return
        api = event.bot.im.v1.message

        async def create(request):
            response = await api.acreate(request)
            # 230002 is an explicit rejection of the chat route. An ambiguous
            # API error or transport failure keeps the existing unknown state.
            if response.success() or response.code != 230002:
                return response
            allowed = event.get_extra("_ffa_platform_authorize")
            if allowed is None or not await asyncio.to_thread(allowed):
                return response
            body = request.request_body
            return await api.acreate(CreateMessageRequest.builder().receive_id_type("open_id").request_body(
                CreateMessageRequestBody.builder().receive_id(target.peer).content(body.content)
                .msg_type(body.msg_type).uuid(body.uuid).build()
            ).build())

        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(acreate=create))))
        await LarkMessageEvent.send_message_chain(result, client, receive_id=chat, receive_id_type="chat_id")
        await AstrMessageEvent.send(event, result)

    @staticmethod
    def cache_unmentioned_voice(event, message):
        if (message_type(event) != "audio" or message.conversation.kind is not ChatKind.GROUP
                or message.conversation.bot in message.mentions):
            return False
        LarkCommunication._voice_record(event)
        return True

    @staticmethod
    def _voice_record(event):
        from astrbot.api.message_components import Record

        parts = event.get_messages()
        for part in parts:
            if type(part).__name__ == "Record":
                return part
        record = Record(file=None)
        parts.append(record)
        return record

    @staticmethod
    def is_external_material(event):
        return message_type(event) == "merge_forward"

    async def prepare_media(self, event, message, allowed, author_allowed):
        if not self.is_external_material(event):
            await super().prepare_media(event, message, allowed, author_allowed)
            if message_type(event) == "audio":
                self._voice_record(event)
            return

        from astrbot.api.message_components import Plain
        from fat_fish.gateway.media_ingress import UnsupportedMedia

        material = await read_forward(event, quoted=False, author_allowed=author_allowed, allowed=allowed)
        if not material:
            raise UnsupportedMedia("这条飞书合并转发的正文暂时无法读取。")
        event.message_str = "请概括此合并转发的内容。\n" + material
        event.message_obj.message_str = event.message_str
        parts = event.get_messages()
        parts[:] = [part for part in parts if type(part).__name__ == "At"] + [Plain(event.message_str)]

    def __init__(self, address, bot, context):
        super().__init__(address, bot, context)
        self._next_bot_probe = 0
        self._ingress_adapter = None
        self._detach_ingress = None
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
        if adapter is not self._ingress_adapter:
            if self._detach_ingress is not None:
                self._detach_ingress()
            self._detach_ingress = attach_native_ingress(adapter, self.context.private_dir)
            self._ingress_adapter = adapter
        if self.retractions is not None:
            self.retractions.attach(self.context.platform_instance())

    async def stop(self):
        if self._detach_ingress is not None:
            self._detach_ingress()
            self._detach_ingress = None
            self._ingress_adapter = None
        if self.retractions is not None:
            self.retractions.detach()
        await super().stop()

    def track_inbound(self, event, message):
        if self.retractions is None:
            return True
        self.retractions.attach(self.context.platform_instance())
        return self.retractions.track(event, message)

    async def reconcile_retractions(self, event, message, *, force=False):
        if self.retractions is not None:
            await self.retractions.reconcile(event, message, force=force)

    def recalled_inputs(self, inputs):
        return bool(self.retractions and self.retractions.has_recalled(inputs))

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

    async def refresh_native_access(self, event, message, identities):
        """Grant private member access from the authenticated installation tenant."""
        room = message.conversation
        if room.kind is not ChatKind.PRIVATE:
            return None
        evidence = event.get_extra("_ffa_lark_organization")
        adapter = self.context.platform_instance()
        if (isinstance(evidence, tuple) and len(evidence) == 5
                and isinstance(evidence[1], str) and evidence[1]
                and evidence[0] == getattr(adapter, "appid", None)
                and evidence[1] == evidence[2] and evidence[3] == "user"
                and evidence[4] == message.sender.subject
                and room.provider == self.address and room.bot == self.bot
                and await self.healthy()):
            identities.set_native_access(message.sender, room, allowed=True)
            await asyncio.to_thread(identities.admit_native_member, message.sender, room)
        # Existing explicit bindings retain their normal authorization path.
        return True

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
