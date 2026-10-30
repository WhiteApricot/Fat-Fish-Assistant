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
 async def test_context_specific_help(self):
  private=await self.issue('300',True,'900','/help');self.assertIn('/personal',private.sent[0]);self.assertNotIn('/groupmode',private.sent[0]);self.assertNotIn('/blacklist',private.sent[0])
  group=await self.issue('300',False,'900','/help');self.assertIn('/groupmode',group.sent[0]);self.assertNotIn('/personal',group.sent[0]);self.assertNotIn('/memory 或',group.sent[0])
 async def test_code_free_confirm_bound_actor_group_and_single_use(self):
  e=await self.issue('300',False,'900','/黑名单 add 400')
  code=self.g.pending.pending[('300','group:900')][0];self.assertNotIn(code,e.sent[0])
  await self.issue('400',False,'900','/confirm');await self.issue('300',False,'901','/confirm')
  self.assertNotIn('400',self.g.load()[1].blocked_for('900'))
  await self.issue('300',False,'900','/comfirm')
  cfg,p=self.g.load();self.assertIn('400',p.blocked_for('900'));self.assertNotIn('400',p.blocked_for('901'));self.assertEqual(cfg['group_epochs'],{'900':8,'901':7})
  e=await self.issue('300',False,'900','/confirm');self.assertIn('没有有效',e.sent[0])
 async def test_owner_group_grant_is_explicit(self):
  await self.issue('100',True,'900','/权限 set 400 管理员')
  self.assertNotIn(('100','private'),self.g.pending.pending)
  await self.issue('100',True,'900','/权限 set 400 管理员 901');await self.issue('100',True,'900','/确认')
  _,p=self.g.load();self.assertEqual(p.level('400',False,'901'),2);self.assertEqual(p.level('400',False,'900'),0);self.assertEqual(p.level('400',False,''),0)
 async def test_global_blacklist_is_owner_only_and_dominates(self):
  e=await self.issue('300',True,'900','/黑名单 add 400');self.assertIn('主人',e.sent[0])
  await self.issue('100',True,'900','/黑名单 add 300');await self.issue('100',True,'900','/confirm')
  _,p=self.g.load();self.assertEqual(p.level('300',True,'900'),-1);self.assertEqual(p.level('300',True,'901'),-1);self.assertEqual(p.level('300',True,''),-1)
 async def test_new_group_requires_owner_confirmation(self):
  await self.issue('300',False,'900','/whitelist add 902');await self.issue('300',False,'900','/confirm')
  self.assertNotIn('902',self.g.load()[0]['approved_groups'])
  await self.issue('100',True,'900','/whitelist add 902')
  self.assertNotIn('902',self.g.load()[0]['approved_groups'])
  await self.issue('300',True,'900','/confirm')
  self.assertNotIn('902',self.g.load()[0]['groups'])
  await self.issue('100',True,'900','/confirm')
  cfg,_=self.g.load();self.assertIn('902',cfg['approved_groups']);self.assertEqual(cfg['groups']['902'],'mention')
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
 async def test_numeric_role_is_scoped_owner_only(self):
  await self.issue('300',False,'900','/role set 400 2');await self.issue('300',False,'900','/confirm')
  self.assertNotIn('400',self.g.load()[0]['group_admins']['900'])
  for value,expected in [('2',2),('1',0)]:
   await self.issue('100',False,'900','/role set 400 '+value)
   await self.issue('100',False,'900','/confirm')
   self.assertEqual(self.g.load()[1].level('400',False,'900'),expected)
  self.assertEqual(self.g.load()[1].level('400',False,''),0)
 async def test_private_persona_is_self_only_private_and_confirmed(self):
  e=await self.issue('300',False,'900','/私聊人格 set 活泼');self.assertIn('本人私聊',e.sent[0])
  e=await self.issue('400',True,'900','/privatepersona set 活泼');self.assertIn('添加',e.sent[0])
  await self.issue('300',True,'900','/privatepersona set 活泼一点')
  self.assertNotIn('private_persona_notes',self.g.load()[0])
  await self.issue('300',False,'900','/confirm')
  self.assertNotIn('private_persona_notes',self.g.load()[0])
  await self.issue('300',True,'900','/confirm')
  cfg,_=self.g.load();self.assertEqual(cfg['private_persona_notes'],{'300':'活泼一点'});self.assertNotIn('group_persona_notes',cfg)
  await self.issue('300',True,'900','/私聊人格 reset');await self.issue('300',True,'900','/confirm')
  self.assertEqual(self.g.load()[0]['private_persona_notes'],{'300':''})

unittest.main(verbosity=2)
