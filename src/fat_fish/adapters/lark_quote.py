"""Read an explicit Lark quote through the authenticated native SDK."""
import asyncio
import json

from fat_fish.gateway.quote_context import QUOTE_DEPTH, excerpt, with_neighbors


def _label(row, bot):
    return "机器人" if row.sender.sender_type == "app" or row.sender.id == bot else "用户"


def _text(row):
    try:
        content = json.loads(row.body.content)
    except (AttributeError, TypeError, ValueError):
        return ""
    if not isinstance(content, dict):
        return ""
    if isinstance(content.get("text"), str):
        return content["text"]
    if row.msg_type == "post":
        return "\n".join("".join(part.get("text", "") for part in line if isinstance(part, dict))
                         for line in content.get("content", ()) if isinstance(line, list))
    return ""


async def quote_context(event, message, author_allowed):
    from lark_oapi.api.im.v1 import GetMessageRequest, ListMessageRequest

    chat = getattr(event.message_obj.raw_message, "chat_id", None)
    if not isinstance(chat, str) or not chat.startswith("oc_"):
        return ""
    try:
        api = event.bot.im.v1.message
    except AttributeError:
        return ""

    async def fetch(ident, timeout):
        try:
            request = GetMessageRequest.builder().message_id(ident).build()
            response = await asyncio.wait_for(api.aget(request), timeout)
            if response.success() and response.data:
                return next((row for row in response.data.items or ()
                             if row.message_id == ident and row.chat_id == chat and row.sender
                             and row.sender.id and author_allowed(row.sender.id)), None)
        except (OSError, RuntimeError, ValueError, AttributeError, TimeoutError):
            pass
        return None

    parent = await fetch(message.reply_to, 3)
    if parent is None:
        return ""
    native = next((part for part in event.get_messages()
                   if type(part).__name__ == "Reply" and str(part.id) == message.reply_to), None)
    content = _text(parent) or getattr(native, "message_str", "") or "[非文字消息]"
    quote = excerpt(content)
    if not quote:
        return ""
    bot = message.conversation.bot

    async def ancestors():
        seen = {message.message_id, message.reply_to}
        rows = []
        ident = getattr(parent, "parent_id", None)
        try:
            async with asyncio.timeout(4):
                while ident and len(rows) < QUOTE_DEPTH - 1 and ident not in seen:
                    row = await fetch(ident, 2)
                    if row is None:
                        break
                    text = excerpt(_text(row) or "[非文字消息]", 600)
                    if not text:
                        break
                    seen.add(ident)
                    rows.append(f"<message>{_label(row, bot)}：{text}</message>")
                    ident = getattr(row, "parent_id", None)
        except TimeoutError:
            pass
        return list(reversed(rows)), seen, bool(ident)

    async def adjacent(start, end, order):
        if start >= end:
            return []
        request = (ListMessageRequest.builder().container_id_type("chat").container_id(chat)
                   .start_time(str(start)).end_time(str(end)).sort_type(order).page_size(24).build())
        try:
            response = await asyncio.wait_for(api.alist(request), 2)
            if response.success() and response.data:
                return [row for row in response.data.items or () if row.chat_id == chat and row.sender
                        and row.sender.id and author_allowed(row.sender.id)]
        except (OSError, RuntimeError, ValueError, AttributeError, TimeoutError):
            pass
        return []

    try:
        anchor = int(parent.create_time)
    except (ValueError, TypeError):
        chain, _, truncated = await ancestors()
        return with_neighbors(f"{_label(parent, bot)}：{quote}", (), (), ancestry=chain, incomplete=truncated)
    second = anchor // 1000
    earlier, later, (chain, seen, truncated) = await asyncio.gather(
        adjacent(max(0, second - 1800), second + 1, "ByCreateTimeDesc"),
        adjacent(second, min(second + 1800, int(message.timestamp) + 1), "ByCreateTimeAsc"),
        ancestors(),
    )
    before, after = {}, {}
    for row in (*earlier, *later):
        if row.message_id in seen:
            continue
        try:
            stamp = int(row.create_time)
        except (ValueError, TypeError):
            continue
        if stamp == anchor or stamp > int(message.timestamp * 1000):
            continue
        body = _text(row)
        if body:
            (before if stamp < anchor else after)[row.message_id] = (
                stamp, _label(row, bot), body)
    return with_neighbors(f"{_label(parent, bot)}：{quote}", [row[1:] for row in sorted(before.values())],
                          [row[1:] for row in sorted(after.values())], ancestry=chain, incomplete=truncated)
