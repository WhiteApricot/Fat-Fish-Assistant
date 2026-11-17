"""SnowLuma and OneBot capabilities exposed to the communication runtime."""
import asyncio
import logging

from fat_fish.communication.model import Capability, ChatKind, MessageRetraction
from fat_fish.adapters.astrbot_communication import AstrBotCommunication, ONEBOT_NAME, convert
from fat_fish.adapters.onebot_retraction import OneBotRetractions, qq_id


logger = logging.getLogger(__name__)


class OneBotCommunication(AstrBotCommunication):
    native_platform = ONEBOT_NAME
    requires_native_access = True

    capabilities = AstrBotCommunication.capabilities | {Capability.RETRACT}

    def __init__(self, address, bot, context):
        super().__init__(address, bot, context)
        self.retractions = (OneBotRetractions(address, bot, context)
                            if context and context.private_dir and context.retract_native else None)

    async def start(self):
        await super().start()
        if self.retractions and self.retractions.detach is None:
            self.retractions.attach(self.context.platform_instance())

    async def stop(self):
        if self.retractions and self.retractions.detach:
            self.retractions.detach()
            self.retractions.detach = None
        await super().stop()

    def track_inbound(self, event, message):
        return not self.retractions or self.retractions.track(message)

    def is_recalled(self, message):
        return bool(self.retractions and self.retractions.is_recalled(
            message.conversation, message.sender.subject, message.message_id))

    def recalled_inputs(self, inputs):
        return any(self.is_recalled(item.message) for item in inputs)

    def filter_recalled_history(self, room, inputs, messages):
        return self.retractions.filter_history(room, inputs, messages) if self.retractions else messages

    async def reconcile_retractions(self, event, message, *, force=False):
        if self.retractions:
            await self.retractions.reconcile(message)

    def convert_retraction(self, notice):
        if not isinstance(notice, MessageRetraction):
            raise ValueError("Expected a correlated OneBot recall")
        return notice

    async def verify_retraction(self, notice):
        return bool(self.retractions and notice in self.retractions.verified and await self.healthy())

    async def send_friend_notice(self, event, message, text):
        raw = getattr(event.message_obj, "raw_message", None)
        args = {}
        if isinstance(raw, dict) and raw.get("sub_type") == "group":
            sender = raw.get("sender")
            group = qq_id(sender.get("group_id")) if isinstance(sender, dict) else None
            if (raw.get("post_type") == "message" and raw.get("message_type") == "private"
                    and qq_id(raw.get("self_id")) == self.bot
                    and qq_id(raw.get("user_id")) == message.sender.subject and group):
                args["group_id"] = int(group)
        await event.bot.call_action(
            "send_private_msg", user_id=int(message.sender.subject), self_id=int(self.bot),
            message=[{"type": "text", "data": {"text": text}}], **args,
        )

    async def refresh_native_access(self, event, message, identities):
        room, account = message.conversation, message.sender
        raw = getattr(event.message_obj, "raw_message", None)
        if room.kind is ChatKind.PRIVATE:
            try:
                friend = account.subject in await self.friends(event)
            except Exception as exc:
                logger.warning("QQ friendship lookup failed (%s)", type(exc).__name__)
                identities.set_native_access(account, room, allowed=False)
                return None
            identities.set_native_access(account, room, allowed=friend)
            return friend
        sender = raw.get("sender") if isinstance(raw, dict) else None
        is_moderator = (isinstance(sender, dict) and raw.get("post_type") == "message"
                        and raw.get("message_type") == "group"
                        and str(raw.get("group_id")) == room.peer
                        and str(raw.get("self_id")) == self.bot
                        and str(sender.get("user_id")) == account.subject
                        and sender.get("role") in {"owner", "admin"})
        if is_moderator:
            try:
                is_moderator = await self._group_role(event.bot, room, account.subject) in {"owner", "admin"}
            except Exception as exc:
                logger.warning("QQ group role lookup failed (%s)", type(exc).__name__)
                is_moderator = False
        identities.set_native_access(account, room, allowed=is_moderator)
        return None

    async def _group_role(self, bot, room, subject):
        native = await asyncio.wait_for(bot.call_action(
            "get_group_member_info", group_id=int(room.peer), user_id=int(subject),
            no_cache=True, self_id=int(self.bot)), 3)
        if (not isinstance(native, dict) or str(native.get("user_id")) != subject
                or str(native.get("group_id", room.peer)) != room.peer
                or native.get("role") not in {"owner", "admin", "member"}):
            raise ValueError("Invalid QQ group membership evidence")
        return native["role"]

    async def refresh_delivery_access(self, account, room, identities, *, delegated=False):
        adapter = self.context.platform_instance()
        if adapter is None:
            raise ConnectionError("QQ adapter is unavailable")
        bot = adapter.get_client()
        if room.kind is ChatKind.PRIVATE:
            allowed = account.subject in await self._friends(bot)
        elif delegated:
            allowed = await self._group_role(bot, room, account.subject) in {"owner", "admin"}
        else:
            return
        identities.set_native_access(account, room, allowed=allowed)

    async def group_members(self, event, message):
        from fat_fish.adapters.group_platform import GroupPlatform

        if not hasattr(self, "_groups"):
            self._groups = GroupPlatform()
        return await self._groups.roster(message.conversation.peer, event.bot)

    async def friends(self, event):
        return await self._friends(event.bot)

    async def _friends(self, bot):
        rows = await asyncio.wait_for(bot.call_action("get_friend_list", self_id=int(self.bot)), 5)
        if not isinstance(rows, list):
            raise ValueError("Malformed friend list")
        if any(not isinstance(row, dict) or not str(row.get("user_id", "")).isdecimal() for row in rows):
            raise ValueError("Malformed friend list entry")
        return tuple(str(row["user_id"]) for row in rows)

    async def recall_message(self, event, message_id):
        if not str(message_id).lstrip("-").isdecimal():
            raise ValueError("Invalid OneBot message ID")
        return await event.bot.call_action("delete_msg", message_id=int(message_id))

    async def resolve_forward(self, event, ident):
        return await event.bot.call_action("get_forward_msg", id=ident)

    async def prepare_media(self, event, message, allowed, author_allowed):
        from fat_fish.integrations.forwards import recover_forward_components

        recover_forward_components(event)
        parts = event.get_messages()
        if message.reply_to:
            quote, images = await self.quote_context(event, message, author_allowed)
            if not quote or not allowed():
                raise PermissionError("Quote could not be verified in this conversation")
            event.set_extra("_ffa_quoted_context", quote)
            if images:
                from astrbot.api.message_components import Image

                present = {getattr(part, "url", None) or getattr(part, "file", None) for part in parts}
                parts.extend(Image(file=ref, url=ref) for ref in dict.fromkeys(images) if ref not in present)

    async def quote_context(self, event, message, author_allowed):
        if not message.reply_to:
            return "", []
        from fat_fish.adapters.qq_quote import resolve_quote_thread

        for part in event.get_messages():
            if type(part).__name__ == "Reply":
                room = message.conversation

                def message_allowed(ident, author):
                    return not self.retractions or not self.retractions.is_recalled(room, author, ident)

                return await resolve_quote_thread(event, part, author_allowed=author_allowed,
                                                  message_allowed=message_allowed)
        return "", []
