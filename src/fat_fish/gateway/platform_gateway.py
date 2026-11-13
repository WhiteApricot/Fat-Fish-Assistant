"""AstrBot ingress for registered communication providers."""
import asyncio
from dataclasses import replace
from collections import OrderedDict, deque
import json
import logging
import re
import secrets
import sqlite3
from pathlib import Path
import time
from contextlib import nullcontext

from fat_fish.adapters.astrbot_communication import convert
from fat_fish.adapters.group_media import GroupMedia
from fat_fish.integrations.web_chat import WEB_PROGRESS_TYPE
from fat_fish.communication.model import Account, Capability, ChatKind, Conversation, MessageRetraction, ProviderId
from fat_fish.communication.registry import ProviderContext, ProviderRegistry
from fat_fish.communication.runtime import CommunicationRuntime
from fat_fish.core.identity import IdentityDirectory, Role
from fat_fish.memory.experience import contains_secret, sensitive_information
from fat_fish.participation.participation import DEFAULT_COOLDOWN, Participation
from fat_fish.persona.chat_settings import ChatSettings
from fat_fish.commands.confirmation import PendingActions
from fat_fish.commands.intents import ROUTES
from fat_fish.commands.natural_language import NaturalLanguageManager
from fat_fish.commands.command_help import platform_help
from fat_fish.commands.registry import Command, CommandRegistry
from fat_fish.reminders.dispatch import ReminderAccess
from fat_fish.memory.gateway_bridge import GatewayMemory
from fat_fish.gateway.media_ingress import UnsupportedMedia, prepare_media


IDENTITY_REQUEST = re.compile(r"^/?ffa-(owner|invite|link|unlink)(?:\s|$)", re.I)
logger = logging.getLogger(__name__)


class PlatformGateway:
    def __init__(self, *, private):
        self.path = Path(private) / "instance-runtime"
        self.context = None
        self.reminders = None
        self.memory_bridge = GatewayMemory(self)
        self.chat_settings = ChatSettings(self.path.with_name("ffa-chat-settings.json"))
        self.runtime = None
        self.registry = ProviderRegistry(self.path.with_name("ffa-communication.json"))
        self._stopping = False
        self.participation = Participation()
        self.group_media = GroupMedia()
        self.participation.salt = secrets.token_hex(32)
        self.pending = PendingActions()
        self.commands = CommandRegistry({
            **dict.fromkeys(("persona", "privatepersona", "grouppersona", "globalpersona",
                             "personal", "webresearch", "naturalcommand", "groupsettings"),
                            self._settings_command),
            "role": self._role_command,
            "globaladmin": self._role_command,
            "blacklist": self._role_command,
            "confirm": self._role_command,
            "cancel": self._role_command,
            "whitelist": self._room_command,
            "new": self._new_command,
            "history": self._history_command,
            "clarify": self._clarify_command,
            "help": self._help_command,
            "groupmode": self._group_mode_command,
            "undo": self._undo_command,
            "reminder": self._reminder_command,
            "memory": self._memory_command,
            "groupmemory": self._memory_command,
        })
        self.natural_language = NaturalLanguageManager(self.commands)
        self._room_signatures = {}
        self._command_receipts = OrderedDict()
        self._reminder_undo = OrderedDict()
        self._admin_undo = OrderedDict()

    @property
    def reminder_runtime(self):
        return self.communication()

    def _recent_commands(self, key):
        cutoff = time.monotonic() - 1800
        return [receipt for recorded, receipt in self._command_receipts.get(key, ()) if recorded > cutoff]

    def _record_command(self, key, words, result):
        name = words[0].removeprefix("/")
        if name in {"history", "clarify", "cancel"} or "/confirm " in result:
            return
        label = name
        if len(words) > 1 and words[1] in ROUTES.get(name, {}):
            label += " " + words[1]
        if name in {"memory", "groupmemory", "persona", "globalpersona", "privatepersona", "grouppersona"}:
            result = "[已返回回执，记忆正文或自定义人格内容已隐藏]"
        elif contains_secret(result):
            result = "[已返回回执，敏感内容已隐藏]"
        history = self._command_receipts.setdefault(key, deque(maxlen=8))
        history.append((time.monotonic(), f"{label}：{result[:800]}"))
        self._command_receipts.move_to_end(key)
        if len(self._command_receipts) > 512:
            self._command_receipts.popitem(last=False)

    async def _participate(self, message, admission, event):
        room = message.conversation
        if room.kind != ChatKind.GROUP:
            return True
        directory = self.communication().identities
        signature = await asyncio.to_thread(directory.room_membership_signature, room)
        if self._room_signatures.get(room.storage_key) != signature:
            self.participation.rooms.pop((room.storage_key, 0), None)
            self._room_signatures[room.storage_key] = signature
            if len(self._room_signatures) > 256:
                oldest = next(iter(self._room_signatures))
                self._room_signatures.pop(oldest)
                self.participation.rooms.pop((oldest, 0), None)
        if message.text.lstrip().startswith("/"):
            return room.bot in message.mentions
        text = event.message_str
        if event.get_extra("_ffa_material", False) or sensitive_information(text):
            return room.bot in message.mentions
        speaker = self.participation.label(room.storage_key, admission.principal_id)
        self.participation.capture(room.storage_key, 0, speaker, text)
        def current_group():
            return directory.room_membership_signature(room) == signature

        event.set_extra("_ffa_canonical_group_current", current_group)
        if admission.mode != "smart" or room.bot in message.mentions:
            event.set_extra("_ffa_canonical_group_context", self.participation.context(room.storage_key, 0))
            return True
        resolve_provider = getattr(self.context, "get_using_provider_async", None)
        try:
            provider = await resolve_provider(event.unified_msg_origin) if resolve_provider else None
        except (AttributeError, TypeError, RuntimeError, OSError):
            provider = None
        if provider is None:
            return False
        context = await self.participation.choose(room.storage_key, 0,
                                                  self.chat_settings.read().get("cooldowns", {}).get(room.storage_key, DEFAULT_COOLDOWN),
                                                  quote_context=event.get_extra("_ffa_quoted_context", ""),
                                                  provider=provider)
        if context and current_group():
            event.set_extra("_ffa_canonical_group_context", context)
            event.set_extra("_ffa_canonical_wake", True)
            return True
        return False

    def _record_group_reply(self, message, admission, event, text):
        room = message.conversation
        if (room.kind is not ChatKind.GROUP or admission.mode != "smart"
                or event.get_extra("_ffa_reply_is_command") or sensitive_information(text)):
            return
        current = event.get_extra("_ffa_canonical_group_current")
        if current is None or not current():
            return
        state = self.participation.capture(room.storage_key, 0, "__bot__", text or "[Bot已回复图片或其他内容]")
        state["reply_to"] = self.participation.label(room.storage_key, admission.principal_id)
        state["last"] = self.participation.clock()
        if not event.get_extra("_ffa_canonical_wake"):
            state["followups"] = 0

    async def _settings_command(self, runtime, message, admission, words, event):
        return self.chat_settings.command(event, message, admission, runtime.permissions,
                                          event.get_extra("_ffa_platform_authorize"))

    async def _new_command(self, runtime, message, admission, words, event):
        manager = getattr(self.context, "conversation_manager", None)
        if len(words) != 1 or message.conversation.kind is not ChatKind.PRIVATE:
            return "请在私聊发送 /new 开启新对话。"
        if manager is None or not event.get_extra("_ffa_platform_authorize")():
            return "当前无法开启新对话。"
        await manager.new_conversation(event.unified_msg_origin)
        return "已开启新对话。"

    async def _history_command(self, runtime, message, admission, words, event):
        receipts = self._recent_commands((admission.principal_id, message.conversation.storage_key))
        if words[1:] == ["all"]:
            reply = "\n".join(receipts)
        elif len(words) == 1:
            reply = "上一条指令 /" + receipts[-1] if receipts else ""
        else:
            reply = "用法：/history [all]"
        return reply or "当前会话没有可查询的近期指令回执"

    async def _clarify_command(self, runtime, message, admission, words, event):
        return "请明确要执行的操作、目标和范围。"

    async def _help_command(self, runtime, message, admission, words, event):
        if words[1:] in (["memory"], ["groupmemory"]):
            await self._memory_command(runtime, message, admission, words, event)
            return None
        return platform_help(message.conversation.kind is ChatKind.PRIVATE, admission, words[1:],
                             reminders=self.reminders is not None, registry=self.commands)

    async def _group_mode_command(self, runtime, message, admission, words, event):
        if message.conversation.kind is not ChatKind.GROUP or not runtime.permissions.can_moderate(admission):
            return "仅 L2/L3 可在授权群设置本群行为"
        if len(words) == 1:
            return f"本群模式：{admission.mode}；/groupmode mention|smart"
        modes = {"mention": "mention", "@": "mention", "@触发": "mention",
                 "smart": "smart", "智能": "smart", "智能插话": "smart"}
        if len(words) != 2 or words[1] not in modes:
            return "格式：/groupmode @触发|智能插话"
        actor = (admission.principal_id, message.sender.storage_key)
        code = self.pending.issue(actor, message.conversation.storage_key, "groupmode", (modes[words[1]],))
        return f"90秒内发送 /confirm {code} 确认本群模式变更"

    async def _undo_command(self, runtime, message, admission, words, event):
        if words[1:] in (["role"], ["globaladmin"]):
            return await self._undo_admin(runtime, message, admission, words[1])
        if len(words) == 2 and words[1] in {"personal", "naturalcommand", "webresearch", "groupsettings"}:
            return self.chat_settings.undo_preference(words[1], message, admission, runtime.permissions,
                                                      event.get_extra("_ffa_platform_authorize"))
        key = message.sender.storage_key, message.conversation.storage_key
        entry = self._reminder_undo.pop(key, None) if len(words) == 1 or words[1:] == ["reminder"] else None
        if entry and entry[0] > time.monotonic() and self.reminders is not None:
            access = ReminderAccess(runtime, message.conversation)
            return await asyncio.to_thread(self.reminders.undo, message.sender.subject, access, entry[1])
        return "当前会话没有可撤销的近期提醒操作。"

    async def _undo_admin(self, runtime, message, admission, command):
        actor = (admission.principal_id, message.sender.storage_key)
        scope = message.conversation.storage_key
        item = self.pending.peek(actor, scope)
        if not item:
            key = actor, scope
            recent = self._admin_undo.get(key)
            if recent is None:
                return "当前会话没有可撤销的近期管理员授权，请明确指定账号和范围"
            expires, room, target, expected, previous_role = recent
            if command == "globaladmin" and room is not None:
                return "当前会话没有可撤销的近期管理员授权，请明确指定账号和范围"
            if expires <= time.monotonic():
                self._admin_undo.pop(key, None)
                return "近期授权已过期或权限配置已有变化，请明确指定账号和范围"
            try:
                if room is None:
                    if admission.role != Role.OWNER:
                        return "当前权限不足，未执行"
                    revision = await asyncio.to_thread(runtime.identities.authorization_revision)
                    if revision != expected:
                        raise ValueError("Authorization changed after the grant")
                    code = self.pending.issue(actor, scope, "global-admin-undo", (target, expected, previous_role))
                    return f"撤销刚才授予 {target.subject} 的全局管理员权限。90秒内发送 /confirm {code}"
                await asyncio.to_thread(runtime.identities.change_room_moderator, message.sender, room,
                                        target, grant=False, from_global=True, expected=expected)
            except (PermissionError, ValueError):
                return "近期授权已过期或权限配置已有变化，请明确指定账号和范围"
            self._admin_undo.pop(key, None)
            self.participation.rooms.pop((room.storage_key, 0), None)
            return f"已撤销 {target.subject} 在群 {room.peer} 的显式Bot管理员授权"
        _, operation, _, _ = item
        if operation not in {"scoped-moderator", "group-moderator", "global-admin", "global-admin-undo"}:
            return "当前待确认的是其他操作，管理员申请没有变更"
        if self.pending.consume(actor, scope, item[0]) is None:
            return "近期授权已过期或权限配置已有变化，请明确指定账号和范围"
        return "已取消刚才的管理员申请，权限没有变更"

    async def _reminder_command(self, runtime, message, admission, words, event):
        if self.reminders is None:
            return "当前未启用提醒服务。"
        access = ReminderAccess(runtime, message.conversation)
        try:
            reply = await asyncio.to_thread(self.reminders.command, event, words[1:], access)
        except ValueError:
            return "提醒时间、目标或内容无效。"
        if task := event.get_extra("_ffa_reminder_undo"):
            key = message.sender.storage_key, message.conversation.storage_key
            self._reminder_undo[key] = (time.monotonic() + 1800, task)
            self._reminder_undo.move_to_end(key)
            if len(self._reminder_undo) > 256:
                self._reminder_undo.popitem(last=False)
        return reply

    async def _memory_command(self, runtime, message, admission, words, event):
        if words[:2] == ["/memory", "reset"] or (
                message.conversation.kind is ChatKind.GROUP and words[:2] == ["/groupmemory", "reset"]):
            try:
                return self._memory_reset(event, message, admission, words)
            except PermissionError:
                return "当前范围未启用记忆或权限不足。"
        if not await self.memory_bridge.handle_preference(event):
            await self.memory_bridge.prepare(event)
            await self.memory_bridge.handle_command(event)
        return None

    async def _role_command(self, runtime, message, admission, words, event):
        command, *args = words
        if command not in {"/role", "/confirm", "/cancel", "/globaladmin", "/blacklist"}:
            return None
        if command == "/role" and args[:1] == ["global"]:
            command, args = "/globaladmin", args[1:]
        global_admin_request = command == "/globaladmin" and args[:1] in (["add"], ["remove"])
        if command == "/blacklist" and message.conversation.kind is ChatKind.GROUP:
            room = message.conversation
            if not runtime.permissions.can_moderate(admission):
                return "全局黑名单由主人在私聊管理；群黑名单由本群Bot管理员管理"
            if args in (["help"], ["帮助"]):
                return "/blacklist list：查看本群黑名单\n/blacklist add 账号：添加\n/blacklist remove 账号：移除"
            try:
                if not args or args == ["list"]:
                    entries = await asyncio.to_thread(runtime.identities.room_blocked_accounts, message.sender, room)
                    if not args:
                        return f"本群黑名单数量：{len(entries)}\n用 /blacklist help 查看操作"
                    return "本群黑名单：" + ("、".join(entries) or "暂无")
                if len(args) == 2 and args[0] in {"add", "remove"}:
                    target = Account(room.provider, args[1])
                    await asyncio.to_thread(runtime.identities.change_room_block, message.sender, room,
                                            target, blocked=args[0] == "add")
                    return "已更新本群黑名单。"
            except (PermissionError, ValueError):
                return "权限不足或目标账号无效。"
            return "用 /blacklist help 查看当前场景的格式"
        if command == "/globaladmin" and args[:1] in (["add"], ["remove"]):
            if admission.role != Role.OWNER:
                return "只有 Owner 可以任免全局管理员。"
            role = "admin" if args[0] == "add" else "member"
            args = ["grant", message.sender.provider.platform, message.sender.provider.instance, *args[1:], role]
            command = "/role"
        if command == "/blacklist" and args[:1] in (["add"], ["remove"]):
            role = "blocked" if args[0] == "add" else "member"
            args = ["grant", message.sender.provider.platform, message.sender.provider.instance, *args[1:], role]
            command = "/role"
        if command in {"/globaladmin", "/blacklist"}:
            if admission.role < Role.ADMIN or message.conversation.kind != ChatKind.PRIVATE:
                return "请在授权私聊使用 list、add 或 remove。"
            if args in (["help"], ["帮助"]):
                return f"{command} list · {command} add|remove 账号"
            if args != ["list"] and (command != "/blacklist" or args):
                return f"用法：{command} list|add|remove"
            wanted = Role.ADMIN if command == "/globaladmin" else Role.BLOCKED
            accounts = await asyncio.to_thread(runtime.identities.accounts_with_role, message.sender.provider, wanted)
            if not args:
                return f"全局黑名单数量：{len(accounts)}\n用 /blacklist help 查看操作"
            title = "全局管理员" if command == "/globaladmin" else "全局黑名单"
            return title + "：" + ("、".join(account.subject for account in accounts) or "暂无")
        scope = message.conversation.storage_key
        actor = (admission.principal_id, message.sender.storage_key)
        if command == "/role":
            if not args:
                return f"当前角色：{Role(admission.role).name}。"
            if args == ["undo"]:
                return await self._undo_admin(runtime, message, admission, "role")
            if args in (["help"], ["帮助"]):
                return ("/role list [群号] · /role set 账号 管理员|普通好友 [群号]\n"
                        "/role add|remove 账号 [群号] · /role global list|add|remove")
            if args[0] in {"add", "remove", "set", "list"} and (
                    event.get_extra("_ffa_natural") or message.conversation.kind is ChatKind.PRIVATE
                    or args[0] == "set" or len(args) > 2):
                return await self._scoped_role(runtime, message, admission, actor, scope, args)
            if message.conversation.kind == ChatKind.GROUP and args[:1] in (["add"], ["remove"]):
                if not runtime.permissions.can_moderate(admission) or len(args) != 2:
                    return "群成员授权需要本群管理员及明确的目标账号。"
                try:
                    target = Account(message.sender.provider, args[1])
                except ValueError:
                    return "目标账号无效。"
                code = self.pending.issue(actor, scope, "group-member", (target, args[0] == "add"))
                return f"请在 90 秒内发送 /confirm {code} 确认群授权变更。"
            if message.conversation.kind == ChatKind.GROUP and args[:1] in (["moderator-add"], ["moderator-remove"]):
                if len(args) != 2 or not runtime.permissions.is_global_admin(admission):
                    return "群管理员任免需要全局管理权限和明确的目标账号。"
                target = Account(message.sender.provider, args[1])
                code = self.pending.issue(actor, scope, "group-moderator", (target, args[0] == "moderator-add"))
                return f"请在 90 秒内发送 /confirm {code} 确认群管理员变更。"
            if args[:1] == ["list"] and message.conversation.kind == ChatKind.GROUP:
                if not runtime.permissions.can_moderate(admission):
                    return "当前账号无权查看群成员授权。"
                return await self._scoped_role(runtime, message, admission, actor, scope, args)
            if (message.conversation.kind != ChatKind.PRIVATE or admission.role < Role.ADMIN
                    or len(args) != 5 or args[0] != "grant" or args[-1] not in {"member", "admin", "blocked"}):
                return "授权用法：/role grant 平台 实例 账号 member|admin|blocked，需在获准的私聊确认。"
            try:
                target = Account(ProviderId(*args[1:3]), args[3])
            except ValueError:
                return "目标平台、实例或账号无效。"
            role = Role[args[-1].upper()]
            if role == Role.ADMIN and admission.role != Role.OWNER:
                return "只有 Owner 可以任免全局管理员。"
            code = self.pending.issue(actor, scope, "global-admin" if global_admin_request else "grant-role", (target, role))
            return f"请在 90 秒内发送 /confirm {code}，或使用 /cancel 取消。"
        if command == "/cancel":
            if args:
                return "取消操作不接受参数。"
            if self.pending.consume_latest(actor, scope) is None:
                return "当前没有有效的待确认操作。"
            return "已取消待确认操作。"
        pending = self.pending.consume(actor, scope, args[0]) if len(args) == 1 else None
        if not pending:
            return "没有匹配的待确认操作，或确认码已过期。"
        if pending[0] == "grant-role" and admission.role < Role.ADMIN:
            return "当前授权不足，权限未修改。"
        if pending[0] == "global-admin" and admission.role != Role.OWNER:
            return "只有主人可以管理全局管理员"
        if pending[0] == "global-admin-undo":
            target, expected, previous_role = pending[1]
            if admission.role != Role.OWNER:
                return "只有主人可以管理全局管理员"
            try:
                await asyncio.to_thread(runtime.identities.provision, message.sender, target, previous_role,
                                        expected=expected)
            except (PermissionError, ValueError):
                return "近期授权已过期或权限配置已有变化，请明确指定账号和范围"
            self._admin_undo.pop((actor, scope), None)
            return f"已撤销 {target.subject} 的全局管理员权限"
        if pending[0] == "unlink":
            try:
                await asyncio.to_thread(runtime.identities.unlink, message.sender, pending[1][0])
            except PermissionError:
                return "当前权限不足，账号未解绑。"
            return "已解绑目标账号。"
        if pending[0] == "reset-memory":
            bridge = self.memory_bridge
            access = bridge._access(event, message)
            if (access.scope,) != pending[1]:
                return "当前范围已变化，记忆未清除。"
            try:
                await asyncio.to_thread(bridge.memory().reset_scope, access)
            except PermissionError:
                return "当前权限不足，记忆未清除。"
            return "已清除当前范围的长期记忆。"
        if pending[0] == "group-moderator":
            target, grant = pending[1]
            try:
                await asyncio.to_thread(runtime.identities.change_room_moderator, message.sender,
                                        message.conversation, target, grant=grant)
            except PermissionError:
                return "当前授权不足，群管理员未修改。"
            return "已更新本群管理员。"
        if pending[0] == "scoped-moderator":
            room, target, grant = pending[1]
            try:
                revision = await asyncio.to_thread(runtime.identities.change_room_moderator, message.sender,
                                                   room, target, grant=grant, from_global=True)
            except (PermissionError, ValueError):
                return "当前授权不足，群管理员未修改。"
            self.participation.rooms.pop((room.storage_key, 0), None)
            if grant and revision is not None:
                self._save_admin_undo(actor, scope, room, target, revision)
            return f"已{'授予' if grant else '撤销'} {target.subject} 在群 {room.peer} 的Bot管理权限"
        if pending[0] == "group-member":
            target, grant = pending[1]
            try:
                await asyncio.to_thread(runtime.identities.change_room_member, message.sender,
                                        message.conversation, target, grant=grant)
            except PermissionError:
                return "当前授权不足，群成员权限未修改。"
            self.participation.rooms.pop((scope, 0), None)
            return "已更新本群成员授权。"
        if pending[0] == "room-admission":
            room, grant = pending[1]
            try:
                await asyncio.to_thread(runtime.identities.change_room_admission, message.sender, room, grant=grant)
            except PermissionError:
                return "当前权限不足，群准入未修改。"
            self.participation.rooms.pop((room.storage_key, 0), None)
            return "已更新群准入。"
        if pending[0] == "groupmode":
            try:
                await asyncio.to_thread(runtime.identities.change_room_mode, message.sender,
                                        message.conversation, pending[1][0])
            except PermissionError:
                return "仅 L2/L3 可在授权群设置本群行为"
            self.participation.rooms.pop((scope, 0), None)
            return "已更新本群的回复模式。"
        target, role = pending[1]
        try:
            if pending[0] == "global-admin" and role == Role.ADMIN:
                previous, revision = await asyncio.to_thread(runtime.identities.provision, message.sender, target, role,
                                                             capture_revision=True)
                if previous != Role.ADMIN:
                    self._save_admin_undo(actor, scope, None, target, revision, previous)
            else:
                await asyncio.to_thread(runtime.identities.provision, message.sender, target, role)
        except PermissionError:
            return "当前授权不足，权限未修改。"
        return "已更新目标 Principal 的角色。"

    def _save_admin_undo(self, actor, scope, room, target, revision, previous_role=None):
        key = actor, scope
        self._admin_undo[key] = time.monotonic() + 900, room, target, revision, previous_role
        self._admin_undo.move_to_end(key)
        if len(self._admin_undo) > 512:
            self._admin_undo.popitem(last=False)

    async def _scoped_role(self, runtime, message, admission, actor, scope, args):
        room = message.conversation
        operation = args[0]
        if operation == "list" and len(args) in {1, 2}:
            if len(args) == 1 and room.kind is ChatKind.PRIVATE:
                return "请指定要查询的群号。"
            try:
                target_room = room if len(args) == 1 else Conversation(
                    room.provider, room.bot, ChatKind.GROUP, args[1], args[1])
                members = await asyncio.to_thread(runtime.identities.room_moderators, message.sender, target_room)
            except (PermissionError, ValueError):
                return "当前账号无权查看群管理员。"
            return "本群Bot管理员：" + ("、".join(members) or "暂无")
        if not runtime.permissions.is_global_admin(admission):
            return "授权由主人管理；群管理员不能为任何群授予管理权限"
        if operation == "remove" and len(args) == 3 and args[2] == "all":
            try:
                target = Account(room.provider, args[1])
                affected = await asyncio.to_thread(runtime.identities.remove_room_moderator_grants,
                                                   message.sender, room.provider, room.bot, target)
            except (PermissionError, ValueError):
                return "当前权限不足或目标账号无效"
            for address in affected:
                self.participation.rooms.pop((address, 0), None)
            return f"已撤销该账号在 {len(affected)} 个群的Bot管理员身份"
        aliases = {"管理员": True, "admin": True, "l2": True, "2": True, "普通好友": False,
                   "好友": False, "friend": False, "default": False, "l1": False, "1": False}
        if operation == "set" and len(args) in {3, 4} and args[2].lower() in aliases:
            grant, subject, groups = aliases[args[2].lower()], args[1], args[3:]
        elif operation in {"add", "remove"} and len(args) in {2, 3}:
            grant, subject, groups = operation == "add", args[1], args[2:]
        else:
            return "用 /role help 查看当前场景的授权格式"
        if (not groups and room.kind is ChatKind.PRIVATE) or groups == ["all"]:
            return "请明确指定已批准的群号和有效账号。"
        try:
            target = Account(room.provider, subject)
            target_room = room if not groups or (room.kind is ChatKind.GROUP and groups[0] == room.peer) else Conversation(
                room.provider, room.bot, ChatKind.GROUP, groups[0], groups[0])
        except ValueError:
            return "请明确指定已批准的群号和有效账号。"
        code = self.pending.issue(actor, scope, "scoped-moderator", (target_room, target, grant))
        return f"90秒内发送 /confirm {code} 确认群管理员变更"

    async def _room_command(self, runtime, message, admission, words, event=None):
        if not runtime.permissions.is_global_admin(admission):
            return "群开放范围只由主人管理"
        if words[1:] in (["help"], ["帮助"]):
            return "/whitelist list：查看授权群\n/whitelist add 群号：添加\n/whitelist remove 群号：移除"
        if len(words) == 1 or words == ["/whitelist", "list"]:
            try:
                rooms = await asyncio.to_thread(runtime.identities.rooms_for_admin, message.sender)
            except PermissionError:
                return "当前权限不足。"
            if len(words) == 1:
                return f"授权群数量：{len(rooms)}\n用 /whitelist help 查看操作"
            return "授权群：" + ("、".join(rooms) or "暂无")
        if len(words) != 3 or words[1] not in {"add", "remove"}:
            return "用法：/whitelist add|remove 当前平台群号"
        original = message.conversation
        try:
            room = Conversation(original.provider, original.bot, ChatKind.GROUP, words[2], words[2])
        except ValueError:
            return "群号无效。"
        actor = (admission.principal_id, message.sender.storage_key)
        code = self.pending.issue(actor, original.storage_key, "room-admission", (room, words[1] == "add"))
        return f"请在 90 秒内发送 /confirm {code} 确认群准入变更。"

    def _memory_reset(self, event, message, admission, words):
        if len(words) != 2 or (message.conversation.kind != ChatKind.PRIVATE and not admission.room_moderator):
            return "只能在已授权的当前记忆范围申请重置。"
        bridge = self.memory_bridge
        access = bridge._access(event, message)
        access.require(write=True, moderate=access.scope.kind == "group")
        actor = (admission.principal_id, message.sender.storage_key)
        code = self.pending.issue(actor, message.conversation.storage_key, "reset-memory", (access.scope,))
        return f"请在 90 秒内发送 /confirm {code} 清除当前范围的记忆，或使用 /cancel 取消。"

    def communication(self):
        if self.runtime is None:
            self.runtime = CommunicationRuntime(
                IdentityDirectory(self.path.with_name("ffa-identities.sqlite3")),
                self.path.with_name("ffa-communication.seen.db"),
                on_delivery=self.memory_bridge.provider_sent,
                on_retraction=self.memory_bridge.provider_retracted,
            )
        return self.runtime

    async def _configured_provider(self, runtime, address, bot):
        slot = runtime.connections.get(address)
        if slot is not None:
            if slot.state == "start_failed" and slot.provider.bot == bot:
                await runtime.enable(address)
            return
        if self.context is not None:
            await self.registry.ensure(runtime, address, bot, self._provider_context(address))

    def _provider_context(self, address):
        native = self.context

        async def send_native(origin, message):
            if origin.partition(":")[0] != address.instance:
                raise PermissionError("Native route belongs to another provider instance")
            return await native.send_message(origin, message)

        return ProviderContext(
            lambda: native.get_platform_inst(address.instance), send_native,
            getattr(native, "get_using_stt_provider_async", None),
        )

    async def start_configured(self):
        self._stopping = False
        try:
            configured = self.registry.configured()
            if configured:
                runtime = self.communication()
                await asyncio.gather(*(self.registry.start(runtime, address, instance, self._provider_context(address))
                                       for address, instance in configured.items()))
        except (AttributeError, KeyError, OSError, PermissionError, TypeError, ValueError, sqlite3.Error) as exc:
            logger.warning("FFA provider initialization skipped (%s)", type(exc).__name__)

    async def stop_configured(self):
        self._stopping = True
        if self.runtime is not None:
            if await self.runtime.stop_all():
                self.runtime = None

    async def retract(self, provider, native_event):
        """Route a native withdrawal from a registered adapter to the verified runtime."""
        if self._stopping:
            raise PermissionError("Gateway is stopping")
        runtime = self.communication()
        slot = runtime.connections.get(provider)
        if slot is None or not slot.enabled:
            raise PermissionError("Inactive retraction provider")
        convert_retraction = getattr(slot.provider, "convert_retraction", None)
        if Capability.RETRACT not in slot.provider.capabilities or not callable(convert_retraction):
            raise NotImplementedError("Message retraction is unavailable")
        notice = convert_retraction(native_event)
        if not isinstance(notice, MessageRetraction) or notice.conversation.provider != provider:
            raise ValueError("Retraction provider mismatch")
        return await runtime.retract(notice)

    async def _identity_command(self, runtime, message, event):
        text = message.text.strip()
        match = IDENTITY_REQUEST.match(text)
        if match is None:
            return False
        event.stop_event()
        if message.conversation.kind is not ChatKind.PRIVATE:
            return True
        account = await runtime.verify_account(message)
        if account is None:
            return True
        args = text.split()[1:]
        command = match[1].lower()
        scope = "identity-command"
        if not runtime.seen.begin_delivery(scope, message.event_id):
            return True
        try:
            try:
                if command == "owner" and not args:
                    token = await asyncio.to_thread(runtime.identities.issue_owner_challenge, account)
                    response = "请在此私聊于 5 分钟内发送 /ffa-owner " + token
                elif command == "invite" and len(args) == 3:
                    target = Account(ProviderId(*args[:2]), args[2])
                    token = await asyncio.to_thread(runtime.identities.invite, account, account, target)
                    response = "请在目标账号的 Bot 私聊中发送 /ffa-link " + token
                elif command in {"owner", "link"} and len(args) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{43}", args[0]):
                    operation = runtime.identities.confirm_owner_challenge if command == "owner" else runtime.identities.confirm
                    await asyncio.to_thread(operation, account, args[0])
                    response = "账号验证已完成。"
                elif command == "unlink" and len(args) == 3:
                    target = Account(ProviderId(*args[:2]), args[2])
                    principal, role = await asyncio.to_thread(runtime.identities.resolve_principal, account)
                    target_id = await asyncio.to_thread(runtime.identities.principal_id, target)
                    if not principal or not target_id or (principal != target_id and role != Role.OWNER):
                        raise PermissionError("Unlink authorization required")
                    if role < Role.MEMBER:
                        raise PermissionError("Unlink requires an admitted account")
                    actor = (principal, account.storage_key)
                    code = self.pending.issue(actor, message.conversation.storage_key, "unlink", (target,))
                    response = "请在 90 秒内发送 /confirm " + code + " 确认解绑。"
                else:
                    response = "账号操作格式有误。"
            except (PermissionError, ValueError):
                response = "账号操作未完成，请核对授权、邀请和当前账号。"
            await runtime.identity_reply(message, account, lambda: event.send(event.plain_result(response)))
        except BaseException:
            runtime.seen.finish_delivery(scope, message.event_id, "unknown")
            raise
        runtime.seen.finish_delivery(scope, message.event_id, "completed")
        return True

    async def run(self, event, downstream):
        if self._stopping:
            event.stop_event()
            return None
        admission = None
        try:
            message = convert(event)
            runtime = self.communication()
            await self._configured_provider(runtime, message.conversation.provider, message.conversation.bot)
            if await self._identity_command(runtime, message, event):
                return None
            if runtime.accepts(message) and not await asyncio.to_thread(
                    runtime.identities.principal_id, message.sender):
                account = await runtime.verify_account(message)
                if account is not None:
                    await asyncio.to_thread(runtime.identities.observe, account)
            admission = runtime.admit(message)
            if admission is not None:
                slot = runtime.connections[message.conversation.provider]
                observe = getattr(slot.provider, "observe", None)
                if observe is not None:
                    observe(message.conversation, event.unified_msg_origin)
        except (AttributeError, KeyError, OSError, TypeError, ValueError, sqlite3.Error):
            if admission is not None:
                runtime.finish_receive(message, "retryable")
            admission = None
        if admission is None:
            event.stop_event()
            return None

        event.set_extra("_ffa_message_event", message)
        event.set_extra("_ffa_reply_is_command", message.text.lstrip().startswith("/"))
        if message.media:
            event.set_extra("_ffa_reply_window", (time.monotonic(), not message.text.strip()))
        event.set_extra("_ffa_platform_admission", admission)
        slot = runtime.connections[message.conversation.provider]
        receipt_key = admission.principal_id, message.conversation.storage_key
        original_send = event.send
        original_stream = getattr(event, "send_streaming", None)

        def allowed():
            current = runtime.authorize(message)
            return current is not None and current.principal_id == admission.principal_id

        event.set_extra("_ffa_platform_authorize", allowed)

        submitted = False
        completed = False
        sequence = 0
        pacing = getattr(slot.provider, "reply_pacing", None)
        pace = pacing(event, message) if pacing else None

        async def checked_send(result):
            nonlocal sequence
            sequence += 1
            if pace:
                await pace(sequence)

            if event.get_extra("_ffa_reply_superseded", False):
                return None

            from fat_fish.adapters.qq_reply_delay import WEB_PROGRESS_TYPE
            from fat_fish.integrations.web_search import clean_message, is_intermediate_message

            if hasattr(result, "chain") and getattr(result, "type", None) != WEB_PROGRESS_TYPE:
                if is_intermediate_message(event, result):
                    native_result = event.get_result()
                    result_type = getattr(native_result, "result_content_type", None)
                    if getattr(result_type, "name", None) != "GENERAL_RESULT":
                        return None
                    result = event.plain_result("这次回复没有生成成功，请稍后再试。")
                if clean_message(result) and not result.chain:
                    return None

            async def send(_):
                nonlocal submitted
                submitted = True
                response = await original_send(result)
                await self.memory_bridge.sent(event, result)
                self._record_group_reply(message, admission, event,
                                         result.get_plain_text() if hasattr(result, "get_plain_text") else "")
                return response

            return await runtime.reply(message, admission, sequence, send)

        async def checked_stream(generator, *args, **kwargs):
            nonlocal sequence
            sequence += 1
            if pace:
                await pace(sequence)

            async def send(authorize):
                streamed = False
                preview = ""

                def check_submission():
                    nonlocal submitted, streamed
                    authorize()
                    submitted = streamed = True

                async def guarded():
                    nonlocal preview
                    async for part in generator:
                        check_submission()
                        if len(preview) < 1500 and isinstance(part, str):
                            preview += part[:1500 - len(preview)]
                        yield part

                guard = getattr(slot.provider, "guard_stream", None)
                with guard(event, check_submission) if guard else nullcontext():
                    result = await original_stream(guarded(), *args, **kwargs)
                if streamed:
                    await self.memory_bridge.sent(event, streamed_text=preview)
                    self._record_group_reply(message, admission, event, preview)
                return result

            return await runtime.reply(message, admission, sequence, send, check_on_submit=True)

        event.send = checked_send
        if original_stream is not None:
            event.send_streaming = checked_stream
        try:
            if not allowed():
                event.stop_event()
                completed = True
                return None
            pending = self.pending.peek((admission.principal_id, message.sender.storage_key),
                                        message.conversation.storage_key)
            private = message.conversation.kind is ChatKind.PRIVATE
            natural = await self.natural_language.classify(
                self.context, event, message, admission, runtime.permissions, self.chat_settings.read,
                recent=self._recent_commands(receipt_key), pending=pending[1] if pending else None,
                reminders=self.reminders,
            )
            if not allowed():
                event.stop_event()
                completed = True
                return None
            natural_words = None
            if natural and natural[0] != "clarify":
                name, args = natural
                if name in {"confirm", "cancel"}:
                    current = self.pending.peek((admission.principal_id, message.sender.storage_key),
                                                message.conversation.storage_key)
                    if current != pending:
                        event.stop_event()
                        await event.send(event.plain_result("待确认操作已变化，请重新核对。"))
                        completed = True
                        return None
                natural_words = ["/" + name, *args]
                text = " ".join(natural_words)
                message = replace(message, text=text)
                event.message_str = text
                event.set_extra("_ffa_natural", True)
                event.set_extra("_ffa_message_event", message)
                event.set_extra("_ffa_reply_is_command", True)
            if message.quoted_sender:
                author = Account(message.conversation.provider, message.quoted_sender)
                if (runtime.identities.resolve_principal(author)[1] == Role.BLOCKED
                        or runtime.identities.is_room_blocked(message.conversation, author)):
                    event.stop_event()
                    completed = True
                    return None
            words = natural_words or message.text.strip().split()
            undo_key = message.sender.storage_key, message.conversation.storage_key
            if words:
                words[0] = words[0].lower()
                if words[0] == "/comfirm":
                    words[0] = "/confirm"
                if words[0] == "/confirm" and len(words) == 1 and pending:
                    words.append(pending[0])
                if words in (["/memory", "cancel"], ["/groupmemory", "cancel"],
                             ["/memory", "confirm"]):
                    if pending and pending[1] == "reset-memory":
                        words = ["/cancel"] if words[1] == "cancel" else ["/confirm", pending[0]]
                if words[0] == "/undo":
                    persona_command = words[1] if words[1:] in (["persona"], ["globalpersona"],
                                                                ["privatepersona"], ["grouppersona"]) else None
                    if len(words) == 1:
                        recent_persona = self.chat_settings.latest_persona_undo(admission.principal_id, undo_key[1])
                        recent_setting = self.chat_settings.latest_preference_undo(admission.principal_id, undo_key[1])
                        reminder = self._reminder_undo.get(undo_key)
                        recent_reminder = (reminder[0] - 1800, "reminder") if reminder and reminder[0] > time.monotonic() else None
                        latest = max(filter(None, (recent_persona, recent_setting, recent_reminder)), default=None)
                        if latest and latest == recent_persona:
                            persona_command = latest[1]
                        elif latest and latest == recent_setting:
                            words = ["/undo", latest[1]]
                    if persona_command:
                        words = ["/" + persona_command, "undo"]
                        message = replace(message, text=" ".join(words))
                        event.message_str = message.text
                        event.set_extra("_ffa_message_event", message)

            async def command_reply(reply):
                await event.send(event.plain_result(reply))
                self._record_command(receipt_key, words, reply)

            if words and words[0].startswith("/") and self.commands.find(words[0][1:]) is not None:
                event.stop_event()
                try:
                    command = Command(words[0][1:], tuple(words[1:]))
                    current = runtime.authorize(message)
                    reply = await self.commands.dispatch(
                        command, current, allowed, builtin_args=(runtime, message, current, words, event))
                except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                    logger.warning("FFA management command failed (%s)", type(exc).__name__)
                    reply = "指令执行失败，不能确认是否完整生效；请检查本机日志后再试"
                if reply is not None:
                    await command_reply(reply)
                completed = True
                return None
            await self.memory_bridge.prepare(event)
            media = getattr(slot.provider, "prepare_media", None)
            def author_allowed(subject):
                if subject == message.conversation.bot:
                    return True
                try:
                    account = Account(message.conversation.provider, subject)
                except ValueError:
                    return False
                return (runtime.identities.resolve_principal(account)[1] != Role.BLOCKED
                        and not runtime.identities.is_room_blocked(message.conversation, account))

            try:
                if media is not None:
                    await media(event, message, allowed, author_allowed)
                if not private:
                    room = message.conversation.storage_key
                    epoch = await asyncio.to_thread(runtime.identities.room_membership_signature, message.conversation)
                    def media_allowed():
                        return allowed() and runtime.identities.room_membership_signature(message.conversation) == epoch

                    if not media_allowed():
                        raise PermissionError("Group media authorization changed")
                    self.group_media.observe(event, epoch, room=room,
                                             mentioned=message.conversation.bot in message.mentions)
                    if getattr(self.context, "get_using_stt_provider_async", None):
                        await self.group_media.transcribe(event, epoch, self.context, media_allowed, room=room)
                    if not media_allowed():
                        raise PermissionError("Group media authorization changed")
                if message.media or event.get_messages():
                    await prepare_media(event, allowed, forward_reader=getattr(self, "forward_reader", None),
                                        author_allowed=author_allowed,
                                        resolve_forward=getattr(slot.provider, "resolve_forward", None))
            except UnsupportedMedia as exc:
                event.stop_event()
                if allowed():
                    await event.send(event.plain_result(str(exc)))
                completed = True
                return None
            except (ValueError, PermissionError):
                event.stop_event()
                completed = True
                return None
            if not allowed() or not await self._participate(message, admission, event):
                event.stop_event()
                completed = True
                return None
            if not allowed():
                event.stop_event()
                completed = True
                return None
            if message.conversation.kind is ChatKind.GROUP:
                roster = getattr(slot.provider, "group_members", None)
                if roster:
                    members = await roster(event, message)
                    if members and allowed():
                        event.set_extra("_ffa_group_native_roles", json.dumps([
                            {"发言者": self.participation.label(message.conversation.storage_key, current.principal_id),
                             "昵称": member["name"], "平台群身份": member["role"]}
                            for subject, member in members.items()
                            if member["role"] in {"owner", "admin"}
                            and (current := runtime.permissions.conversation(
                                Account(message.conversation.provider, subject), message.conversation))
                        ], ensure_ascii=False))
            if not allowed():
                event.stop_event()
                completed = True
                return None
            self.chat_settings.prepare(event, message, admission)
            result = await downstream()
            completed = True
            return result
        except (ConnectionError, TimeoutError):
            slot.state = "unhealthy"
            event.stop_event()
            return None
        except asyncio.CancelledError:
            event.stop_event()
            raise
        finally:
            event.set_extra("_ffa_platform_authorize", None)
            event.send = original_send
            if original_stream is not None:
                event.send_streaming = original_stream
            state = ("completed" if completed else "unknown"
                     if submitted or event.get_extra("_ffa_mutating_tool_started") else "retryable")
            runtime.finish_receive(message, state)
