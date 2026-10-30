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
import asyncio,copy,json,pathlib,sys,tempfile,types
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(RELEASE/'astrbot'))
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.astr_agent_hooks import MainAgentHooks
from astrbot.core.feiyu_p2c import gateway

class E:
 def __init__(self,level,text='danger',native=False):
  self.message_str=text;self._extras={'_feiyu_access_checked':True,'_feiyu_level':level,'_feiyu_personal_native':native};self.role='member';self.is_wake=False;self.is_at_or_wake_command=False
 def get_extra(self,key=None,default=None):return self._extras.get(key,default) if key is not None else self._extras
 def set_extra(self,k,v):self._extras[k]=v
 def get_sender_id(self):return '300'
 def get_self_id(self):return '999'
 def get_platform_name(self):return 'aiocqhttp'
 def get_messages(self):return []
 def is_private_chat(self):return True
 def get_group_id(self):return ""
 def stop_event(self):self.stopped=True

async def main():
 cfg=copy.deepcopy(DEFAULT_CONFIG);cfg['admins_id']=[];cfg['wake_prefix']=['/']
 stage=WakingCheckStage();await stage.initialize(types.SimpleNamespace(astrbot_config=cfg,db_helper=None,astrbot_config_id='fixture'))
 called=[]
 class Filter(CommandFilter):
  def filter(self,event,cfg):called.append(True);return False
 command=Filter('danger');handler=types.SimpleNamespace(event_filters=[command],handler_module_path='fixture')
 with patch('astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type',return_value=[handler]),patch('astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session',new=AsyncMock(return_value=[])):
  for level,text in [(1,'danger'),(1,'/danger'),(2,'danger'),(2,'/danger')]:await stage.process(E(level,text))
  assert not called
  await stage.process(E(3));assert len(called)==1
  command.command_name='new';await stage.process(E(1,'/new',True));assert len(called)==2
 with patch('astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type',return_value=[]),patch('astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session',new=AsyncMock(return_value=[])),patch.object(stage._umo_auto_name_recorder,'schedule'):
  for checked in (True,False):
   e=E(1,'继续聊');e.stopped=False;e.is_private_chat=lambda:False;e.get_group_id=lambda:'900'
   e.set_extra('_feiyu_smart',True);e.set_extra('_feiyu_access_checked',checked)
   await stage.process(e);assert e.stopped is (not checked)
 with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as d:
  path=pathlib.Path(d)/'access.json';path.write_text(json.dumps({'owner':'100','admins':[],'blocked':['300'],'groups':{},'approved_groups':[]}))
  old=gateway.path;gateway.path=path
  try:
   run=types.SimpleNamespace(context=types.SimpleNamespace(event=E(1)))
   with patch('astrbot.core.astr_agent_hooks.call_event_hook',new=AsyncMock()) as hook:
    try:await MainAgentHooks().on_tool_start(run,None,None)
    except PermissionError:pass
    else:raise AssertionError('revoked tool was admitted')
    hook.assert_not_called()
   cfg=json.loads(path.read_text());cfg['blocked']=[];path.write_text(json.dumps(cfg))
   with patch('astrbot.core.astr_agent_hooks.call_event_hook',new=AsyncMock()) as hook:
    for name in ('memorize_long_term_memory','web_search_deepseek','deepseek_fetch_web_page'):
     await MainAgentHooks().on_tool_start(run,types.SimpleNamespace(name=name),None)
    assert hook.await_count==3
    for level in (1,2,3):
     run.context.event=E(level)
     for name in ('execute_work','execute_shell','unknown_future_tool'):
      try:await MainAgentHooks().on_tool_start(run,types.SimpleNamespace(name=name),None)
      except PermissionError:pass
      else:raise AssertionError('Non-chat tool passed P2C boundary')
    assert hook.await_count==3
  finally:gateway.path=old
 print({'plain_and_slash_native_command_bypass':'blocked','owner_native_command':'preserved','friend_new':'preserved','revoked_tool_start':'blocked'})
asyncio.run(main())
