"""Dual-provider regression tests with simulated external transports."""
import asyncio
from contextlib import closing
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fat_fish.adapters.astrbot_communication import convert, create_provider, QQ
from fat_fish.communication.model import Account, ChatKind, MessageRetraction, ProviderId
from fat_fish.communication.registry import ProviderContext
from fat_fish.core.identity import Role
from fat_fish.gateway.platform_gateway import PlatformGateway
from test_communication import NativeEvent, VerifiedProvider


class DualPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gateway = PlatformGateway(private=Path(self.tmp.name))
        self.runtime = self.gateway.communication()
        self.ids = self.runtime.identities
        self.owner = Account(QQ, '31')
        self.ids.confirm_owner_challenge(self.owner, self.ids.issue_owner_challenge(self.owner))
        self.lark = ProviderId('lark', 'office')
        self.native = SimpleNamespace(meta=lambda: SimpleNamespace(name='lark'), bot_open_id='900', appid='app')
        self.provider = create_provider(self.lark, '900', ProviderContext(lambda: self.native, AsyncMock()), {})
        self.runtime.register(self.provider)
        self.runtime.register(VerifiedProvider(QQ))
        await asyncio.gather(self.runtime.enable(QQ), self.runtime.enable(self.lark))
        self.addAsyncCleanup(self.runtime.stop_all)

    def event(self, platform='lark', **kwargs):
        return NativeEvent(platform, 'office' if platform == 'lark' else QQ.instance, **kwargs)

    async def test_same_tenant_first_message_has_private_member_access(self):
        event = self.event()
        event.set_extra('_ffa_lark_organization', ('app', 'tenant', 'tenant', 'user', '32'))
        async def downstream():
            await event.send('accepted')
        await self.gateway.run(event, downstream)
        self.assertEqual(event.sent, ['accepted'])
        message = convert(event)
        admission = self.runtime.authorize(message)
        self.assertEqual(admission.role, Role.MEMBER)
        # Native evidence is scoped to this private conversation and platform.
        self.assertEqual(self.ids.resolve_principal(message.sender)[1], Role.MEMBER)
        self.assertIsNone(self.ids.principal_id(Account(QQ, '32')))

    async def test_missing_foreign_or_mismatched_tenant_evidence_has_no_grant(self):
        invalid = [None, ('app', 'tenant', 'other', 'user', '32'),
                   ('other-app', 'tenant', 'tenant', 'user', '32'),
                   ('app', '', '', 'user', '32'), ('app', 'tenant', 'tenant', 'app', '32'),
                   ('app', 'tenant', 'tenant', 'user', 'different')]
        for evidence in invalid:
            with self.subTest(evidence=evidence):
                event = self.event()
                event.set_extra('_ffa_lark_organization', evidence)
                await self.provider.refresh_native_access(event, convert(event), self.ids)
                self.ids.observe(convert(event).sender)
                self.assertIsNone(self.runtime.authorize(convert(event)))

    async def test_blocked_member_stays_blocked(self):
        event = self.event()
        account = convert(event).sender
        self.ids.observe(account)
        self.ids.provision(self.owner, account, Role.BLOCKED)
        event.set_extra('_ffa_lark_organization', ('app', 'tenant', 'tenant', 'user', '32'))
        await self.provider.refresh_native_access(event, convert(event), self.ids)
        self.assertIsNone(self.runtime.authorize(convert(event)))

    async def test_private_native_access_does_not_grant_group_authority(self):
        event = self.event()
        event.set_extra('_ffa_lark_organization', ('app', 'tenant', 'tenant', 'user', '32'))
        await self.provider.refresh_native_access(event, convert(event), self.ids)
        account = convert(event).sender
        self.ids.observe(account)
        group = self.event(group='same')
        self.assertIsNone(self.runtime.authorize(convert(group)))
        self.assertFalse(self.ids.conversation_permissions(account, convert(group).conversation)[4])

    async def test_stop_restart_and_failure_are_provider_scoped(self):
        await self.runtime.disable(self.lark)
        await self.runtime.disable(self.lark)
        self.assertEqual(await self.runtime.health(QQ), 'running')
        await self.runtime.enable(self.lark)
        self.assertEqual(await self.runtime.health(self.lark), 'running')
        self.native.bot_open_id = 'wrong'
        await self.runtime.disable(self.lark)
        self.assertFalse(await self.runtime.enable(self.lark))
        self.assertEqual(await self.runtime.health(QQ), 'running')
        self.native.bot_open_id = '900'
        self.assertTrue(await self.runtime.enable(self.lark))

    async def test_identical_ids_deliver_once_per_platform(self):
        events = [self.event(), self.event('aiocqhttp')]
        for event in events:
            self.ids.observe(convert(event).sender)
            self.ids.provision(self.owner, convert(event).sender, Role.MEMBER)
        calls = []
        async def handle(message, admission):
            calls.append(message.conversation.provider)
            await asyncio.sleep(0)
        self.assertEqual(await asyncio.gather(*(self.runtime.receive(convert(e), handle) for e in events)), [True, True])
        self.assertEqual(len(calls), 2)
        self.assertEqual(await asyncio.gather(*(self.runtime.receive(convert(e), handle) for e in events)), [False, False])
        self.assertNotEqual(convert(events[0]).event_id, convert(events[1]).event_id)

    async def test_linked_principal_turn_and_retraction_isolation(self):
        events = [self.event(), self.event('aiocqhttp')]
        qq = convert(events[1]).sender
        self.ids.observe(qq)
        self.ids.provision(self.owner, qq, Role.MEMBER)
        self.ids.confirm_platform_link(convert(events[0]).sender, self.ids.issue_platform_link(qq))
        principal = self.ids.principal_id(qq)
        turns = []
        for event in events:
            message = convert(event)
            item = self.gateway.chat_turns.receive(message, event, 100)
            turns.append(self.gateway.chat_turns.admit(item, principal, reply_window=(4, 6.5)))
        self.assertNotEqual(turns[0].key, turns[1].key)
        self.assertIsNot(turns[0].schedule, turns[1].schedule)
        message = convert(events[0])
        self.gateway.chat_turns.retract(MessageRetraction(message.conversation, message.sender, message.message_id))
        self.assertTrue(turns[0].superseded)
        self.assertFalse(turns[1].superseded)
    async def test_authenticated_envelopes_keep_tenant_evidence_per_concurrent_event(self):
        from fat_fish.adapters.lark_latency import attach_native_ingress
        received = []
        async def parse(**kwargs):
            return []
        def create(native):
            return self.event(sender=native.sender)
        async def convert_native(envelope):
            await asyncio.sleep(0)
            event = adapter.create_event(SimpleNamespace(raw_message=SimpleNamespace(message_type='text'), sender=envelope.event.sender.sender_id.open_id))
            received.append(event.get_extra('_ffa_lark_organization'))
        adapter = SimpleNamespace(convert_msg=convert_native, create_event=create, _parse_message_components=parse)
        detach = attach_native_ingress(adapter, None)
        def envelope(tenant, subject):
            return SimpleNamespace(header=SimpleNamespace(app_id='app',tenant_key=tenant), event=SimpleNamespace(sender=SimpleNamespace(tenant_key=tenant,sender_type='user',sender_id=SimpleNamespace(open_id=subject))))
        try:
            await asyncio.gather(adapter.convert_msg(envelope('one','32')), adapter.convert_msg(envelope('two','33')))
            self.assertCountEqual(received, [('app','one','one','user','32'),('app','two','two','user','33')])
            orphan = adapter.create_event(SimpleNamespace(raw_message=SimpleNamespace(message_type='text'),sender='32'))
            self.assertIsNone(orphan.get_extra('_ffa_lark_organization'))
        finally:
            detach()
        self.assertIs(adapter.convert_msg, convert_native)
    async def test_real_adapters_register_in_both_orders_and_preserve_health(self):
        import json
        from fat_fish.adapters.onebot_communication import OneBotCommunication
        for order in (('qq', 'lark'), ('lark', 'qq')):
            with self.subTest(order=order):
                private = Path(self.tmp.name) / '-'.join(order)
                private.mkdir()
                gateway = PlatformGateway(private=private)
                natives = {'office': self.native, QQ.instance: SimpleNamespace(meta=lambda: SimpleNamespace(name='aiocqhttp'))}
                gateway.context = SimpleNamespace(get_platform_inst=natives.get, send_message=AsyncMock(return_value=True))
                # Retraction listeners have separate focused coverage. No private
                # transport callback is installed by these minimal native fixtures.
                configuration = {'schema': 2, 'providers': [
                    {'provider': 'astrbot', 'platform': platform,
                     'instance': 'office' if platform == 'lark' else QQ.instance, 'bot': '900'}
                    for platform in order
                ]}
                gateway.registry.path.write_text(json.dumps(configuration))
                with patch.object(
                    gateway, '_provider_context',
                    side_effect=lambda address: ProviderContext(
                        lambda: natives[address.instance], AsyncMock(return_value=True)),
                ):
                    await gateway.start_configured()
                runtime = gateway.communication()
                self.assertIsInstance(runtime.connections[QQ].provider, OneBotCommunication)
                self.assertEqual(len(runtime.connections), 2)
                with self.assertRaises(ValueError):
                    runtime.register(runtime.connections[QQ].provider)
                first = await runtime.health_snapshot(QQ)
                self.assertEqual(first['state'], 'running')
                self.assertIsNotNone(first['last_healthy_at'])
                await runtime.disable(self.lark)
                self.assertEqual((await runtime.health_snapshot(QQ))['start_attempts'],1)
                await runtime.enable(self.lark)
                self.assertEqual((await runtime.health_snapshot(self.lark))['restart_attempts'],1)
                await runtime.stop_all()

    async def test_offline_reminder_survives_ticks_and_other_provider_delivers(self):
        from fat_fish.reminders.dispatch import ReminderAccess, ReminderDispatch
        from fat_fish.reminders.reminders import ReminderStore
        private = Path(self.tmp.name) / 'reminder-runtime'
        private.mkdir()
        gateway = PlatformGateway(private=private)
        runtime = gateway.communication()
        identities = runtime.identities
        owner = Account(QQ, '32')
        identities.confirm_owner_challenge(owner, identities.issue_owner_challenge(owner))
        identities.confirm_platform_link(Account(self.lark, '32'), identities.issue_platform_link(owner))
        for address in (QQ, self.lark):
            runtime.register(VerifiedProvider(address))
            await runtime.enable(address)
        self.addAsyncCleanup(runtime.stop_all)
        store = ReminderStore(private / 'reminders.sqlite3')
        for platform in ('lark', 'aiocqhttp'):
            event = self.event(platform)
            access = ReminderAccess(runtime, convert(event).conversation)
            self.assertIn('已创建提醒', store.command(event, ['add','十秒之后',platform], access, structured=True))
        await runtime.disable(QQ)
        dispatch = ReminderDispatch(store, runtime)
        due = max(row['due'] for row in store.rows()) + 1
        for _ in range(4):
            await store.tick(dispatch.authorize, dispatch.send, now=due, ready=dispatch.ready, delivery_state=dispatch.delivery_state)
        self.assertEqual(len(runtime.connections[self.lark].provider.deliveries), 1)
        qq_row = next(row for row in store.rows() if row['platform'] == QQ.instance)
        self.assertEqual(qq_row['state'], 'active')
        # Reconstruct the durable store before reconnecting the offline provider.
        recovered = ReminderStore(store.path)
        await runtime.enable(QQ)
        dispatch = ReminderDispatch(recovered, runtime)
        for _ in range(3):
            await recovered.tick(dispatch.authorize, dispatch.send, now=due, ready=dispatch.ready, delivery_state=dispatch.delivery_state)
        for address in (QQ, self.lark):
            deliveries = runtime.connections[address].provider.deliveries
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0][0].provider, address)
    async def test_offline_native_authorization_does_not_consume_retries(self):
        from fat_fish.reminders.dispatch import ReminderAccess, ReminderDispatch
        from fat_fish.reminders.reminders import ReminderStore
        private = Path(self.tmp.name) / 'native-reminder-runtime'
        private.mkdir()
        gateway = PlatformGateway(private=private)
        runtime = gateway.communication()
        owner = Account(QQ, '32')
        ids = runtime.identities
        ids.confirm_owner_challenge(owner, ids.issue_owner_challenge(owner))
        bot = SimpleNamespace(_wsr_api_clients={}, _wsr_event_clients=set())
        native = SimpleNamespace(meta=lambda: SimpleNamespace(name='aiocqhttp'), get_client=lambda:bot)
        provider = create_provider(QQ, '900', ProviderContext(lambda:native, AsyncMock()), {})
        runtime.register(provider)
        await runtime.enable(QQ)
        self.addAsyncCleanup(runtime.stop_all)
        event = self.event('aiocqhttp')
        room = convert(event).conversation
        ids.set_native_access(owner, room, allowed=True)
        store = ReminderStore(private / 'reminders.sqlite3')
        self.assertIn('已创建提醒', store.command(event, ['add','十秒之后','offline'], ReminderAccess(runtime,room), structured=True))
        dispatch = ReminderDispatch(store, runtime)
        due = store.rows()[0]['due'] + 1
        for _ in range(4):
            await store.tick(dispatch.authorize, dispatch.send, now=due, ready=dispatch.ready)
        self.assertEqual(store.rows()[0]['state'], 'active')
        with closing(store.connect()) as db:
            self.assertEqual(db.execute('select count(*) from authorization_failures').fetchone()[0], 0)
