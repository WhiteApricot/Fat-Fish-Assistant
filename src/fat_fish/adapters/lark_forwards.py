"""Read only the merge-forward tree attached to an authenticated Lark event."""
import asyncio
from collections import defaultdict
from html import escape
from itertools import islice
import json

from fat_fish.integrations.forwards import MARKER

MAX_DEPTH = 4
MAX_NODES = 500
MAX_TEXT = 32_000
MAX_REQUESTS = 12
MEDIA = {"image": "图片", "audio": "语音", "file": "文件", "media": "视频", "sticker": "表情"}


def message_type(event):
    return getattr(getattr(event.message_obj, "raw_message", None), "message_type", None)


async def get_items(api, message_id, timeout):
    from lark_oapi.api.im.v1 import GetMessageRequest

    try:
        response = await asyncio.wait_for(api.aget(GetMessageRequest.builder().message_id(message_id).build()), timeout)
        if response.success() and response.data:
            return response.data.items or ()
    except (OSError, RuntimeError, ValueError, AttributeError, TimeoutError):
        pass
    return ()


def _body(row):
    try:
        content = json.loads(row.body.content)
    except (AttributeError, TypeError, ValueError):
        return ""
    if not isinstance(content, dict):
        return ""
    if row.msg_type == "text" and isinstance(content.get("text"), str):
        text = content["text"]
        for mention in row.mentions or ():
            if mention.key and mention.name:
                text = text.replace(mention.key, "@" + mention.name)
        return text
    if row.msg_type != "post":
        return ""
    lines = [content["title"]] if isinstance(content.get("title"), str) else []
    for line in content.get("content") or ():
        if isinstance(line, list):
            lines.append("".join(
                part.get("text") if isinstance(part.get("text"), str)
                else "[转发内图片不可读取]" if part.get("tag") == "img"
                else "[转发内资源不可读取]" if part.get("tag") in {"media", "file", "audio"}
                else ""
                for part in line if isinstance(part, dict)
            ))
    return "\n".join(lines)


async def read_forward(event, *, quoted, author_allowed, allowed, items=None):
    """Use only the native event envelope to select a root; never accept a prompt-supplied ID."""
    raw = getattr(event.message_obj, "raw_message", None)
    chat = getattr(raw, "chat_id", None)
    root_id = getattr(raw, "parent_id" if quoted else "message_id", None)
    if (not isinstance(chat, str) or not chat.startswith("oc_") or not isinstance(root_id, str)
            or not root_id.startswith("om_") or (not quoted and message_type(event) != "merge_forward")):
        return ""
    api = getattr(getattr(getattr(event, "bot", None), "im", None), "v1", None)
    if api is None or not allowed():
        return ""

    lines = [MARKER, "<飞书合并转发资料>"]
    size = sum(len(line) + 1 for line in lines)
    limited = False
    seen = set()
    labels = {}
    children = defaultdict(list)
    indexed = set()
    requests = 0 if items is not None else 1

    def index(rows):
        nonlocal limited
        for number, row in enumerate(islice(rows, MAX_NODES + 2)):
            if number > MAX_NODES:
                limited = True
                break
            ident = row.message_id
            parent = row.upper_message_id
            key = (parent, ident)
            if key in indexed:
                continue
            if len(indexed) >= MAX_NODES + 1:
                limited = True
                break
            indexed.add(key)
            if parent and ident:
                children[parent].append(row)

    def add(depth, value):
        nonlocal size, limited
        line = ("  " * (depth - 1)) + value
        budget = MAX_TEXT - size - 100
        if len(line) + 1 > budget:
            if budget > 4:
                lines.append(line[:budget - 1])
            limited = True
            size = MAX_TEXT - 100
            return
        lines.append(line)
        size += len(line) + 1

    async def visit(parent, depth):
        nonlocal requests
        for row in children.get(parent, ()):
            if not allowed():
                raise PermissionError("Forward access revoked")
            if len(seen) >= MAX_NODES or size >= MAX_TEXT - 100:
                return
            ident = row.message_id
            if ident in seen:
                add(depth, "[部分记录重复或形成循环]")
                continue
            seen.add(ident)
            if row.deleted:
                add(depth, "[该消息已撤回]")
                continue
            sender = row.sender
            # Forward copies retain their source chat ID. The authenticated
            # GetMessage response and upper_message_id establish their scope.
            if not sender or not sender.id or not author_allowed(sender.id):
                add(depth, "[该消息的来源不可读取]")
                continue
            label = labels.setdefault(sender.id, f"发言者 {len(labels) + 1}")
            if row.msg_type == "merge_forward":
                add(depth, f"{label}｜内层合并转发")
                if depth >= MAX_DEPTH:
                    add(depth, "[已达嵌套深度上限]")
                    continue
                if not children.get(ident) and requests < MAX_REQUESTS:
                    requests += 1
                    # Some responses already include the flattened subtree.
                    # Fetch only a known child when its subtree is missing.
                    subtree = (await get_items(api.message, ident, 1.5))[:MAX_NODES + 1]
                    verified = any(
                        item.message_id == ident and item.msg_type == "merge_forward"
                        and item.upper_message_id in (None, "", parent) and item.chat_id == row.chat_id
                        and item.sender and item.sender.id == sender.id and not item.deleted
                        for item in subtree
                    )
                    if verified:
                        # Index only this subtree. A disconnected row in a
                        # secondary response cannot attach to another branch.
                        pending = {ident}
                        for _ in range(MAX_DEPTH - depth):
                            descendants = [item for item in subtree if item.upper_message_id in pending]
                            index(descendants)
                            pending = {item.message_id for item in descendants} - seen
                            if not pending:
                                break
                if children.get(ident):
                    await visit(ident, depth + 1)
                else:
                    add(depth + 1, "[内层消息未返回或不可读取]")
            elif row.msg_type in MEDIA:
                add(depth, f"{label}｜[转发内{MEDIA[row.msg_type]}的资源不可读取]")
            else:
                body = _body(row)
                add(depth, f"{label}｜" + (escape(body, quote=False) if body else "[正文不可读取]"))

    try:
        async with asyncio.timeout(6):
            if items is None:
                items = await get_items(api.message, root_id, 2)
            root = next((row for row in islice(items, MAX_NODES + 1)
                         if row.message_id == root_id and row.chat_id == chat), None)
            if (not root or root.msg_type != "merge_forward" or root.deleted
                    or not root.sender or not root.sender.id or not author_allowed(root.sender.id)):
                return ""
            seen.add(root_id)
            index(items)
            if children.get(root_id):
                await visit(root_id, 1)
            else:
                add(1, "[平台未返回子消息]")
    except TimeoutError:
        add(1, "[读取超时，以下仅为已取得的部分]" if len(lines) == 2 else "[其余记录读取超时]")
    if not allowed():
        raise PermissionError("Forward access revoked")
    if limited or len(seen) >= MAX_NODES:
        lines.append("[已达读取上限，其余记录未显示]")
    return "\n".join((*lines, "</飞书合并转发资料>"))
