"""SnowLuma and OneBot capabilities exposed to the communication runtime."""
from fat_fish.adapters.astrbot_communication import AstrBotCommunication, ONEBOT_NAME


class OneBotCommunication(AstrBotCommunication):
    native_platform = ONEBOT_NAME

    async def group_members(self, event, message):
        from fat_fish.adapters.group_platform import GroupPlatform

        if not hasattr(self, "_groups"):
            self._groups = GroupPlatform()
        return await self._groups.roster(message.conversation.peer, event.bot)

    async def friends(self, event):
        import asyncio

        rows = await asyncio.wait_for(event.bot.call_action("get_friend_list"), 5)
        if not isinstance(rows, list):
            raise ValueError("Malformed friend list")
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
                return await resolve_quote_thread(event, part, author_allowed=author_allowed)
        return "", []
