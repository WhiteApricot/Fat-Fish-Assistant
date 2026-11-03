"""State assertions for the R1 tool boundary, with synthetic QQ events."""
import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location('scoped_fixture', Path(__file__).with_name('Verify-P2C-Scoped.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from p2f.turn_tools import TurnTools


class TurnToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        case = fixture.Checks()
        case.setUp()
        self.addCleanup(case.tearDown)
        self.gateway = case.g
        self.guard = SimpleNamespace(confirm=SimpleNamespace(items={}), locks={}, handle=AsyncMock())
        self.reply = patch('p2f.turn_tools.send_command_reply', new_callable=AsyncMock)
        self.reply.start()
        self.addCleanup(self.reply.stop)

    def turn(self, sender='100', private=True, group='900'):
        event = fixture.Event(sender, private, group, 'synthetic user request 300')
        event.unified_msg_origin = 'test:' + (sender if private else group)
        event.set_extra('_feiyu_security_epoch', 7)
        return TurnTools(self.guard, self.gateway, event)

    async def test_grant_confirm_query_undo_and_confirm(self):
        grant = self.turn()
        await grant.execute('global_admin_grant', {'qq': '300'})
        self.assertEqual(self.gateway.load()[0].get('global_admins', []), [])
        await grant.execute('confirm_action', {})
        self.assertEqual(self.gateway.load()[0].get('global_admins', []), [])
        await self.turn('300').execute('confirm_action', {})
        self.assertEqual(self.gateway.load()[0].get('global_admins', []), [])
        await self.turn().execute('confirm_action', {})
        self.assertEqual(self.gateway.load()[0]['global_admins'], ['300'])
        undo = self.turn()
        history = await undo.execute('get_recent_actions', {})
        self.assertIn('globaladmin add', history)
        self.assertIn('经二次确认', history)
        await undo.execute('undo_recent_admin_grant', {})
        self.assertEqual(self.gateway.load()[0]['global_admins'], ['300'])
        await self.turn().execute('confirm_action', {})
        await self.turn().execute('confirm_action', {})
        self.assertEqual(self.gateway.load()[0]['global_admins'], [])

    async def test_expiry_cancel_invalid_unknown_and_budget(self):
        clock = [0]
        self.gateway.pending.clock = lambda: clock[0]
        await self.turn().execute('global_admin_grant', {'qq': '300'})
        clock[0] = 91
        await self.turn().execute('confirm_action', {})
        self.assertEqual(self.gateway.load()[0].get('global_admins', []), [])
        await self.turn().execute('global_admin_grant', {'qq': '300'})
        await self.turn().execute('cancel_action', {})
        await self.turn().execute('confirm_action', {})
        self.assertIsNone(self.gateway.pending.peek('100', 'private'))
        turn = self.turn()
        await turn.execute('global_admin_grant', {'qq': 'anyone'})
        await turn.execute('unknown', {})
        await turn.execute('get_recent_actions', {})
        await turn.execute('get_pending_actions', {})
        await turn.execute('global_admin_grant', {'qq': '300'})
        self.assertIsNone(self.gateway.pending.peek('100', 'private'))

    async def test_persona_restores_previous_value_and_quote_cannot_change_it(self):
        await self.turn().execute('private_persona_update', {'note': 'A'})
        await self.turn().execute('private_persona_update', {'note': 'B'})
        quoted = self.turn()
        quoted.event.get_messages = lambda: [type('Reply', (), {})()]
        await quoted.execute('private_persona_reset', {})
        self.assertEqual(self.gateway.load()[0]['private_persona_notes']['100'], 'B')
        await self.turn().execute('private_persona_undo', {})
        await self.turn().execute('private_persona_undo', {})
        self.assertEqual(self.gateway.load()[0]['private_persona_notes']['100'], 'A')
        await self.turn('300').execute('global_admin_grant', {'qq': '999'})
        self.assertIsNone(self.gateway.pending.peek('300', 'private'))

    async def test_reset_history_is_redacted_and_scope_bound(self):
        original = self.turn()
        other = self.turn('100', False)
        self.gateway.record_command(original.event, 'role', 'old-private')
        self.gateway.record_command(other.event, 'role', 'other-group')
        self.gateway.record_command(original.event, 'memory', '本私聊的长期记忆已清除 secret-fixture')
        self.assertEqual(self.gateway.command_receipts(original.event),
                         ['memory reset：已清除当前会话的长期记忆和上下文，无法撤销'])
        self.assertEqual(self.gateway.command_receipts(other.event), ['role：other-group'])
        self.assertEqual(await self.turn('300').execute('get_recent_actions', {}), '[]')

    async def test_native_hook_approves_only_this_turns_registered_handler(self):
        from astrbot.core.astr_agent_hooks import MainAgentHooks
        turn = self.turn()
        request = SimpleNamespace(func_tool=None)
        turn.install(request)
        context = SimpleNamespace(context=SimpleNamespace(event=turn.event))
        turn.event.set_extra('_feiyu_access_checked', True)
        tool = request.func_tool.get_tool('get_recent_actions')
        with patch.dict('sys.modules', {'astrbot.core.feiyu_p2c': SimpleNamespace(gateway=self.gateway)}), \
             patch('astrbot.core.astr_agent_hooks.call_event_hook', new_callable=AsyncMock):
            await MainAgentHooks().on_tool_start(context, tool, {})
            with self.assertRaises(PermissionError):
                await MainAgentHooks().on_tool_start(context, SimpleNamespace(name=tool.name, handler=None), {})

    async def test_failed_tool_suppresses_false_success_from_agent(self):
        event = self.turn().event
        self.gateway.command = lambda *args: '指令执行失败，未更改配置。'

        async def downstream():
            turn = TurnTools(self.guard, self.gateway, event)
            await turn.execute('private_persona_update', {'note': 'A'})
            await event.send(event.plain_result('搞定了，已经修改。'))

        await self.gateway.run(event, downstream)
        self.assertEqual(event.sent, ['【系统】指令执行失败，未更改配置。'])
        self.assertNotIn('private_persona_notes', self.gateway.load()[0])

    async def test_memory_tools_reuse_confirmation_and_delete_real_fixture_rows(self):
        from unittest.mock import Mock
        from fat_fish_guard.main import FatFishGuard
        from fat_fish_guard.policy import Confirmations
        rows = [{'id': 17}]
        async def delete(ids):
            rows[:] = [row for row in rows if row['id'] not in ids]

        engine = SimpleNamespace(batch_delete_memories=AsyncMock(side_effect=delete),
                                 _invalidate_search_cache=Mock())
        memory = SimpleNamespace(memory_engine=engine,
            conversation_manager=SimpleNamespace(clear_session=AsyncMock()),
            _ensure_plugin_ready=AsyncMock(return_value=(True, None)))
        self.guard.context = SimpleNamespace(
            get_registered_star=lambda _: SimpleNamespace(activated=True, star_cls=memory),
            conversation_manager=SimpleNamespace(new_conversation=AsyncMock()))
        self.guard.confirm = Confirmations()
        self.guard.rows = AsyncMock(side_effect=lambda *_: list(rows))
        self.guard.handle = lambda *args: FatFishGuard.handle(self.guard, *args)

        with patch.dict('sys.modules', {'astrbot.core.feiyu_p2c': SimpleNamespace(gateway=self.gateway)}), \
             patch('fat_fish_guard.main.protect_memory_engine'):
            apply = self.turn()
            apply.event.set_extra('_feiyu_access_checked', True)
            await apply.execute('memory_reset', {})
            self.assertEqual(rows, [{'id': 17}])
            confirm = self.turn()
            confirm.event.set_extra('_feiyu_access_checked', True)
            await confirm.execute('confirm_action', {})
        self.assertEqual(rows, [])
        self.guard.context.conversation_manager.new_conversation.assert_awaited_once()
        self.assertIn('memory reset', await self.turn().execute('get_recent_actions', {}))

    async def test_turn_removes_management_tools_at_budget(self):
        turn = self.turn()
        request = SimpleNamespace(func_tool=None)
        turn.install(request)
        query = request.func_tool.get_tool('get_recent_actions')
        for _ in range(4):
            await query.handler(turn.event)
        self.assertIsNone(request.func_tool.get_tool('global_admin_grant'))
        await turn.execute('global_admin_grant', {'qq': '300'})
        self.assertIsNone(self.gateway.pending.peek('100', 'private'))

    async def test_unknown_tool_replaces_false_success(self):
        from astrbot.core.provider.entities import LLMResponse
        from fat_fish_guard.main import FatFishGuard
        turn = self.turn()
        turn.install(SimpleNamespace(func_tool=None))
        response = LLMResponse(role='assistant', completion_text='搞定了。')
        context = SimpleNamespace(messages=[SimpleNamespace(role='user', content='测试'),
            SimpleNamespace(role='tool', content='error: Tool invented not found. Available tools are: fixture')])
        await FatFishGuard.report_unknown_tool(self.guard, turn.event, context, response)
        self.assertIn('未执行', response.completion_text)
        self.assertNotIn('global_admins', self.gateway.load()[0])

    async def test_reminder_query_then_update_real_task_in_scope(self):
        from p2f.reminders import ReminderStore
        self.gateway.reminders = ReminderStore(self.gateway.path.with_name('reminders.sqlite3'))
        await self.turn().execute('reminder_create', {'request': '2030-01-01 20:00提醒我查验测试任务'})
        rows = self.gateway.reminders.rows()
        self.assertEqual(len(rows), 1)
        task_id = str(rows[0]['number'])
        turn = self.turn()
        self.assertIn('编号：'+task_id, await turn.execute('reminder_query', {}))
        await turn.execute('reminder_update_text', {'id': task_id, 'text': '替换后的测试事项'})
        self.assertEqual(self.gateway.reminders.rows()[0]['body'], '替换后的测试事项')
        await self.turn('300').execute('reminder_delete', {'id': task_id})
        self.assertNotEqual(self.gateway.reminders.rows()[0]['state'], 'deleted')


if __name__ == '__main__':
    unittest.main()
