import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fat_fish.adapters.astrbot_communication import QQ, convert
from fat_fish.communication.model import (
    Account, Capability, ChatKind, Conversation, MessageRetraction, NotSubmitted, Outgoing, ProviderId,
)
from fat_fish.core.identity import IdentityDirectory, Role
from fat_fish.gateway.platform_gateway import PlatformGateway


class At:
    def __init__(self, target):
        self.qq = target


class Reply:
    def __init__(self, ident, sender):
        self.id = ident
        self.sender_id = sender


class NativeBot:
    async def call_action(self, action):
        if action != "get_friend_list":
            raise AssertionError(action)
        return [{"user_id": 32}]


class NativeEvent:
    def __init__(self, platform="lark", instance="office", sender="32", bot="900",
                 group="", ident="42", text="你好", parts=(), raw=None, thread=None):
        self.platform, self.instance, self.sender, self.bot_id = platform, instance, sender, bot
        self.group, self.session_id = group, thread or group or sender
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id=ident, timestamp=1771000000,
                                           raw_message=raw or {})
        self.parts = list(parts)
        self.extra = {}
        self.bot = NativeBot()
        self.sent = []
        self.stopped = False

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return self.instance

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return self.bot_id

    def get_group_id(self):
        return self.group

    def get_messages(self):
        return self.parts

    def is_private_chat(self):
        return not self.group

    @property
    def unified_msg_origin(self):
        kind = "GroupMessage" if self.group else "FriendMessage"
        return f"{self.instance}:{kind}:{self.session_id}"

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    async def send(self, message):
        self.sent.append(message)


class ControlledProvider:
    capabilities = frozenset({Capability.TEXT, Capability.PUSH})

    def __init__(self, address, bot="900"):
        self.address = address
        self.bot = bot
        self.active = False
        self.fail = False
        self.deliveries = []

    async def start(self):
        if self.fail:
            raise ConnectionError("offline")
        self.active = True

    async def stop(self):
        self.active = False

    async def healthy(self):
        return self.active and not self.fail

    async def send_checked(self, target, message, allowed):
        if not allowed():
            raise NotSubmitted("revoked before submission")
        await self.send(target, message)

    async def send(self, target, message):
        if self.fail:
            raise ConnectionError("offline")
        self.deliveries.append((target, message))


class VerifiedProvider(ControlledProvider):
    verification_capabilities = frozenset({"private-message"})

    async def verify_account(self, event):
        if event.conversation.kind is ChatKind.PRIVATE and not event.reply_to and not event.media:
            return event.sender
        return None


class RetractionProvider(VerifiedProvider):
    capabilities = VerifiedProvider.capabilities | {Capability.RETRACT}

    def convert_retraction(self, native_event):
        conversation, subject, message_id = native_event
        return MessageRetraction(conversation, Account(self.address, subject), message_id)

    async def verify_retraction(self, notice):
        return self.active and not self.fail


def create_test_provider(address, bot, context, config):
    provider = VerifiedProvider(address, bot)
    provider.fail = config["fail"]
    return provider


class VerifiedIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gateway = PlatformGateway(private=Path(tmp.name))
        self.runtime = self.gateway.communication()
        self.ids = self.runtime.identities
        self.providers = (ProviderId("lark", "office"), QQ, ProviderId("synthetic", "third"))
        for address in self.providers:
            self.runtime.register(VerifiedProvider(address))
            await self.runtime.enable(address)
        self.sequence = 0

    async def event(self, account, text, *, group="", message_id=None):
        self.sequence += 1
        address = account.provider
        platform = "aiocqhttp" if address == QQ else address.platform
        event = NativeEvent(platform, address.instance, sender=account.subject, text=text,
                            group=group, ident=message_id or str(self.sequence))

        async def downstream():
            access = event.get_extra("_ffa_memory_access")
            event.recalled = self.gateway.memory_bridge.memory().context(access, text) if access else ""
            await event.send("response")

        await self.gateway.run(event, downstream)
        return event

    async def test_managed_gateway_and_reminder_share_delivery_boundary(self):
        from fat_fish.gateway.platform_gateway import PlatformGateway
        from fat_fish.reminders.dispatch import ReminderDispatch
        from fat_fish.reminders.reminders import ReminderStore

        gateway = PlatformGateway(private=self.gateway.path.parent)
        gateway.runtime = self.runtime
        gateway.reminders = ReminderStore(self.gateway.path.with_name("reminders.sqlite3"))
        owner = Account(self.providers[0], "31")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        native = NativeEvent("lark", owner.provider.instance, sender=owner.subject,
                             text="/remind add 10分钟后提醒我看鲸鱼")

        async def unexpected():
            self.fail("Reminder command escaped to the model")

        await gateway.run(native, unexpected)
        self.assertIn("已创建提醒", native.sent[0])
        row = gateway.reminders.rows()[0]
        self.assertEqual(row["creator_principal"], self.ids.principal_id(owner))
        dispatch = ReminderDispatch(gateway.reminders, self.runtime)
        await gateway.reminders.tick(dispatch.authorize, dispatch.send, now=row["due"] + 1)
        await gateway.reminders.tick(dispatch.authorize, dispatch.send, now=row["due"] + 1)
        provider = self.runtime.connections[owner.provider].provider
        self.assertEqual(len(provider.deliveries), 1)
        self.assertIn("看鲸鱼", provider.deliveries[0][1].text)
        self.assertEqual(self.runtime.seen.delivery("reply:" + convert(native).conversation.storage_key,
                                                   convert(native).event_id + ":1"), "submitted")

    async def test_reset_confirmation_is_bound_to_account_and_scope(self):
        owner = Account(self.providers[0], "31")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        other = Account(self.providers[1], "31")
        await self.bind(owner, other)
        await self.event(owner, "我喜欢观察蓝鲸")
        request = await self.event(owner, "/memory reset")
        code = request.sent[0].split("/confirm ")[1].split()[0]
        self.assertIn("没有匹配", (await self.event(other, "/confirm " + code)).sent[0])
        self.assertIn("已清除", (await self.event(owner, "/confirm " + code)).sent[0])
        self.assertIn("没有匹配", (await self.event(owner, "/confirm " + code)).sent[0])
        request = await self.event(owner, "/memory reset")
        self.assertIn("/confirm ", request.sent[0])
        self.assertEqual((await self.event(owner, "/memory cancel")).sent, ["已取消待确认操作。"])
        self.assertEqual((await self.event(owner, "/cancel")).sent, ["当前没有有效的待确认操作。"])

    async def test_owner_opens_mention_room_and_confirms_member_grant(self):
        owner = Account(self.providers[0], "31")
        member = Account(owner.provider, "35")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        self.ids.observe(member)
        self.ids.provision(owner, member, Role.MEMBER)
        reply = await self.event(owner, "/whitelist add 123")
        code = reply.sent[0].split("/confirm ")[1].split()[0]
        self.assertIn("已更新", (await self.event(owner, "/confirm " + code)).sent[0])
        room = Conversation(owner.provider, "900", ChatKind.GROUP, "123", "123")
        other = Conversation(owner.provider, "900", ChatKind.GROUP, "124", "124")
        self.assertEqual(self.runtime.permissions.conversation(owner, room).mode, "mention")
        self.assertIsNone(self.runtime.permissions.conversation(member, room))
        self.ids.change_room_member(owner, room, member, grant=True)
        self.assertIsNotNone(self.runtime.permissions.conversation(member, room, mentions=("900",)))
        self.assertIsNone(self.runtime.permissions.conversation(member, room, mentions=()))
        self.assertIsNone(self.runtime.permissions.conversation(member, other))

    async def test_global_admin_whitelist_is_revalidated_at_confirmation(self):
        owner, admin = (Account(self.providers[0], subject) for subject in ("31", "32"))
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        self.ids.observe(admin)
        self.ids.provision(owner, admin, Role.ADMIN)
        self.assertIn("授权群数量：0", (await self.event(admin, "/whitelist")).sent[0])
        request = await self.event(admin, "/whitelist add 123")
        code = request.sent[0].split("/confirm ")[1].split()[0]
        self.ids.provision(owner, admin, Role.MEMBER)
        self.assertIn("权限不足", (await self.event(admin, "/confirm " + code)).sent[0])
        self.assertEqual(self.ids.rooms_for_admin(owner), [])

    async def bind(self, actor, target):
        address = target.provider
        request = f"/ffa-invite {address.platform} {address.instance} {target.subject}"
        reply = await self.event(actor, request)
        token = reply.sent[0].split()[-1]
        self.assertIn("已完成", (await self.event(target, "/ffa-link " + token)).sent[0])
        return token

    async def test_non_qq_owner_and_transitive_bindings_recover_across_providers(self):
        owner, qq, third = (Account(address, "31") for address in self.providers)
        issued = await self.event(owner, "/ffa-owner")
        code = issued.sent[0].split()[-1]
        self.assertIn("未完成", (await self.event(qq, "/ffa-owner " + code)).sent[0])
        self.assertEqual((await self.event(owner, "/ffa-owner " + code, group="123")).sent, [])
        self.assertIn("已完成", (await self.event(owner, "/ffa-owner " + code)).sent[0])
        principal = self.ids.principal_id(owner)
        UUID(principal)
        self.assertIn("未完成", (await self.event(owner, "/ffa-owner " + code)).sent[0])
        await self.bind(owner, qq)
        token = await self.bind(qq, third)
        self.assertIn("未完成", (await self.event(third, "/ffa-link " + token)).sent[0])
        for account in (owner, qq, third):
            self.assertEqual(self.ids.resolve(account), (principal, Role.OWNER))
        first = await self.event(owner, "我最喜欢观赏鲸鱼")
        second = await self.event(qq, "鲸鱼")
        bridge = self.gateway.memory_bridge
        self.assertEqual(first.get_extra("_ffa_memory_access").principal, principal)
        self.assertEqual(second.get_extra("_ffa_memory_access").principal, principal)
        self.assertIn("观赏鲸鱼", second.recalled)
        room = Conversation(qq.provider, "900", ChatKind.GROUP, "123", "123")
        self.ids.set_room(owner, room, "open", [qq])
        self.assertNotIn("观赏鲸鱼", (await self.event(qq, "鲸鱼", group="123")).recalled)
        self.assertTrue(bridge.options()["reflect"])
        self.assertEqual(bridge.options()["embedding_space"], "local-bge")
        self.ids.unlink(third, owner)
        self.assertEqual(self.ids.resolve(owner), (None, Role.VISITOR))
        self.assertEqual(self.ids.resolve(qq), (principal, Role.OWNER))

    async def test_pending_principal_transitive_binding_and_role_grant(self):
        owner = Account(self.providers[2], "31")
        code = self.ids.issue_owner_challenge(owner)
        self.assertIn("已完成", (await self.event(owner, "/ffa-owner " + code)).sent[0])
        alice, qq, third = (Account(address, "48") for address in self.providers)
        event = await self.event(alice, "你好")
        self.assertEqual(event.sent, [])
        principal, role = self.ids.resolve(alice)
        UUID(principal)
        self.assertEqual(role, Role.VISITOR)
        await self.bind(alice, qq)
        await self.bind(qq, third)
        self.assertEqual(self.ids.resolve(third), (principal, Role.VISITOR))
        self.ids.provision(owner, qq, Role.MEMBER)
        self.assertEqual(self.ids.resolve(alice), (principal, Role.MEMBER))
        self.assertEqual((await self.event(third, "你好")).sent, ["response"])
        other = Account(self.providers[0], "49")
        await self.event(other, "你好")
        reply = await self.event(qq, f"/ffa-invite lark office {other.subject}")
        self.assertIn("未完成", reply.sent[0])
        self.assertNotEqual(self.ids.principal_id(other), principal)

    async def test_memory_pause_is_shared_only_by_the_same_principal_and_scope(self):
        owner = Account(self.providers[0], "31")
        code = self.ids.issue_owner_challenge(owner)
        await self.event(owner, "/ffa-owner " + code)
        qq = Account(QQ, "42")
        await self.bind(owner, qq)
        await self.event(owner, "我最喜欢观赏鲸鱼")
        self.assertIn("已暂停", (await self.event(qq, "/memory off")).sent[0])
        paused = await self.event(owner, "观赏鲸鱼")
        self.assertEqual(paused.recalled, "")
        self.assertIsNone(paused.get_extra("_ffa_memory_access"))
        room = Conversation(qq.provider, "900", ChatKind.GROUP, "123", "123")
        member = Account(QQ, "43")
        await self.event(member, "你好")
        self.ids.provision(owner, member, Role.MEMBER)
        self.ids.set_room(owner, room, "open", [qq, member])
        self.assertIn("无法修改", (await self.event(member, "/memory off", group="123")).sent[0])
        self.assertIsNone((await self.event(qq, "群默认关闭", group="123")).get_extra("_ffa_memory_access"))
        self.assertIn("已开启", (await self.event(qq, "/memory on", group="123")).sent[0])
        remembered = (await self.event(qq, "/groupmemory remember 本群观察蓝鲸", group="123")).sent[0]
        self.assertIn("已保存", remembered)
        fact_key = remembered.split()[1]
        self.assertIn("本群观察蓝鲸", (await self.event(qq, "/groupmemory search 蓝鲸", group="123")).sent[0])
        self.assertIn("未生效", (await self.event(member, "/groupmemory delete " + fact_key, group="123")).sent[0])
        self.assertIn("已删除", (await self.event(qq, "/groupmemory delete " + fact_key, group="123")).sent[0])
        self.assertIsNotNone((await self.event(qq, "群聊活动", group="123")).get_extra("_ffa_memory_access"))
        self.assertIn("已暂停", (await self.event(qq, "/memory off", group="123")).sent[0])
        self.assertIsNone((await self.event(member, "暂停期间", group="123")).get_extra("_ffa_memory_access"))
        await self.event(owner, "/memory on")
        self.assertIn("观赏鲸鱼", (await self.event(qq, "鲸鱼")).recalled)
        self.assertIsNone((await self.event(qq, "仍然暂停", group="123")).get_extra("_ffa_memory_access"))

    async def test_persona_shared_by_principal_and_stale_undo_is_rejected(self):
        owner = Account(self.providers[0], "31")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        qq = Account(QQ, "42")
        await self.bind(owner, qq)

        await self.event(owner, "/persona set 先说结论")
        self.assertIn("先说结论", (await self.event(qq, "/persona list")).sent[0])
        await self.event(qq, "/persona edit 1 解释过程")
        self.assertIn("人格已被更新", (await self.event(owner, "/persona undo")).sent[0])
        self.assertIn("已恢复", (await self.event(qq, "/undo")).sent[0])
        self.assertIn("先说结论", (await self.event(owner, "/persona list")).sent[0])
        self.ids.unlink(owner, qq)
        self.assertEqual((await self.event(qq, "/persona set 未授权修改")).sent, [])
        self.assertIn("先说结论", (await self.event(owner, "/persona list")).sent[0])

    async def test_file_preprocessing_reaches_same_downstream_on_bound_providers(self):
        owner = Account(self.providers[0], "31")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        qq = Account(QQ, "42")
        await self.bind(owner, qq)
        results = []

        class File:
            name = "fixture.txt"
            file_ = "materialized.txt"

            @property
            def file(self):
                raise AssertionError("Conversion must not resolve a native attachment")

        class Plain:
            def __init__(self, text):
                self.text = text

        components = SimpleNamespace(Plain=Plain)

        async def read(parts, *, temp_root):
            self.assertIsInstance(parts[0], File)
            return "[FFA_FILE_MATERIAL] fixture"

        with patch.dict(sys.modules, {"astrbot.api.message_components": components}), patch(
            "fat_fish.integrations.text_files.read_attachments", read
        ):
            for account in (owner, qq):
                address = account.provider
                native = NativeEvent("aiocqhttp" if address == QQ else address.platform,
                                     address.instance, sender=account.subject, parts=[File()])

                async def downstream():
                    results.append((native.message_str, native.get_messages()[0].text))

                await self.gateway.run(native, downstream)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    async def test_main_structured_persona_intent_reaches_shared_handler(self):
        owner = Account(self.providers[0], "31")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        requests = []

        class Model:
            async def text_chat(self, **request):
                requests.append(request)
                return SimpleNamespace(completion_text='{"command":"persona","args":["set","先说结论 再解释"]}')

        async def resolve(_):
            return Model()

        self.gateway.context = SimpleNamespace(get_using_provider_async=resolve)
        result = await self.event(owner, "把人格设为先说结论 再解释")
        self.assertIn("已保存", result.sent[0])
        self.assertIn("先说结论 再解释", (await self.event(owner, "/persona list")).sent[0])
        from fat_fish.commands.intents import GUIDANCE, PRIVATE_GROUP_GUIDANCE
        self.assertEqual(requests[0]["system_prompt"], GUIDANCE + "\n" + PRIVATE_GROUP_GUIDANCE)
        self.assertEqual(json.loads(requests[0]["prompt"])["persona_numbers"]["persona"], [])
        history = (await self.event(owner, "/history all")).sent[0]
        self.assertIn("persona set：", history)
        self.assertIn("persona list：", history)
        self.assertNotIn("先说结论", history)

    async def test_registered_command_reaches_help_and_natural_dispatch_with_authorization(self):
        from unittest.mock import AsyncMock

        owner, member = (Account(self.providers[0], subject) for subject in ("31", "32"))
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        self.ids.observe(member)
        self.ids.provision(owner, member, Role.MEMBER)
        handler = AsyncMock(return_value="已执行扩展")
        self.gateway.commands.register("sample", routes={"show": {1}}, help="/sample show",
                                       authorize=self.runtime.permissions.is_global_admin, handle=handler)
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
            completion_text='{"command":"sample","args":["show"]}')))
        self.gateway.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        self.assertEqual((await self.event(owner, "/help sample")).sent, ["/sample show"])
        self.assertEqual((await self.event(member, "请查询扩展状态")).sent, ["当前会话权限不足。"])
        handler.assert_not_awaited()
        self.assertEqual((await self.event(owner, "请查询扩展状态")).sent, ["已执行扩展"])
        command, admission, _ = handler.await_args.args
        self.assertEqual((command.name, command.args), ("sample", ("show",)))
        self.assertEqual(admission.role, Role.OWNER)
        self.assertIn('"sample"', provider.text_chat.await_args.kwargs["system_prompt"])

    async def test_natural_administration_changes_state_only_after_valid_confirmation(self):
        from unittest.mock import AsyncMock

        owner, member = (Account(self.providers[0], subject) for subject in ("31", "32"))
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        self.ids.observe(member)
        self.ids.provision(owner, member, Role.MEMBER)
        model = SimpleNamespace(text_chat=AsyncMock())
        self.gateway.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=model))
        room = Conversation(owner.provider, "900", ChatKind.GROUP, "123", "123")

        async def natural(account, text, command, *args):
            model.text_chat.return_value = SimpleNamespace(completion_text=json.dumps(
                {"command": command, "args": args}))
            return (await self.event(account, text)).sent[0]

        self.assertIn("群开放范围", await natural(member, "开放群123", "whitelist", "add", "123"))
        self.assertIn("/confirm", await natural(owner, "开放群123", "whitelist", "add", "123"))
        self.assertIsNone(self.runtime.permissions.conversation(owner, room))
        self.assertIn("已取消", await natural(owner, "取消刚才的操作", "cancel"))
        self.assertIn("没有匹配", await natural(owner, "确定", "confirm"))
        await natural(owner, "开放群123", "whitelist", "add", "123")
        self.assertIn("已更新", await natural(owner, "确定", "confirm"))
        self.assertEqual(self.runtime.permissions.conversation(owner, room).mode, "mention")
        self.assertEqual(await natural(owner, "把那个群改一下", "clarify"), "response")
        self.assertIn("关闭", await natural(owner, "关闭自然语言管理", "naturalcommand", "off"))
        self.assertFalse(self.gateway.chat_settings.read()["naturalcommand"][self.ids.principal_id(owner)])
        model.text_chat.reset_mock()
        self.assertEqual((await self.event(owner, "开放群456")).sent, ["response"])
        model.text_chat.assert_not_awaited()
        self.assertIn("已撤销", (await self.event(owner, "/undo naturalcommand")).sent[0])
        self.assertNotIn(self.ids.principal_id(owner), self.gateway.chat_settings.read()["naturalcommand"])
        self.assertIn("whitelist", (await self.event(owner, "/help whitelist")).sent[0])
        self.assertIn("/confirm", await natural(owner, "授予32全局管理员权限", "globaladmin", "add", "32"))
        self.assertEqual(self.ids.resolve(member)[1], Role.MEMBER)
        await natural(owner, "确定", "confirm")
        self.assertEqual(self.ids.resolve(member)[1], Role.ADMIN)
        self.assertIn("/confirm", await natural(owner, "撤销全局管理员授权", "undo", "globaladmin"))
        self.assertEqual(self.ids.resolve(member)[1], Role.ADMIN)
        await natural(owner, "确定", "confirm")
        self.assertEqual(self.ids.resolve(member)[1], Role.MEMBER)
        clock = 1000
        self.gateway.pending.clock = lambda: clock
        await natural(owner, "关闭群123", "whitelist", "remove", "123")
        clock += 90
        self.assertIn("没有匹配", await natural(owner, "确定", "confirm"))
        self.assertEqual(self.runtime.permissions.conversation(owner, room).mode, "mention")
        self.assertIn("已更新群准入", (await self.event(owner, "/history all")).sent[0])

    async def test_main_private_scoped_role_requires_confirmation(self):
        from unittest.mock import AsyncMock

        owner, target = (Account(self.providers[0], subject) for subject in ("31", "32"))
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        self.ids.observe(target)
        self.ids.provision(owner, target, Role.MEMBER)
        room = Conversation(owner.provider, "900", ChatKind.GROUP, "123", "123")
        self.ids.set_room(owner, room, "mention", [owner])
        provider = SimpleNamespace(text_chat=AsyncMock(side_effect=[
            SimpleNamespace(completion_text='{"command":"role","args":["add","32","123"]}'),
            SimpleNamespace(completion_text='{"command":"confirm","args":[]}'),
        ]))
        self.gateway.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        self.assertIn("确认群管理员", (await self.event(owner, "把32设成群123的管理员")).sent[0])
        self.assertFalse(self.ids.conversation_permissions(target, room)[-1])
        self.assertIn("已授予 32", (await self.event(owner, "确定执行")).sent[0])
        self.assertTrue(self.ids.conversation_permissions(target, room)[-1])
        self.assertIn("32", (await self.event(owner, "/role list 123")).sent[0])
        self.assertIn("1 个群", (await self.event(owner, "/role remove 32 all")).sent[0])
        self.assertFalse(self.ids.conversation_permissions(target, room)[-1])
        request = await self.event(owner, "/role add 32 123")
        code = request.sent[0].split("/confirm ")[1].split()[0]
        self.assertIn("权限没有变更", (await self.event(owner, "/role undo")).sent[0])
        self.assertIn("没有匹配", (await self.event(owner, "/confirm " + code)).sent[0])
        await self.event(owner, "/role add 32 123")
        self.assertIn("已授予", (await self.event(owner, "/confirm")).sent[0])
        self.assertIn("已撤销", (await self.event(owner, "/role undo")).sent[0])
        self.assertFalse(self.ids.conversation_permissions(target, room)[-1])
        await self.event(owner, "/role add 32 123")
        await self.event(owner, "/confirm")
        self.ids.change_room_mode(owner, room, "smart")
        self.assertIn("权限配置已有变化", (await self.event(owner, "/role undo")).sent[0])
        self.assertTrue(self.ids.conversation_permissions(target, room)[-1])

    async def test_verified_retraction_erases_memory_once(self):
        owner = Account(self.providers[0], "31")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        event = await self.event(owner, "我喜欢桂花茶")
        message = convert(event)
        notice = MessageRetraction(message.conversation, owner, message.message_id)
        provider = self.runtime.connections[owner.provider].provider
        with self.assertRaises(NotImplementedError):
            await self.runtime.retract(notice)
        provider.capabilities |= {Capability.RETRACT}

        async def verify(_):
            return False

        provider.verify_retraction = verify
        with self.assertRaises(PermissionError):
            await self.runtime.retract(notice)

        async def verified(_):
            return True

        provider.verify_retraction = verified
        wrong = Account(owner.provider, "unverified")
        with self.assertRaisesRegex(ValueError, "Private peer mismatch"):
            await self.runtime.retract(MessageRetraction(message.conversation, wrong, message.message_id))
        self.assertTrue(await self.runtime.retract(notice))
        self.assertFalse(await self.runtime.retract(notice))
        probe = await self.event(owner, "回忆我喜欢的茶")
        access = probe.get_extra("_ffa_memory_access")
        rendered = self.gateway.memory_bridge.memory().context(access, "桂花茶",
                                                               exclude_turn=convert(probe).event_id)
        self.assertNotIn("桂花茶", rendered)
        self.assertEqual(self.gateway.memory_bridge.memory().search(access, "桂花茶"), [])

    async def test_early_native_retraction_blocks_late_evidence_across_restart(self):
        owner = Account(self.providers[0], "31")
        self.ids.confirm_owner_challenge(owner, self.ids.issue_owner_challenge(owner))
        room = Conversation(owner.provider, "900", ChatKind.PRIVATE, owner.subject, owner.subject)
        native = (room, owner.subject, "late")
        with self.assertRaises(NotImplementedError):
            await self.gateway.retract(owner.provider, native)
        slot = self.runtime.connections[owner.provider]
        await self.runtime.disable(owner.provider)
        slot.provider = RetractionProvider(owner.provider)
        await self.runtime.enable(owner.provider)
        slot.provider.fail = True
        with self.assertRaises(PermissionError):
            await self.gateway.retract(owner.provider, native)
        slot.provider.fail = False
        self.assertTrue(await self.gateway.retract(owner.provider, native))

        restarted = PlatformGateway(private=self.gateway.path.parent)
        restarted.communication().register(RetractionProvider(owner.provider))
        await restarted.communication().enable(owner.provider)
        self.gateway = restarted
        self.assertFalse(await self.gateway.retract(owner.provider, native))
        late = await self.event(owner, "我喜欢桂花茶", message_id="late")
        self.assertIsNone(late.get_extra("_ffa_memory_receipt"))
        probe = await self.event(owner, "请回忆我的偏好")
        self.assertNotIn("桂花茶", probe.recalled)

        other = await self.event(owner, "我喜欢桂花茶", message_id="unrelated")
        self.assertIsNotNone(other.get_extra("_ffa_memory_receipt"))

    async def test_expired_and_concurrent_invites_cannot_overwrite_bindings(self):
        owner = Account(self.providers[0], "31")
        self.ids.clock = lambda: 1000
        code = self.ids.issue_owner_challenge(owner)
        self.ids.confirm_owner_challenge(owner, code)
        user = Account(self.providers[0], "32")
        self.ids.observe(user)
        target = Account(QQ, "42")
        expired = self.ids.invite(user, user, target)
        self.ids.clock = lambda: 1300
        with self.assertRaises(PermissionError):
            self.ids.confirm(target, expired)
        codes = [self.ids.invite(actor, actor, target) for actor in (owner, user)]
        results = await asyncio.gather(*(asyncio.to_thread(self.ids.confirm, target, code)
                                       for code in codes), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, str) for result in results), 1)
        self.assertEqual(self.ids.principal_id(target), next(result for result in results if isinstance(result, str)))


class SharedMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_unhandled_media_is_rejected(self):
        from fat_fish.gateway.media_ingress import UnsupportedMedia, prepare_media

        event = NativeEvent(parts=[type("Music", (), {})()])
        with self.assertRaisesRegex(UnsupportedMedia, "暂不支持"):
            await prepare_media(event, lambda: True)

    def test_same_sender_media_isolated_by_provider_and_room_membership(self):
        from fat_fish.adapters.group_media import GroupMedia

        media = GroupMedia(clock=lambda: 1000)
        image = type("Image", (), {})()
        qq = NativeEvent(platform="aiocqhttp", group="123", parts=[image])
        qq_room = convert(qq).conversation.storage_key
        media.observe(qq, ("old",), mentioned=False, room=qq_room)
        lark = NativeEvent(group="123")
        media.observe(lark, ("old",), mentioned=True, room=convert(lark).conversation.storage_key)
        self.assertEqual(lark.parts, [])
        after_revocation = NativeEvent(platform="aiocqhttp", group="123")
        media.observe(after_revocation, ("changed",), mentioned=True, room=qq_room)
        self.assertEqual(after_revocation.parts, [])

    async def test_inline_forward_reaches_shared_components(self):
        from fat_fish.gateway.media_ingress import prepare_media
        from fat_fish.integrations.forwards import ForwardReader

        class Plain:
            def __init__(self, text):
                self.text = text

        class Node:
            uin = "42"
            name = "fixture-speaker"
            time = 123
            content = [Plain("转发内容")]

        components = SimpleNamespace(Image=object, Plain=Plain)
        rendered = []
        with patch.dict(sys.modules, {"astrbot.api.message_components": components}):
            for platform in ("aiocqhttp", "lark"):
                event = NativeEvent(platform=platform, parts=[Node()])
                await prepare_media(event, lambda: True, forward_reader=ForwardReader(),
                                    author_allowed=lambda account: account == "42")
                self.assertIsInstance(event.get_messages()[0], Plain)
                self.assertIn("转发内容", event.get_messages()[0].text)
                self.assertIn("fixture-speaker", event.get_messages()[0].text)
                rendered.append(event.get_messages()[0].text)
        self.assertEqual(rendered[0], rendered[1])

    async def test_local_attachment_is_restricted_to_native_temporary_storage(self):
        from fat_fish.integrations.text_files import read_attachments

        with tempfile.TemporaryDirectory() as directory:
            native = Path(directory) / "native"
            native.mkdir()
            allowed = native / "sample.txt"
            allowed.write_text("可读文件", encoding="utf-8")
            outside = Path(directory) / "external.txt"
            outside.write_text("外部私有文件", encoding="utf-8")
            file_type = type("File", (), {"name": "sample.txt"})
            good, bad = file_type(), file_type()
            good.file_, bad.file_ = str(allowed), str(outside)
            material = await read_attachments([good, bad], temp_root=native)
            self.assertIn("可读文件", material)
            self.assertNotIn("外部私有文件", material)
            self.assertIn("未能读取", material)


class PersonaScopeTests(unittest.TestCase):
    def test_preference_undo_checks_current_revision(self):
        from fat_fish.permissions.service import Admission, PermissionService
        from fat_fish.persona.chat_settings import ChatSettings

        with tempfile.TemporaryDirectory() as directory:
            settings = ChatSettings(Path(directory) / "settings.json")
            first = Admission("first", Role.ADMIN, "smart", True)
            second = Admission("second", Role.ADMIN, "smart", True)
            permissions = PermissionService(None)

            def command(text, actor):
                event = NativeEvent(group="123", text=text)
                message = convert(event)
                if text == "/undo webresearch":
                    return settings.undo_preference("webresearch", message, actor, permissions, lambda: True)
                return settings.command(event, message, actor, permissions, lambda: True)

            self.assertIn("关闭", command("/webresearch off", first))
            self.assertIn("开启", command("/webresearch on", second))
            self.assertIn("未覆盖", command("/undo webresearch", first))
            self.assertIn("已撤销", command("/undo webresearch", second))
            self.assertIn("未覆盖", command("/undo webresearch", first))

    def test_concurrent_persona_edits_preserve_newer_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        from fat_fish.permissions.service import Admission, PermissionService
        from fat_fish.persona.chat_settings import ChatSettings

        with tempfile.TemporaryDirectory() as directory:
            settings = ChatSettings(Path(directory) / "persona.json")
            admission = Admission("principal", Role.MEMBER, "open")

            def change(index):
                event = NativeEvent(text=f"/persona add 并发条目{index}")
                return settings.command(event, convert(event), admission, PermissionService(None), lambda: True)

            with ThreadPoolExecutor(max_workers=4) as workers:
                self.assertTrue(all("已保存" in reply for reply in workers.map(change, range(8))))
            entry = settings.read()["persona_entries"]["private:principal"]
            self.assertEqual(entry["revision"], 8)
            self.assertEqual({text for _, text in entry["rows"]}, {f"并发条目{index}" for index in range(8)})

    def test_global_private_and_group_persona_lifecycle(self):
        from fat_fish.permissions.service import Admission, PermissionService
        from fat_fish.persona.chat_settings import ChatSettings

        with tempfile.TemporaryDirectory() as directory:
            settings = ChatSettings(Path(directory) / "persona.json")
            permissions = PermissionService(None)
            owner = Admission("owner-principal", Role.OWNER, "open", True)
            member = Admission("member-principal", Role.MEMBER, "open")

            def command(text, admission=owner, group="", authorized=True):
                event = NativeEvent(group=group, text=text)
                return settings.command(event, convert(event), admission, permissions, lambda: authorized)

            self.assertIn("请在授权私聊", command("/globalpersona add 全局", member))
            self.assertIn("需要本群管理权限", command("/persona add 未授权", member, "123"))
            for prefix, group in (("/globalpersona", ""), ("/persona", ""), ("/persona", "123")):
                self.assertIn("已保存", command(prefix + " add 初始内容", group=group))
                self.assertIn("初始内容", command(prefix + " list", group=group))
                self.assertIn("已保存", command(prefix + " edit 1 编辑内容", group=group))
                self.assertIn("已保存", command(prefix + " remove 1", group=group))
                self.assertIn("已恢复", command(prefix + " undo", group=group))
                self.assertIn("编辑内容", command(prefix + " list", group=group))
                self.assertIn("已保存", command(prefix + " reset", group=group))
                with patch("fat_fish.persona.chat_settings.monotonic", return_value=float("inf")):
                    self.assertIn("没有可撤销", command(prefix + " undo", group=group))

            command("/persona add 第一群", group="123")
            command("/persona add 第二群", group="456")
            self.assertNotIn("第一群", command("/persona list", group="456"))
            command("/persona add 个人内容")
            self.assertNotIn("个人内容", command("/persona list", member))
            self.assertIn("授权已变化", command("/persona reset", authorized=False))


class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_mode_requires_current_moderator_confirmation(self):
        from fat_fish.permissions.service import Admission, PermissionService

        gateway = PlatformGateway(private=Path("."))
        message = convert(NativeEvent(group="123"))
        admission = Admission("principal", Role.MEMBER, "mention", True)
        change_mode = Mock(side_effect=PermissionError)
        runtime = SimpleNamespace(identities=SimpleNamespace(change_room_mode=change_mode),
                                  permissions=PermissionService(None))
        actor = (admission.principal_id, message.sender.storage_key)
        scope = message.conversation.storage_key
        code = gateway.pending.issue(actor, scope, "groupmode", ("smart",))
        outcome = await gateway._role_command(runtime, message, admission, ["/confirm", code], None)
        self.assertIn("仅 L2/L3", outcome)
        change_mode.assert_called_once_with(message.sender, message.conversation, "smart")
        self.assertIsNone(gateway.pending.peek(actor, scope))

    async def test_existing_participation_decisions_and_budgets(self):
        from fat_fish.participation.participation import Participation

        clock = 1000
        participation = Participation(clock=lambda: clock)
        room = participation.capture("room", 0, "群友一", "你怎么看这个问题？")
        evaluate = unittest.mock.AsyncMock(return_value={"should_reply": False})
        provider = object()
        with patch.object(participation.decision, "evaluate", evaluate), patch(
            "fat_fish.participation.participation.asyncio.sleep", new_callable=unittest.mock.AsyncMock,
        ):
            self.assertIsNone(await participation.choose("room", 0, provider=provider))
            self.assertEqual(room["reason"], "judge_no")
            clock += 15
            self.assertIsNone(await participation.choose("room", 0, provider=provider))
            self.assertEqual(room["reason"], "judge_interval")
            clock += 1
            evaluate.return_value = {"should_reply": True}
            self.assertTrue(await participation.choose("room", 0, provider=provider))
            self.assertIsNone(await participation.choose("room", 0, provider=provider))
            self.assertEqual(room["reason"], "cooldown")

            participation.capture("room", 0, "__bot__", "我的回答")
            room["reply_to"] = "群友一"
            participation.capture("room", 0, "群友一", "我刚才问的是什么")
            clock += 3
            evaluate.reset_mock()
            self.assertTrue(await participation.choose("room", 0, provider=provider))
            evaluate.assert_not_awaited()

            room["sent"].extend([clock] * 12)
            self.assertIsNone(await participation.choose("room", 0, provider=provider))
            self.assertEqual(room["reason"], "hourly_limit")
            clock += 3601
            participation.capture("room", 0, "群友一", "大家怎么看")
            evaluate.side_effect = RuntimeError("fixture")
            self.assertIsNone(await participation.choose("room", 0, provider=provider))
            self.assertEqual(room["reason"], "judge_error")

    async def test_membership_change_during_judge_discards_context(self):
        from fat_fish.permissions.service import Admission

        gateway = PlatformGateway(private=Path("."))
        address = ProviderId("lark", "office")
        room = Conversation(address, "900", ChatKind.GROUP, "123", "123")
        event = NativeEvent(group="123")
        message = convert(event)
        self.assertEqual(message.conversation, room)
        directory = SimpleNamespace(room_membership_signature=Mock(side_effect=[("before",), ("after",)]))
        gateway.runtime = SimpleNamespace(identities=directory)
        gateway.context = SimpleNamespace(get_using_provider_async=unittest.mock.AsyncMock(return_value=object()))
        with patch.object(gateway.participation, "choose", return_value="旧成员上下文"):
            self.assertFalse(await gateway._participate(message, Admission("principal", Role.MEMBER, "smart"), event))
        self.assertIsNone(event.get_extra("_ffa_canonical_group_context"))


class ProviderRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_retraction_capability_requires_conversion_and_verification(self):
        from fat_fish.communication.registry import ProviderDefinition

        address = ProviderId("lark", "office")
        valid = RetractionProvider(address)
        definition = SimpleNamespace(entrypoint=f"{__name__}:create_test_provider",
                                     capabilities=valid.capabilities, verification_capabilities=frozenset())
        with patch(f"{__name__}.create_test_provider", return_value=valid):
            self.assertIs(ProviderDefinition.create(definition, address, "900", {}, None), valid)
            with patch.object(valid, "verify_retraction", None):
                with self.assertRaisesRegex(ValueError, "retraction interface"):
                    ProviderDefinition.create(definition, address, "900", {}, None)

    async def test_discovered_instances_validate_config_and_isolate_start_failure(self):
        manifest = {
            "provider_id": "fixture", "name": "Fixture", "version": "1",
            "entrypoint": f"{__name__}:create_test_provider",
            "supported_platforms": ["synthetic"],
            "capabilities": ["text", "push"],
            "configuration_schema": {"fail": "bool"},
            "required_dependencies": [],
            "verification_capabilities": ["private-message"],
            "lifecycle_capabilities": ["start", "stop", "health"],
        }
        entry = SimpleNamespace(name="fixture", dist=SimpleNamespace(name="fixture-dist"),
                                load=Mock(return_value=lambda: manifest))
        untrusted = SimpleNamespace(name="forged", dist=SimpleNamespace(name="unknown-dist"), load=Mock())
        with tempfile.TemporaryDirectory() as tmp, patch(
            "fat_fish.communication.registry.entry_points", return_value=[entry, untrusted]
        ):
            gateway = PlatformGateway(private=Path(tmp))
            gateway.context = SimpleNamespace(get_platform_inst=lambda _: None)
            platform_gateway = gateway
            platform_gateway.registry.path.write_text(json.dumps({
                "schema": 2, "trusted_packages": ["fixture-dist"],
                "providers": [
                    {"provider": "fixture", "platform": "synthetic", "instance": name,
                     "bot": "900", "config": {"fail": fail}}
                    for name, fail in (("up", False), ("down", True), ("invalid", "false"))
                ],
            }))
            await platform_gateway.start_configured()
            runtime = platform_gateway.communication()
            up, down, invalid = (ProviderId("synthetic", name) for name in ("up", "down", "invalid"))
            untrusted.load.assert_not_called()
            self.assertNotIn(invalid, runtime.connections)
            self.assertEqual(await runtime.health(up), "running")
            self.assertEqual(await runtime.health(down), "unhealthy")
            runtime.connections[down].provider.fail = False
            self.assertTrue(await runtime.enable(down))
            await runtime.disable(up)
            self.assertEqual(await runtime.health(down), "running")
            self.assertEqual(await runtime.health(up), "disabled")
            provider = runtime.connections[down].provider
            with patch.object(provider, "stop", side_effect=ConnectionError("cleanup interrupted")):
                await platform_gateway.stop_configured()
                self.assertIs(platform_gateway.runtime, runtime)
                self.assertFalse(await runtime.enable(down))
                self.assertEqual(await runtime.health(down), "unhealthy")
            await platform_gateway.stop_configured()
            self.assertEqual(await runtime.health(down), "disabled")
            self.assertIsNone(platform_gateway.runtime)


class CommunicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        private = Path(tmp.name)
        self.private = private
        self.gateway = PlatformGateway(private=private)
        self.runtime = self.gateway.communication()
        self.ids = self.runtime.identities
        self.owner = Account(QQ, "31")
        self.ids.confirm_owner_challenge(self.owner, self.ids.issue_owner_challenge(self.owner))
        self.lark = ProviderId("lark", "office")
        self.second = ProviderId("synthetic", "other")
        self.providers = {address: VerifiedProvider(address) for address in (self.lark, self.second)}
        for address, provider in self.providers.items():
            self.runtime.register(provider)
            await self.runtime.enable(address)

    def provision(self, account, role):
        self.ids.observe(account)
        return self.ids.provision(self.owner, account, role)

    async def deliver(self, event):
        async def downstream():
            await event.send(event.plain_result("业务回复"))
        return await self.gateway.run(event, downstream)

    def test_media_envelope_preserves_same_components_across_platforms(self):
        parts = [
            type(kind, (), {})() for kind in ("Image", "Record", "File")
        ]
        for part, reference, mime in zip(parts, ("image.png", "voice.ogg", "notes.txt"),
                                         ("image/png", "audio/ogg", "text/plain")):
            if type(part).__name__ == "File":
                part.file_ = reference
            else:
                part.file = reference
            part.filename = reference
            part.mime_type = mime
        qq = convert(NativeEvent("aiocqhttp", "feiyu-onebot", parts=parts))
        lark = convert(NativeEvent("lark", "office", parts=parts))
        self.assertEqual(qq.media, lark.media)
        self.assertEqual([item.kind for item in qq.media], ["image", "record", "file"])
        self.assertEqual([item.reference for item in qq.media], ["image.png", "voice.ogg", "notes.txt"])
        self.assertEqual(qq.media[2].filename, "notes.txt")
        self.assertEqual(qq.media[1].content_type, "audio/ogg")

    def test_qq_conversion_reads_structured_segments(self):
        msg = convert(NativeEvent("aiocqhttp", "feiyu-onebot", group="17",
                                  parts=[At("900"), Reply("12", "33")],
                                  raw={"message": [{"type": "at", "data": {"qq": 45}}]}))
        self.assertEqual(msg.conversation.provider, QQ)
        self.assertEqual((msg.conversation.peer, msg.sender.subject, msg.message_id), ("17", "32", "42"))
        self.assertEqual(msg.mentions, ("900", "45"))
        self.assertEqual(msg.reply_to, "12")
        self.assertEqual(msg.quoted_sender, "33")
        self.assertEqual(msg.adapter_id, "astrbot")
        self.assertEqual(msg.timestamp, 1771000000)


    async def test_binding_uses_verified_target_and_cannot_replay(self):
        target = Account(self.lark, "ou_owner")
        invitation = self.ids.invite(self.owner, self.owner, target)
        impostor = NativeEvent(sender="ou_other", text="/ffa-link " + invitation)
        await self.deliver(impostor)
        self.assertIn("未完成", impostor.sent[0])
        proof = NativeEvent(sender="ou_owner", text="/ffa-link " + invitation)
        await self.deliver(proof)
        self.assertIn("完成", proof.sent[0])
        self.assertEqual(self.ids.resolve(target), (self.ids.principal_id(self.owner), Role.OWNER))
        owner_id = self.ids.principal_id(self.owner)
        self.assertEqual(str(UUID(owner_id)), owner_id)
        self.assertEqual(self.ids.principal_id(target), owner_id)
        event = convert(NativeEvent(sender="ou_owner"))
        self.assertEqual(self.runtime.authorize(event).principal_id, owner_id)
        with self.assertRaises(PermissionError):
            self.ids.confirm(target, invitation)
        with self.assertRaises(PermissionError):
            self.ids.provision(Account(self.second, "31"), target, Role.ADMIN)
        self.ids.unlink(self.owner, target)
        await self.deliver(NativeEvent(sender="ou_owner"))
        self.assertEqual(self.ids.resolve(target), (None, Role.VISITOR))
        self.assertIsNone(self.ids.principal_id(target))

    def test_unlink_revokes_issuing_account_and_preserves_binding_audit(self):
        alias = Account(self.lark, "ou_alias")
        target = Account(self.second, "target")
        self.ids.confirm(alias, self.ids.invite(self.owner, self.owner, alias))
        invitation = self.ids.invite(alias, alias, target)
        self.ids.unlink(self.owner, alias)
        self.ids.confirm(alias, self.ids.invite(self.owner, self.owner, alias))
        with self.assertRaises(PermissionError):
            self.ids.confirm(target, invitation)
        with self.ids.transaction() as db:
            audit = db.execute("""SELECT verification_method,revoked_by FROM identity_bindings
                WHERE address=? ORDER BY revoked_at IS NULL""", (alias.storage_key,)).fetchall()
        self.assertEqual(audit, [("adapter-invitation", self.owner.storage_key),
                                 ("adapter-invitation", None)])




    async def test_scope_trigger_duplicate_and_id_isolation(self):
        lark_user = Account(self.lark, "32")
        second_user = Account(self.second, "32")
        person = self.provision(lark_user, Role.MEMBER)
        self.assertNotEqual(person, self.provision(second_user, Role.MEMBER))
        group = NativeEvent(group="17", parts=[At("900")])
        self.ids.set_room(self.owner, convert(group).conversation, "mention", (lark_user,))
        unmentioned = NativeEvent(group="17")
        unrelated = NativeEvent("synthetic", "other", group="17", parts=[At("900")])
        for event in (unmentioned, unrelated, group):
            await self.deliver(event)
        replay = NativeEvent(group="17", parts=[At("900")])
        await self.deliver(replay)
        self.assertEqual(group.sent, ["业务回复"])
        self.assertTrue(all(event.stopped for event in (unmentioned, unrelated, replay)))
        self.assertNotEqual(convert(group).event_id, convert(unrelated).event_id)

    async def test_concurrent_events_and_fault_isolation(self):
        for address in self.providers:
            self.provision(Account(address, "32"), Role.MEMBER)
        first = NativeEvent()
        second = NativeEvent("synthetic", "other")
        entered = set()
        ready = asyncio.Event()

        def handler(event):
            async def downstream():
                entered.add(event.instance)
                if len(entered) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 2)
                if event is first:
                    raise ConnectionError("adapter disconnected")
                await event.send("independent reply")
            return downstream

        await asyncio.gather(*(self.gateway.run(e, handler(e)) for e in (first, second)))
        self.assertTrue(first.stopped)
        self.assertEqual(second.sent, ["independent reply"])
        self.providers[self.lark].fail = True
        self.assertEqual(await self.runtime.health(self.lark), "unhealthy")
        self.assertEqual(await self.runtime.health(self.second), "running")

    async def test_push_permissions_capabilities_and_revoke_before_reply(self):
        person = Account(self.lark, "32")
        self.provision(person, Role.MEMBER)
        event = NativeEvent()
        target = convert(event).conversation
        self.assertTrue(await self.runtime.send(person, target, Outgoing("提醒"), "job", proactive=True))
        self.assertFalse(await self.runtime.send(person, target, Outgoing("提醒"), "job", proactive=True))
        with self.assertRaises(ValueError):
            await self.runtime.send(person, target, Outgoing("引用", reply_to="1"), "quote")
        with self.assertRaises(PermissionError):
            await self.runtime.send(Account(self.second, "32"), target, Outgoing("泄露"), "deny")
        self.assertEqual(len(self.providers[self.lark].deliveries), 1)

        async def downstream():
            self.provision(person, Role.BLOCKED)
            await event.send("must not leak")

        with self.assertRaises(asyncio.CancelledError):
            await self.gateway.run(event, downstream)
        self.assertEqual(event.sent, [])

    async def test_unknown_adapter_and_spoofed_bot_denied(self):
        self.provision(Account(self.lark, "32"), Role.MEMBER)
        wrong_bot = NativeEvent(bot="wrong")
        wrong_instance = NativeEvent(instance="wrong")
        for event in (wrong_bot, wrong_instance):
            await self.deliver(event)
            self.assertTrue(event.stopped)
            self.assertEqual(event.sent, [])

    async def test_disabled_provider_and_durable_identity(self):
        person = Account(self.lark, "32")
        ident = self.provision(person, Role.MEMBER)
        await self.runtime.disable(self.lark)
        blocked = NativeEvent()
        await self.deliver(blocked)
        self.assertTrue(blocked.stopped)
        reloaded = PlatformGateway(private=self.private).communication()
        self.assertEqual(reloaded.identities.resolve(person), (ident, Role.MEMBER))
        self.assertEqual(await self.runtime.health(self.lark), "disabled")

    async def test_declared_native_provider_only_routes_observed_sessions(self):
        address = ProviderId("lark", "native-lark")
        alice = Account(address, "ou_alice")
        bob = Account(address, "ou_bob")
        for account in (alice, bob):
            self.provision(account, Role.MEMBER)
        cfg = {"schema": 2, "providers": [
            {"provider": "astrbot", "platform": "lark", "instance": "native-lark", "bot": "900"},
        ]}
        (self.private / "ffa-communication.json").write_text(json.dumps(cfg))
        native = SimpleNamespace(meta=lambda: SimpleNamespace(name="lark"))
        self.gateway.context = SimpleNamespace(get_platform_inst=lambda name: native)
        first = NativeEvent(instance=address.instance, sender=alice.subject, thread="pinned-session")
        colliding = NativeEvent(instance=address.instance, sender=bob.subject, thread="pinned-session")
        await self.deliver(first)
        await self.deliver(colliding)
        self.assertEqual(first.sent, ["业务回复"])
        self.assertTrue(colliding.stopped)
        self.assertEqual(colliding.sent, [])
        route = convert(colliding).conversation
        with self.assertRaises(PermissionError):
            await self.runtime.send(bob, route, Outgoing("mismatched native target"), "bad-route")
