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
import asyncio,json,pathlib,sys,tempfile,types
sys.path.insert(0,str(RELEASE/'astrbot'))
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage,MessageMember,Group
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.message_type import MessageType
from astrbot.core.message.components import Plain
from astrbot.core.feiyu_p2c import gateway
from p2c.dedup import SeenMessages

async def main():
 with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as folder:
  config=pathlib.Path(folder)/'access.json';config.write_text(json.dumps({'owner':'100','admins':[],'blocked':['400'],'groups':{},'approved_groups':['900']}))
  original=gateway.path;original_seen=gateway.seen;gateway.path=config;gateway.seen=SeenMessages(config.with_suffix(".seen.db"))
  hits=[]
  scheduler=PipelineScheduler.__new__(PipelineScheduler)
  async def provider():return None
  scheduler.ctx=types.SimpleNamespace(plugin_manager=types.SimpleNamespace(context=types.SimpleNamespace(get_using_provider_async=provider)))
  async def downstream(e):hits.append(e.get_sender_id())
  scheduler._process_stages=downstream
  try:
   for sender,group in [('400',''),('300','900'),('100','')]:
    obj=AstrBotMessage();obj.type=MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    obj.self_id='999';obj.sender=MessageMember(sender);obj.message=[Plain(text='fixture')];obj.message_id='fixture-'+sender;obj.raw_message={};obj.group_id=group
    event=AstrMessageEvent('fixture',obj,PlatformMetadata('aiocqhttp','fixture','feiyu-onebot'),group or sender)
    async def action(name):return []
    event.bot=types.SimpleNamespace(call_action=action)
    async def send(message):raise AssertionError('No real send expected')
    event.send=send
    await scheduler.execute(event)
   assert hits==['100'],hits
   print({'real_scheduler_pre_plugin_boundary':'passed','blocked_events_reaching_downstream':0,'QQ_messages_sent':0})
  finally:gateway.path=original;gateway.seen=original_seen
asyncio.run(main())
