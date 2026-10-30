import os,sys,tempfile,json
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot'),str(RELEASE/'plugins')]
_OriginalTemp=tempfile.TemporaryDirectory
class IdentityTemp(_OriginalTemp):
 def __enter__(self):
  folder=super().__enter__()
  (Path(folder)/'instance-identity.json').write_text(json.dumps({'owner':'100'}))
  return folder
tempfile.TemporaryDirectory=IdentityTemp
_bootstrap=_OriginalTemp(ignore_cleanup_errors=True)
_runtime=Path(_bootstrap.name)
(_runtime/'private').mkdir()
(_runtime/'private/instance-identity.json').write_text(json.dumps({'owner':'100'}))
(_runtime/'private/p2c-access.json').write_text(json.dumps({'owner':'100','admins':[],'blocked':[],'approved_groups':[],'groups':{}}))
(_runtime/'persona').mkdir()
(_runtime/'persona/Persona_Master.md').write_text('Synthetic public test persona')
(_runtime/'persona/Persona_Examples.json').write_text('[]')
os.environ.update(FEIYU_SOURCE_ROOT=str(RELEASE/'src'),FEIYU_RUNTIME_ROOT=str(_runtime),FEIYU_PERSONA_ROOT=str(_runtime/'persona'),ASTRBOT_ROOT=str(_runtime))
import asyncio,json,pathlib,sys,tempfile,time,types
from unittest.mock import patch,AsyncMock
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
import astrbot.api
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.platform_message_history_mgr import PlatformMessageHistoryManager
from astrbot.core.persona_mgr import PersonaManager
from p2c.scoped_gateway import ScopedGateway
from p2c.native_groups import NativeGroups

class Event:
 def __init__(self,group,sender,ident,text):
  self.group,self.sender,self.text=group,sender,text;self.extra={}
  self.unified_msg_origin='fixture:GroupMessage:p2c7_'+group
  self.message_obj=types.SimpleNamespace(message_id=ident,timestamp=time.time()-1800)
 def get_group_id(self):return self.group
 def get_platform_id(self):return 'fixture'
 def get_sender_id(self):return self.sender
 def get_sender_name(self):return '昵称'+self.sender
 def get_self_id(self):return '999'
 def get_extra(self,k,d=None):return self.extra.get(k,d)
 def set_extra(self,k,v):self.extra[k]=v

async def main():
 with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as temp:
  root=pathlib.Path(temp);config=root/'access.json';database=root/'native.db'
  cfg={'owner':'100','admins':[],'blocked':[],'approved_groups':['900','901'],'groups':{'900':'mention','901':'mention'},'group_admins':{'900':['300']},'group_blocked':{},'group_epochs':{'900':7,'901':7},'identity_salt':'fixture'}
  config.write_text(json.dumps(cfg));g=ScopedGateway(config)
  db=SQLiteDatabase(str(database));await db.initialize();db.inited=True
  conf={};acm=types.SimpleNamespace(default_conf=conf,get_conf=lambda *a:conf)
  persona=PersonaManager(db,acm);await persona.initialize()
  await persona.create_persona('feiyu-group-public-base','PUBLIC_GROUP_BASE',tools=['memorize_long_term_memory'],skills=[])
  await persona.create_persona('feiyu-v1','PRIVATE_INITIAL',tools=[],skills=[])
  g.context=types.SimpleNamespace(message_history_manager=PlatformMessageHistoryManager(db),persona_manager=persona,get_registered_star=lambda name:None)
  native=NativeGroups(g);a=Event('900','300','m1','我想吃饺子');b=Event('900','400','m2','我想吃包子');other=Event('901','300','m3','另一个群的秘密')
  for e in (a,b,other):await native.append(e,[{'type':'plain','text':e.text}])
  await native.append(a,[{'type':'plain','text':a.text}]);assert await g.context.message_history_manager.count('fixture',native.scope(a))==2
  await g.context.message_history_manager.insert('fixture','fixture:FriendMessage:300',{'type':'user','message':[{'text':'PRIVATE_SECRET'}]},sender_id='300')
  calls=[]
  async def platform_history(action,**kwargs):
   calls.append(action)
   return {'messages':[
    {'message_id':'bootstrap','time':time.time()-2400,'message_type':'group','group_id':900,'sender':{'user_id':300,'nickname':'甲'},'message':[{'type':'text','data':{'text':'同群历史背景'}}]},
    {'message_id':'foreign','time':time.time()-2400,'message_type':'group','group_id':901,'sender':{'user_id':300},'message':[{'type':'text','data':{'text':'FOREIGN_BOOTSTRAP_SECRET'}}]},
    {'message_id':'private','time':time.time()-2400,'message_type':'private','sender':{'user_id':300},'message':[{'type':'text','data':{'text':'PRIVATE_BOOTSTRAP_SECRET'}}]}]}
  a.bot=types.SimpleNamespace(call_action=platform_history)
  await asyncio.gather(native.bootstrap(a),native.bootstrap(a))
  assert calls==['get_group_msg_history']
  loaded=await native.context(a);assert '同群历史背景' in loaded and 'BOOTSTRAP_SECRET' not in loaded
  text=await native.context(a);assert '饺子' in text and '包子' in text and 'PRIVATE_SECRET' not in text and '另一个群的秘密' not in text
  settings={}
  async def get(**kwargs):return dict(settings.get(kwargs['scope_id'],{}))
  async def put(**kwargs):settings[kwargs['scope_id']]=dict(kwargs['value'])
  with patch('p2c.native_groups.sp.get_async',new=get),patch('p2c.native_groups.sp.put_async',new=put):
   await native.persona(a);assert settings[a.unified_msg_origin]['persona_id']=='feiyu-group-900'
   await persona.update_persona('feiyu-v1',system_prompt='PRIVATE_DEBUG_CHANGED')
   await native.persona(a);await native.persona(other)
   assert (await persona.get_persona('feiyu-group-900')).system_prompt=='PUBLIC_GROUP_BASE'
   assert (await persona.get_persona('feiyu-group-901')).system_prompt=='PUBLIC_GROUP_BASE'
   cfg['group_persona_notes']={'900':'本群简短'};g.save(cfg);await native.persona(a)
   assert '本群简短' in (await persona.get_persona('feiyu-group-900')).system_prompt
   assert '本群简短' not in (await persona.get_persona('feiyu-group-901')).system_prompt
  await db.engine.dispose()
  # Re-open the real native SQLite manager: no in-memory window survives.
  db=SQLiteDatabase(str(database));await db.initialize();db.inited=True
  g.context.message_history_manager=PlatformMessageHistoryManager(db);native=NativeGroups(g)
  assert '饺子' in await native.context(a)
  cfg['group_blocked']={'900':['300']};cfg['group_barriers']={'900':time.time()};g.save(cfg)
  text=await native.context(b);assert '饺子' not in text and '包子' in text
  assert '另一个群的秘密' in await native.context(other)
  cfg['group_history_from']={'900':time.time()};g.save(cfg);assert not await native.context(b)
  await db.engine.dispose()
 print({'native_SQLite_group_persistence':'passed','older_than_3_minutes':'retained','duplicate_insert':'suppressed','group_private_and_cross_group_isolation':'passed','private_persona_change_affects_group':False,'group_persona_change_is_local':True,'blacklist_retains_other_authors':True,'group_reset_context_cutoff':'passed'})
asyncio.run(main())
