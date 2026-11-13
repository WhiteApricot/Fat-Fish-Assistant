"""Prepare authorized AstrBot components on the shared chat path."""


class UnsupportedMedia(ValueError):
    """The native envelope cannot supply material for this message."""


UNSUPPORTED_MEDIA = frozenset({"Unknown", "Json", "Xml", "Poke", "Music", "Share"})


async def prepare_media(event, allowed, *, forward_reader=None, author_allowed=None, resolve_forward=None):
    if not allowed():
        raise PermissionError("Media access revoked")
    parts = event.get_messages()
    if any(type(part).__name__ in UNSUPPORTED_MEDIA for part in parts):
        raise UnsupportedMedia("当前消息包含暂不支持的媒体类型。")
    if author_allowed is not None:
        for part in parts:
            if type(part).__name__ == "Reply" and getattr(part, "chain", None):
                sender = getattr(part, "sender_id", None)
                if sender in (None, "", 0, "0") or not author_allowed(str(sender)):
                    raise PermissionError("Quote author is unavailable or blocked")
    forward_types = {"Forward", "Node", "Nodes"}
    if any(type(part).__name__ in forward_types for part in parts):
        from astrbot.api.message_components import Image, Plain

        if forward_reader is None or not forward_reader.available() or author_allowed is None:
            raise UnsupportedMedia("当前无法读取合并转发消息。")
        if resolve_forward is None and any(type(part).__name__ == "Forward" for part in parts):
            raise UnsupportedMedia("当前平台未提供可读取的转发内容。")

        class BlockedAuthors:
            def __contains__(self, subject):
                return not author_allowed(subject)

        material, images = await forward_reader.read(event, BlockedAuthors(), resolve_forward=resolve_forward)
        if not allowed():
            raise PermissionError("Forward access revoked")
        parts[:] = [part for part in parts if type(part).__name__ not in forward_types]
        parts.extend([Plain(material), *(Image(file=ref, url=ref) for ref in images)])
        event.message_str += "\n" + material
        event.message_obj.message_str = event.message_str
        event.set_extra("_ffa_material", True)
    if any(type(part).__name__ == "File" for part in parts):
        from astrbot.api.message_components import Plain
        from fat_fish.integrations.text_files import read_attachments
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
        except ImportError:
            temp_root = None
        else:
            temp_root = get_astrbot_temp_path()
        material = await read_attachments(parts, temp_root=temp_root)
        if not allowed():
            raise PermissionError("Attachment access revoked")
        parts[:] = [part for part in parts if type(part).__name__ != "File"] + [Plain(material)]
        event.message_str += "\n" + material
        event.message_obj.message_str = event.message_str
        event.set_extra("_ffa_material", True)
