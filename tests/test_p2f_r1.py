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

    async def test_confirmed_existing_global_admin_can_be_referenced(self):
        cfg, _ = self.gateway.load()
        cfg['global_admins'] = ['300']
        self.gateway.save(cfg)
        await self.turn().execute('global_admin_grant', {'qq': '300'})
        await self.turn().execute('confirm_action', {})
        await self.turn().execute('undo_recent_admin_grant', {})
        self.assertEqual(self.gateway.load()[0]['global_admins'], ['300'])
        await self.turn().execute('confirm_action', {})
        self.assertEqual(self.gateway.load()[0]['global_admins'], [])

    async def test_group_grant_uses_verified_mention_and_current_group(self):
        turn = self.turn(private=False)
        At = type('At', (), {})
        wake, target = At(), At()
        wake.qq, target.qq = '999', '400'
        turn.event.get_messages = lambda: [wake, target]
        await turn.execute('group_admin_grant', {'qq':'@测试成员','group':'本群'})
        self.assertEqual(self.gateway.pending.peek('100','group:900')[2], ('add','400','900'))
        self.assertNotIn('400', self.gateway.load()[0]['group_admins']['900'])
        await self.turn(private=False).execute('confirm_action', {})
        self.assertIn('400', self.gateway.load()[0]['group_admins']['900'])

    async def test_role_query_uses_real_actor_without_creating_history(self):
        turn = self.turn()
        await turn.execute('get_my_role', {})
        self.assertIn('主人', turn.event.sent[0])
        self.assertEqual(self.gateway.command_receipts(turn.event), [])

    async def test_reminder_update_has_success_supplement(self):
        from p2f.persona_reply import command_reply
        reply = await command_reply(self.gateway, self.turn().event, '已修改提醒时间：fixture', command='reminder')
        self.assertEqual(reply, '我会按新的安排提醒你。')

    async def test_literal_markup_retry_uses_native_call_and_actual_state(self):
        from astrbot.core.provider.entities import LLMResponse
        from fat_fish_guard.main import FatFishGuard
        turn = self.turn()
        turn.request = SimpleNamespace(prompt='修改私聊人格', system_prompt='', func_tool=None)
        turn.install(turn.request)
        retry = LLMResponse(role='tool', tools_call_name=['private_persona_update'],
                            tools_call_args=[{'note':'测试表达'}])
        guard = SimpleNamespace(context=SimpleNamespace(
            get_using_provider_async=AsyncMock(return_value=SimpleNamespace(text_chat=AsyncMock(return_value=retry)))))
        literal = LLMResponse(role='assistant', completion_text='DSML invoke')
        await FatFishGuard.reject_literal_tool_markup(guard, turn.event, literal)
        self.assertEqual(self.gateway.load()[0]['private_persona_notes']['100'], '测试表达')
        self.assertTrue(turn.event.get_extra('_p2f_system_sent'))

    async def test_structured_natural_routes_share_slash_execution(self):
        import json
        from astrbot.core.provider.entities import LLMResponse

        async def run(message, command, args):
            event = self.turn().event
            event.message_str = message
            provider = SimpleNamespace(text_chat=AsyncMock(return_value=LLMResponse(
                role='assistant', completion_text=json.dumps({'command':command,'args':args}))))
            self.gateway.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
            with patch('p2f.persona_reply.send_command_reply', new_callable=AsyncMock):
                admitted = await self.gateway.process(event)
            provider.text_chat.assert_awaited_once()
            self.assertIsNone(provider.text_chat.await_args.kwargs['func_tool'])
            return event, admitted

        event, admitted = await run('开一个新对话', 'new', [])
        self.assertTrue(admitted)
        self.assertEqual(event.message_str, '/new')
        event, admitted = await run('清空所有记忆', 'memory', ['reset'])
        self.assertTrue(admitted)
        self.assertEqual(event.message_str, '/记忆重置')
        await run('将每句话末尾加喵，作为私聊人格', 'privatepersona', ['set','每句话末尾加喵'])
        self.assertEqual(self.gateway.load()[0]['private_persona_notes']['100'], '每句话末尾加喵')
        await run('撤销刚才的私聊人格修改', 'privatepersona', ['undo'])
        self.assertNotIn('100', self.gateway.load()[0]['private_persona_notes'])
        event, _ = await run('取消300的全局管理员权限', 'globaladmin', ['remove','300'])
        self.assertIn('90秒', event.sent[0])
        self.assertIsNotNone(self.gateway.pending.peek('100','private'))
        await run('不执行了', 'cancel', [])
        self.assertIsNone(self.gateway.pending.peek('100','private'))
        event, _ = await run('我是什么身份', 'role', [])
        self.assertIn('主人', event.sent[0])
        from p2f.reminders import ReminderStore
        self.gateway.reminders=ReminderStore(self.gateway.path.with_name('structured-reminders.sqlite3'))
        event, _ = await run('2030-01-01 09:00提醒我起床','reminder',['add','2030-01-01 09:00','起床'])
        self.assertIn('已创建提醒', event.sent[0])
        self.assertEqual(self.gateway.reminders.rows()[0]['body'], '起床')
        event, admitted = await run('你好吗', 'chat', [])
        self.assertTrue(admitted)
        self.assertEqual(event.sent, [])

    async def test_undo_whitelist_scope_and_one_use(self):
        async def slash(text, actor='100'):
            event=self.turn(actor).event
            event.message_str=text
            await self.gateway.process(event)
            return event
        await slash('/whitelist add 902')
        await slash('/undo','300')
        self.assertIn('902',self.gateway.load()[0]['groups'])
        event=await slash('/undo')
        self.assertIn('已撤销',event.sent[0])
        self.assertNotIn('902',self.gateway.load()[0]['groups'])
        self.assertIn('没有匹配', (await slash('/undo')).sent[0])

    async def test_undo_reminder_restores_actual_body(self):
        from p2f.reminders import ReminderStore
        self.gateway.reminders=ReminderStore(self.gateway.path.with_name('undo-reminders.sqlite3'))
        for text in ('/reminder add 2030-01-01 20:00提醒我原任务', '/reminder text 1 新任务', '/undo'):
            event=self.turn().event
            event.message_str=text
            await self.gateway.process(event)
        self.assertIn('已撤销',event.sent[0])
        self.assertEqual(self.gateway.reminders.rows()[0]['body'],'原任务')

    async def test_group_assignment_uses_wire_mention_when_model_omits_target(self):
        import json
        from astrbot.core.provider.entities import LLMResponse
        turn=self.turn(private=False)
        event=turn.event
        event.message_str='帮我把测试成员设成群管理'
        event.message_obj.raw_message={'message':[{'type':'at','data':{'qq':'999'}},
                                                  {'type':'at','data':{'qq':'400'}}]}
        provider=SimpleNamespace(text_chat=AsyncMock(return_value=LLMResponse(
            role='assistant',completion_text=json.dumps({'command':'role','args':['add']}))))
        self.gateway.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        with patch('p2f.persona_reply.send_command_reply',new_callable=AsyncMock) as supplement:
            await self.gateway.process(event)
        self.assertEqual(self.gateway.pending.peek('100','group:900')[2],('add','400','900'))
        self.assertIn('90秒',event.sent[0])
        supplement.assert_awaited_once()

    async def test_persona_pending_supplement_and_retired_affix(self):
        from p2c.private_persona import apply_affix
        from p2f.persona_reply import command_reply
        turn=self.turn()
        note='每句话之后加一个喵字'
        await turn.execute('private_persona_update',{'note':note})
        reply=await command_reply(self.gateway,turn.event,'授予群管理。90秒内发送 /confirm',command='role')
        self.assertTrue(reply.rstrip('。').endswith('喵'))
        await self.turn().execute('private_persona_reset',{})
        self.assertEqual(self.gateway.load()[0]['private_persona_retired_affixes']['100'],[False,'喵'])
        self.assertEqual(apply_affix('已经恢复默认喵。你好喵',note,remove=True),'已经恢复默认。你好')

    async def test_global_admin_undo_requires_fresh_confirmation(self):
        for text in ('/globaladmin add 300','/confirm','/undo'):
            event=self.turn().event
            event.message_str=text
            await self.gateway.process(event)
        self.assertIn('90秒',event.sent[0])
        self.assertEqual(self.gateway.load()[0]['global_admins'],['300'])
        event=self.turn().event
        event.message_str='/confirm'
        await self.gateway.process(event)
        self.assertEqual(self.gateway.load()[0].get('global_admins',[]),[])
        self.assertIsNone(self.gateway.undo_reference(event))

    async def test_undo_refuses_intervening_config_change(self):
        event=self.turn().event
        event.message_str='/whitelist add 902'
        await self.gateway.process(event)
        cfg,_=self.gateway.load()
        cfg['preferences']={'300':'详细'}
        self.gateway.save(cfg)
        event=self.turn().event
        event.message_str='/undo'
        await self.gateway.process(event)
        self.assertIn('配置已变化',event.sent[0])
        self.assertEqual(self.gateway.load()[0],cfg)

    async def test_undo_archived_memory_restores_same_scoped_record(self):
        from fat_fish_guard.main import FatFishGuard
        record={'id':17,'text':'测试事实','metadata':{'status':'active'}}
        async def get_memory(ident):
            self.assertEqual(ident,17)
            return {**record,'metadata':dict(record['metadata'])}
        async def archive(ids,**kwargs):
            self.assertEqual(ids,[17])
            record['metadata']['status']='archived'
            return 1
        async def restore(ident):
            self.assertEqual(ident,17)
            record['metadata']['status']='active'
            return True
        engine=SimpleNamespace(get_memory=get_memory,archive_memories=archive,restore_memory=restore,
                               _invalidate_search_cache=lambda:None)
        plugin=SimpleNamespace(memory_engine=engine,_ensure_plugin_ready=AsyncMock(return_value=(True,None)))
        guard=SimpleNamespace(context=SimpleNamespace(get_registered_star=lambda _:SimpleNamespace(activated=True,star_cls=plugin)),
                              rows=AsyncMock(return_value=[{'id':17}]))
        event=self.turn().event
        with patch.dict('sys.modules', {'astrbot.core.feiyu_p2c':SimpleNamespace(gateway=self.gateway)}), \
             patch('fat_fish_guard.main.protect_memory_engine'):
            await FatFishGuard.handle(guard,event,'/记忆删除 17',event.unified_msg_origin,'100')
            self.assertEqual(record['metadata']['status'],'archived')
            cfg,_=self.gateway.load()
            self.gateway.remember_undo(event,3,'memory',(),cfg,cfg,'已保存')
            undo=self.turn().event
            undo.message_str='/undo'
            self.assertTrue(await self.gateway.process(undo))
            result=await FatFishGuard.handle(guard,undo,undo.message_str,undo.unified_msg_origin,'100')
            self.assertIn('已撤销',result)
            self.assertEqual(record['metadata']['status'],'active')
            self.assertIsNone(self.gateway.undo_reference(undo))

    async def test_natural_group_grant_binds_explicit_qq_and_group_in_private(self):
        import json
        event=self.turn().event
        event.message_str='把400设成群900的管理员'
        provider=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps({'command':'role','args':['add','900','400']}))))
        self.gateway.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        with patch('p2f.persona_reply.send_command_reply',new_callable=AsyncMock):
            await self.gateway.process(event)
        self.assertEqual(self.gateway.pending.peek('100','private')[2],('add','400','900'))
        self.assertNotIn('400',self.gateway.load()[0]['group_admins']['900'])
        confirm=self.turn().event
        confirm.message_str='/confirm'
        await self.gateway.process(confirm)
        self.assertIn('400',self.gateway.load()[0]['group_admins']['900'])

    async def test_unapproved_group_has_no_admin_pending(self):
        event=self.turn().event
        event.message_str='/role add 300 987654'
        await self.gateway.process(event)
        self.assertIn('已批准的群号',event.sent[0])
        self.assertIsNone(self.gateway.pending.peek('100','private'))

    async def test_structured_websearch_uses_existing_official_tool(self):
        import json
        from fat_fish_guard.main import FatFishGuard
        event=self.turn().event
        event.message_str='帮我查一下测试城市的天气'
        provider=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps({'command':'websearch','args':[]}))))
        self.gateway.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        self.assertTrue(await self.gateway.process(event))
        self.assertEqual(event.get_extra('_p2f_search_query'),event.message_str)
        config={'provider_settings':{}}
        guard=SimpleNamespace(context=SimpleNamespace(get_config=lambda **_:config))
        async def call(runtime,**kwargs):
            self.assertEqual(kwargs['query'],event.message_str)
            self.assertEqual(runtime.context.context.get_config()['provider_settings']['websearch_deepseek_key'],['fixture'])
            return 'fixture search data'
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'fixture'}), \
             patch('astrbot.core.tools.web_search_tools.DeepSeekWebSearchTool.call',side_effect=call):
            self.assertEqual(await FatFishGuard.search_current_request(guard,event,event.message_str),'fixture search data')
        self.assertEqual(config,{'provider_settings':{}})

    async def test_natural_switch_leaves_slash_and_other_scopes_available(self):
        import json
        async def call(message,private=True,actor='100'):
            event=self.turn(actor,private=private).event
            event.message_str=message
            await self.gateway.process(event)
            return event
        await call('/naturalcommand off')
        provider=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps({'command':'role','args':[]}))))
        self.gateway.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        event=await call('我是谁')
        self.assertFalse(event.sent)
        provider.text_chat.assert_not_called()
        self.assertIn('主人',(await call('/role')).sent[0])
        self.assertIn('开启',(await call('/naturalcommand',False)).sent[0])
        await call('/naturalcommand off',False)
        await call('/naturalcommand on')
        self.assertIn('关闭',(await call('/naturalcommand',False)).sent[0])
        await call('我是什么身份')
        provider.text_chat.assert_awaited_once()

    async def test_unified_persona_numbering_overlay_and_scope_permissions(self):
        from p2c.persona_settings import effective_notes
        async def call(message,private=True,actor='100'):
            event=self.turn(actor,private=private).event
            event.message_str=message
            await self.gateway.process(event)
            return event.sent[0]
        await call('/globalpersona set 使用清楚的表达')
        await call('/persona set 私聊称呼')
        await call('/persona add 第二条')
        self.assertIn('2：第二条',await call('/persona list'))
        self.assertNotIn('第二条',' '.join(self.gateway.command_receipts(self.turn().event)))
        await call('/persona edit 2 修改后的第二条')
        await call('/persona remove 1')
        self.assertIn('没有此人格编号',await call('/persona remove 9'))
        self.assertIn('2：修改后的第二条',await call('/persona list'))
        await call('/persona undo')
        self.assertIn('1：私聊称呼',await call('/persona list'))
        await call('/persona set 群聊简洁',False)
        await call('/persona add 群聊示例',False)
        await call('/persona edit 2 群聊回答先说结论',False)
        self.assertIn('2：群聊回答先说结论',await call('/persona list',False))
        await call('/persona remove 2',False)
        await call('/persona reset',False)
        await call('/persona undo',False)
        cfg,_=self.gateway.load()
        self.assertIn('当前会话表达要求（冲突时优先）：群聊简洁',effective_notes(cfg,'','900'))
        self.assertIn('私聊称呼',effective_notes(cfg,'100'))
        self.assertEqual(effective_notes(cfg,'300'),'使用清楚的表达')
        self.assertIn('只有主人',await call('/globalpersona reset',False,'300'))
        cfg,_=self.gateway.load()
        cfg['global_admins']=['300']
        self.gateway.save(cfg)
        self.assertIn('已保存',await call('/globalpersona add 使用短句',True,'300'))
        await call('/globalpersona reset')
        cfg,_=self.gateway.load()
        self.assertEqual(effective_notes(cfg,'','900'),'群聊简洁')
        await call('/globalpersona undo')
        cfg,_=self.gateway.load()
        self.assertIn('使用短句',effective_notes(cfg,'','900'))
        self.assertIn('群聊简洁',effective_notes(cfg,'','900'))
        self.assertIn('/globalpersona edit id content',await call('/help globalpersona'))

    async def test_structured_reminder_does_not_split_body_or_guess_missing_field(self):
        import json
        from p2f.reminders import ReminderStore
        self.gateway.reminders=ReminderStore(self.gateway.path.with_name('separate-reminders.sqlite3'))
        async def run(message,args):
            event=self.turn().event
            event.message_str=message
            provider=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
                completion_text=json.dumps({'command':'reminder','args':args}))))
            self.gateway.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
            with patch('p2f.persona_reply.send_command_reply',new_callable=AsyncMock):
                await self.gateway.process(event)
            return event
        await run('叫我起床',['add','起床'])
        self.assertEqual(self.gateway.reminders.rows(),[])
        await run('2030-01-01 09:00 别忘了提醒别人开会',['add','2030-01-01 09:00','提醒别人开会'])
        self.assertEqual(self.gateway.reminders.rows()[0]['body'],'提醒别人开会')

    async def test_help_menu_is_one_english_command_per_line(self):
        async def issue(text):
            event=self.turn().event
            event.message_str=text
            await self.gateway.process(event)
            return event.sent[0].removeprefix('【系统】')
        primary=(await issue('/help')).splitlines()
        self.assertIn('/persona',primary)
        self.assertTrue(all(line.startswith('/') and ' ' not in line for line in primary))
        detail=await issue('/persona help')
        self.assertEqual(detail,await issue('/help persona'))
        self.assertIn('/persona edit id content',detail.splitlines())
        self.assertIn('/persona remove id',detail.splitlines())
        self.assertIn('英文指令',await issue('/私聊人格 reset'))
        self.assertIn('英文指令',await issue('/personal 简短'))
        self.assertIn('已保存',await issue('/persona set 先说结论'))


if __name__ == '__main__':
    unittest.main()
