import fixture_runtime
import os,sys
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
import asyncio,json,pathlib,sys,tempfile,types,unittest
sys.path.insert(0,str(RELEASE/'src'))
from p2c.scoped_gateway import ScopedGateway

class Event:
 def __init__(self,sender='300',private=True,group='900',text=''):
  self.sender,self.private,self.group,self.message_str=sender,private,group,text;self.extra={};self.sent=[];self.stopped=False
  self.message_obj=types.SimpleNamespace(raw_message={},message_id=None)
  async def action(name,**kwargs):return [{'user_id':300}]
  self.bot=types.SimpleNamespace(call_action=action)
 def get_platform_name(self):return 'aiocqhttp'
 def get_platform_id(self):return 'fixture'
 def get_sender_id(self):return self.sender
 def get_self_id(self):return '999'
 def get_sender_name(self):return '测试群友'
 def get_group_id(self):return '' if self.private else self.group
 def is_private_chat(self):return self.private
 def get_messages(self):return []
 def get_extra(self,k,d=None):return self.extra.get(k,d)
 def set_extra(self,k,v):self.extra[k]=v
 def stop_event(self):self.stopped=True
 def plain_result(self,t):return t
 async def send(self,t):self.sent.append(t)

class Checks(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=tempfile.gettempdir());self.path=pathlib.Path(self.tmp.name)/'access.json'
  self.cfg={'owner':'100','admins':[],'blocked':[],'approved_groups':['900','901'],'groups':{'900':'mention','901':'mention'},'group_admins':{'900':['300'],'901':[]},'group_blocked':{},'group_epochs':{'900':7,'901':7},'identity_salt':'fixture'}
  self.path.write_text(json.dumps(self.cfg));self.path.with_name('instance-identity.json').write_text(json.dumps({'owner':'100'}));self.g=ScopedGateway(self.path)
 def tearDown(self):self.tmp.cleanup()
 async def issue(self,sender,private,group,text):
  e=Event(sender,private,group,text);await self.g.process(e);return e
 async def test_group_admin_never_becomes_global_or_other_group_admin(self):
  e=await self.issue('300',True,'900','/权限');self.assertEqual(e.get_extra('_feiyu_level'),1)
  e=await self.issue('300',False,'900','/权限');self.assertEqual(e.get_extra('_feiyu_level'),2)
  e=await self.issue('300',False,'901','/群聊模式 smart');self.assertIn('只有',e.sent[0])
  e=await self.issue('300',False,'900','/权限 set 400 管理员');self.assertIn('不能',e.sent[0])
 async def test_qq_group_admin_is_event_scoped_and_revocable(self):
  async def request(role, text, group='900', raw_group=900):
   e=Event('400',False,group,text)
   e.message_obj.raw_message={'post_type':'message','message_type':'group','group_id':raw_group,'user_id':400,
                              'sender':{'user_id':400,'role':role}}
   await self.g.process(e)
   return e
  first=await request('admin','/groupsettings cooldown 180')
  self.assertEqual(first.get_extra('_feiyu_level'),2)
  self.assertTrue(first.sent[0].startswith('【系统】'))
  self.assertEqual(self.g.load()[0]['cooldowns']['900'],180)
  self.assertNotIn('400',self.g.load()[0]['group_admins']['900'])
  self.assertEqual((await request('owner','/role')).get_extra('_feiyu_level'),2)
  self.assertLess((await request('admin','/role',group='901')).get_extra('_feiyu_level'),2)
  self.assertLess((await request('member','/groupsettings cooldown 181')).get_extra('_feiyu_level'),2)
  self.assertEqual(self.g.load()[0]['cooldowns']['900'],180)
  self.assertEqual((await request('admin','/role global add 500')).get_extra('_feiyu_level'),2)
  self.assertFalse(self.g.pending.pending)
  cfg,_=self.g.load()
  cfg['group_blocked']['900']=['400']
  self.g.save(cfg)
  self.assertFalse((await request('admin','/role')).sent)


 async def test_relative_admin_undo_and_short_confirmation(self):
  await self.issue('100',True,'','/role global add 500')
  cancelled=await self.issue('100',True,'','/role undo')
  self.assertIn('已取消',cancelled.sent[0])
  await self.issue('100',True,'','/confirm')
  self.assertNotIn('500',self.g.load()[1].global_admins)
  await self.issue('100',True,'','/role global add 500')
  confirmed=await self.issue('100',True,'','/confirm')
  self.assertIn('已保存',confirmed.sent[0])
  self.assertIn('已授予 500',str(self.g.recent_commands(confirmed)))
  other=await self.issue('300',True,'','/role undo')
  self.assertNotIn('500',str(self.g.recent_commands(other)))
  group=await self.issue('100',False,'900','/role undo')
  self.assertNotIn('500',str(self.g.recent_commands(group)))
  undo=await self.issue('100',True,'','/role undo')
  self.assertIn('撤销刚才授予 500',undo.sent[0])
  self.assertIn('500',self.g.load()[1].global_admins)
  await self.issue('100',True,'','/confirm')
  self.assertNotIn('500',self.g.load()[1].global_admins)
  self.assertIn('已撤销 500',self.g.recent_commands(undo)[-1])

 async def test_real_command_receipts_reach_astrbot_request_only_in_original_scope(self):
  from unittest.mock import patch
  from fat_fish_guard.main import FatFishGuard
  await self.issue('100',True,'','/role global add 500')
  await self.issue('100',True,'','/confirm')
  await self.issue('100',True,'','/role undo')
  await self.issue('100',True,'','/confirm')
  async def request(actor,private):
   e=Event(actor,private,'900','刚才做了什么？')
   e.set_extra('_feiyu_access_checked',True)
   req=types.SimpleNamespace(prompt=e.message_str,system_prompt='',contexts=[],func_tool=None)
   with patch.dict(sys.modules,{'astrbot.core.feiyu_p2c':types.SimpleNamespace(gateway=self.g)}):
    await FatFishGuard.enforce_private_persona(types.SimpleNamespace(),e,req)
   return req
  await self.issue('100',True,'','/privatepersona reset')
  followup=await request('100',True)
  self.assertIn('privatepersona reset',followup.prompt)
  self.assertIn('已恢复你本人私聊的默认表达',followup.prompt)
  self.assertIn('已授予 500',followup.prompt)
  self.assertIn('已撤销 500',followup.prompt)
  self.assertTrue(followup.prompt.endswith('刚才做了什么？'))
  self.assertNotIn('500',(await request('300',True)).prompt)
  self.assertNotIn('500',(await request('100',False)).prompt)



 async def test_context_specific_help(self):
  private=await self.issue('300',True,'900','/help');self.assertIn('/personal',private.sent[0]);self.assertNotIn('/groupmode',private.sent[0]);self.assertNotIn('/blacklist',private.sent[0])
  group=await self.issue('300',False,'900','/help');self.assertIn('/groupmode',group.sent[0]);self.assertNotIn('/personal',group.sent[0]);self.assertNotIn('/memory 或',group.sent[0])
 async def test_reversible_blacklist_executes_without_confirmation(self):
  await self.issue('300',False,'900','/黑名单 add 400')
  cfg,p=self.g.load();self.assertIn('400',p.blocked_for('900'));self.assertNotIn('400',p.blocked_for('901'))
  self.assertNotIn(('300','group:900'),self.g.pending.pending)
  self.assertEqual(cfg['group_epochs'],{'900':8,'901':7})
  await self.issue('300',False,'900','/黑名单 remove 400')
  self.assertNotIn('400',self.g.load()[1].blocked_for('900'))
 async def test_owner_group_grant_is_explicit(self):
  await self.issue('100',True,'900','/权限 set 400 管理员')
  self.assertNotIn(('100','private'),self.g.pending.pending)
  await self.issue('100',True,'900','/权限 set 400 管理员 901');await self.issue('100',True,'900','/确认')
  _,p=self.g.load();self.assertEqual(p.level('400',False,'901'),2);self.assertEqual(p.level('400',False,'900'),0);self.assertEqual(p.level('400',False,''),0)
 async def test_global_blacklist_is_owner_only_and_dominates(self):
  e=await self.issue('300',True,'900','/黑名单 add 400');self.assertIn('主人',e.sent[0])
  await self.issue('100',True,'900','/黑名单 add 300');await self.issue('100',True,'900','/confirm')
  _,p=self.g.load();self.assertEqual(p.level('300',True,'900'),-1);self.assertEqual(p.level('300',True,'901'),-1);self.assertEqual(p.level('300',True,''),-1)
 async def test_whitelist_direct_after_authorization(self):
  await self.issue('300',False,'900','/whitelist add 902')
  self.assertNotIn('902',self.g.load()[0]['approved_groups'])
  await self.issue('100',True,'900','/whitelist add 902')
  self.assertIn('902',self.g.load()[0]['approved_groups'])
  self.assertEqual(self.g.load()[0]['groups']['902'],'mention')
  self.assertNotIn(('100','private'),self.g.pending.pending)
 async def test_readding_active_group_preserves_mode(self):
  self.cfg['groups']['900']='smart';self.g.save(self.cfg)
  await self.issue('100',True,'900','/whitelist add 900');await self.issue('100',True,'900','/confirm')
  self.assertEqual(self.g.load()[0]['groups']['900'],'smart')
 async def test_persona_can_only_change_current_authorized_group(self):
  e=await self.issue('300',True,'900','/群人格 set test');self.assertIn('本群',e.sent[0])
  e=await self.issue('300',False,'901','/群人格 set test');self.assertIn('本群',e.sent[0])
  await self.issue('300',False,'900','/群人格 set 简短');await self.issue('300',False,'900','/confirm')
  cfg,_=self.g.load();self.assertEqual(cfg['group_persona_notes'],{'900':'简短'})
 async def test_group_memory_does_not_open_private_memory(self):
  e=await self.issue('300',False,'900','/群记忆 search 饺子');self.assertEqual(e.message_str,'/记忆查询 饺子');self.assertTrue(e.get_extra('_feiyu_group_memory_command'))
  e=await self.issue('300',False,'901','/群记忆 search 饺子');self.assertIn('只有',e.sent[0])
 async def test_memory_delete_retains_private_and_group_permissions(self):
  e=await self.issue('300',True,'900','/memory delete 1');self.assertEqual(e.message_str,'/记忆删除 1');self.assertFalse(e.sent)
  e=await self.issue('300',True,'900','/记忆删除 1');self.assertFalse(e.sent)
  e=await self.issue('300',False,'900','/群记忆 delete 1');self.assertEqual(e.message_str,'/记忆删除 1');self.assertTrue(e.get_extra('_feiyu_group_memory_command'))
  e=await self.issue('300',False,'901','/群记忆 delete 1');self.assertIn('只有',e.sent[0])
 async def test_numeric_role_is_scoped_owner_only(self):
  await self.issue('300',False,'900','/role set 400 2');await self.issue('300',False,'900','/confirm')
  self.assertNotIn('400',self.g.load()[0]['group_admins']['900'])
  for value,expected in [('2',2),('1',0)]:
   await self.issue('100',False,'900','/role set 400 '+value)
   await self.issue('100',False,'900','/confirm')
   self.assertEqual(self.g.load()[1].level('400',False,'900'),expected)
  self.assertEqual(self.g.load()[1].level('400',False,''),0)
 async def test_private_persona_is_self_only_private_and_direct(self):
  e=await self.issue('300',False,'900','/私聊人格 set 活泼');self.assertIn('本人私聊',e.sent[0])
  e=await self.issue('400',True,'900','/privatepersona set 活泼');self.assertIn('添加',e.sent[0])
  await self.issue('300',True,'900','/privatepersona set 活泼一点')
  self.assertEqual(self.g.load()[0]['private_persona_notes'],{'300':'活泼一点'})
  await self.issue('300',False,'900','/confirm')
  self.assertEqual(self.g.load()[0]['private_persona_notes'],{'300':'活泼一点'})
  await self.issue('300',True,'900','/confirm')
  cfg,_=self.g.load();self.assertEqual(cfg['private_persona_notes'],{'300':'活泼一点'});self.assertNotIn('group_persona_notes',cfg)
  await self.issue('300',True,'900','/私聊人格 reset');await self.issue('300',True,'900','/confirm')
  self.assertEqual(self.g.load()[0]['private_persona_notes'],{'300':''})

 async def test_global_admin_owner_confirmation_and_scope(self):
  e=await self.issue('100',True,'900','/role global add 200')
  self.assertIn('确认',e.sent[0]);self.assertNotIn('200',self.g.load()[1].global_admins)
  await self.issue('300',True,'900','/confirm');self.assertNotIn('200',self.g.load()[1].global_admins)
  await self.issue('100',False,'900','/confirm');self.assertNotIn('200',self.g.load()[1].global_admins)
  await self.issue('100',True,'900','/confirm');self.assertIn('200',self.g.load()[1].global_admins)
  for group in ['900','901']:
   e=await self.issue('200',False,group,'/groupmode smart');self.assertIn('已保存',e.sent[0]);self.assertEqual(self.g.load()[0]['groups'][group],'smart')
  e=await self.issue('200',True,'900','/role global add 500');self.assertIn('只有主人',e.sent[0])
  e=await self.issue('200',True,'900','/role');self.assertIn('全局管理员',e.sent[0])

 async def test_attached_forward_runs_once_through_authorized_private_gateway(self):
  from p2f.forwards import ForwardReader,MARKER
  self.g.forward_reader=ForwardReader();calls=[]
  async def action(name,**kwargs):
   if name=='get_friend_list':return [{'user_id':300}]
   calls.append((name,kwargs));return {'messages':[{'sender':{'user_id':400,'nickname':'甲'},'time':1,'content':[{'type':'text','data':{'text':'周三预约'}}]}]}
  async def run():
   e=Event('300',True,'','总结聊天记录');e.bot=types.SimpleNamespace(call_action=action)
   parts=[];e.get_messages=lambda:parts
   e.message_obj.raw_message={'message':[{'type':'forward','data':{'id':'synthetic-forward'}}]}
   async def downstream():
    self.assertIn(MARKER,e.message_str);self.assertIn('甲',e.message_str)
    await e.send(e.plain_result('甲提到周三预约'))
   await self.g.run(e,downstream);return e
  first=await run();self.assertEqual(first.sent,['甲提到周三预约'])
  second=await run();self.assertIn('重复',second.sent[0]);self.assertEqual(len(calls),1);self.assertEqual(calls[0][1],{'id':'synthetic-forward'})

 async def test_natural_stranger_is_denied_without_execution(self):
  e=await self.issue('400',True,'900','把群号为902的群加入授权群')
  self.assertEqual(len(e.sent),1);self.assertIn('添加',e.sent[0]);self.assertNotIn('902',self.g.load()[0]['groups'])

 async def test_group_forward_multiple_authors_reaches_synthetic_response(self):
  from p2f.forwards import ForwardReader,MARKER
  self.g.forward_reader=ForwardReader()
  async def prepare(e):e.set_extra('_feiyu_durable_group_context',e.message_str)
  self.g.prepare_group=prepare
  async def action(name,**kwargs):
   if name=='get_group_member_list':return [{'user_id':300,'nickname':'当前用户','role':'member'}]
   return {'messages':[{'sender':{'user_id':400,'nickname':'甲'},'time':1,'message':[{'type':'text','data':{'text':'预算300元'}}]},{'sender':{'user_id':500,'nickname':'乙'},'time':2,'message':[{'type':'text','data':{'text':'周三预约'}}]}]}
  e=Event('300',False,'900','总结聊天记录');e.bot=types.SimpleNamespace(call_action=action)
  parts=[type('At',(),{'qq':'999'})(),type('Forward',(),{'id':'group-forward'})()];e.get_messages=lambda:parts
  async def downstream():
   material=e.message_str.split(MARKER+'\n',1)[1].split('\n',1)[1]
   rows=json.loads(material);answer='；'.join(r['昵称']+'：'+r['内容'] for r in rows)
   await e.send(e.plain_result(answer))
  await self.g.run(e,downstream)
  self.assertEqual(e.sent,['甲：预算300元；乙：周三预约'])
  self.assertEqual(e.session_id,'p2c7_900')


 async def test_inline_nested_forward_with_reused_parent_id(self):
  from p2f.forwards import ForwardReader
  e=Event('100',True,'','')
  e.get_messages=lambda:[type('Forward',(),{'id':'parent'})()]
  inner={'sender':{'user_id':501,'nickname':'子节点'},'message':[{'type':'text','data':{'text':'内层内容'}}]}
  outer={'sender':{'user_id':500},'message':[{'type':'forward','data':{'id':'parent','messages':[inner]}}]}
  e.message_obj.raw_message={'message':[{'type':'forward','data':{'id':'parent','content':[outer]}}]}
  async def fail(*args,**kwargs):self.fail('Inline children must not be re-fetched')
  e.bot.call_action=fail
  text,_=await ForwardReader().read(e)
  self.assertIn('内层内容',text)
  self.assertIn('子节点',text)



 async def test_outage_memory_reset_requires_confirmation_and_executes_once(self):
  from unittest.mock import AsyncMock,Mock,patch
  from fat_fish_guard.main import FatFishGuard
  from fat_fish_guard.policy import Confirmations
  async def provider(_):raise TimeoutError()
  self.g.context=types.SimpleNamespace(get_using_provider_async=provider)
  engine=types.SimpleNamespace(batch_delete_memories=AsyncMock(),_invalidate_search_cache=Mock())
  memory=types.SimpleNamespace(memory_engine=engine,conversation_manager=types.SimpleNamespace(clear_session=AsyncMock()),_ensure_plugin_ready=AsyncMock(return_value=(True,None)))
  context=types.SimpleNamespace(get_registered_star=lambda _:types.SimpleNamespace(activated=True,star_cls=memory),conversation_manager=types.SimpleNamespace(new_conversation=AsyncMock()))
  guard=types.SimpleNamespace(context=context,confirm=Confirmations(),rows=AsyncMock(side_effect=[[{'id':17}],[]]))
  async def dispatch(text):
   event=Event('100',True,'',text)
   event.unified_msg_origin='fixture:FriendMessage:100'
   async def downstream():
    result=await FatFishGuard.handle(guard,event,event.message_str,event.unified_msg_origin,'100')
    await event.send(result)
   await self.g.run(event,downstream)
   return event.sent
  with patch.dict(sys.modules,{'astrbot.core.feiyu_p2c':types.SimpleNamespace(gateway=self.g)}),patch('fat_fish_guard.main.protect_memory_engine'):
   self.assertIn('确认',(await dispatch('/memory reset'))[0])
   engine.batch_delete_memories.assert_not_awaited()
   self.assertIn('已清除',(await dispatch('/confirm'))[0])
   engine.batch_delete_memories.assert_awaited_once_with([17])
   self.assertIn('没有有效',(await dispatch('/confirm'))[0])
   engine.batch_delete_memories.assert_awaited_once()


if __name__=='__main__':
 unittest.main(verbosity=2)
