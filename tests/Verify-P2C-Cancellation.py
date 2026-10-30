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
"""No network, production DB changes or QQ sends: native cancellation boundaries."""
import asyncio, json, pathlib, sys, tempfile, types
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0,str(RELEASE/'astrbot'))
sys.path.insert(0,str(RELEASE/'plugins'))
from astrbot.api import logger
from astrbot.core.message.components import Record
from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
import astrbot.core.feiyu_p2c as integration
from p2c.scoped_gateway import ScopedGateway
from feiyu_guard.main import FeiyuGuard
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.star.star_handler import EventType
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner

async def main():
    entered=asyncio.Event()
    async def transcribe(**kwargs):entered.set();await asyncio.Event().wait()
    ctx=types.SimpleNamespace(get_using_stt_provider_async=AsyncMock(return_value=types.SimpleNamespace(get_text=transcribe)))
    stage=PreProcessStage()
    await stage.initialize(types.SimpleNamespace(astrbot_config={'provider_stt_settings':{'enable':True}},plugin_manager=types.SimpleNamespace(context=ctx)))
    chain=[Record(file='fixture.wav')]
    event=types.SimpleNamespace(get_platform_name=lambda:'aiocqhttp',get_messages=lambda:chain,unified_msg_origin='fixture',message_str='',message_obj=types.SimpleNamespace(message_str=''))
    with patch.object(Record,'convert_to_file_path',new=AsyncMock(return_value='fixture.wav')),patch('astrbot.core.pipeline.preprocess_stage.stage.ensure_wav',new=AsyncMock(return_value='fixture.wav')),patch.object(stage,'_track_temp_media'):
        task=asyncio.create_task(stage.process(event));await entered.wait();task.cancel()
        result=await asyncio.gather(task,return_exceptions=True)
        assert isinstance(result[0],asyncio.CancelledError),'STT swallowed access cancellation'
    with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as d:
        path=pathlib.Path(d)/'access.json'
        cfg={'owner':'100','admins':[],'blocked':[],'approved_groups':['900'],'groups':{'900':'smart'},'group_epochs':{'900':1}}
        path.write_text(json.dumps(cfg));gateway=ScopedGateway(path)
        guard=FeiyuGuard(types.SimpleNamespace())
        original=integration.gateway;integration.gateway=gateway
        try:
            extra={'_feiyu_access_checked':True,'_feiyu_security_epoch':1,'_feiyu_smart':True}
            e=types.SimpleNamespace(get_extra=lambda k,default=None:extra.get(k,default),get_sender_id=lambda:'300',get_group_id=lambda:'900',is_private_chat=lambda:False)
            request=types.SimpleNamespace(prompt='fixture',system_prompt='',image_urls=[])
            await guard.apply_personal_preference(e,request)
            for reason in ('blocked','epoch','mode'):
                changed=json.loads(path.read_text())
                if reason=='blocked':changed['blocked']=['300']
                elif reason=='epoch':changed['group_epochs']['900']=2
                else:changed['groups']['900']='mention'
                gateway.save(changed)
                try:await guard.apply_personal_preference(e,request)
                except asyncio.CancelledError:pass
                else:raise AssertionError('Model request admitted after '+reason)
                gateway.save(cfg)
            cfg['private_persona_notes']={'300':'轻快但认真'};gateway.save(cfg)
            for actor,private,expected in [('300',True,True),('301',True,False),('300',False,False)]:
                e=types.SimpleNamespace(get_extra=lambda k,default=None:extra.get(k,default),get_sender_id=lambda:actor,get_group_id=lambda:'' if private else '900',is_private_chat=lambda:private)
                request=types.SimpleNamespace(prompt='fixture',system_prompt='',image_urls=[])
                await guard.apply_personal_preference(e,request)
                await guard.enforce_private_persona(e,request)
                assert ('轻快但认真' in request.system_prompt) is expected
            cfg['private_persona_notes']['300']='';gateway.save(cfg)
            e=types.SimpleNamespace(get_extra=lambda k,default=None:extra.get(k,default),get_sender_id=lambda:'300',get_group_id=lambda:'',is_private_chat=lambda:True)
            request=types.SimpleNamespace(prompt='fixture',system_prompt='基础人格与检索资料',image_urls=[])
            await guard.enforce_private_persona(e,request)
            assert '已恢复默认' in request.system_prompt and '轻快但认真' not in request.system_prompt
            assert request.prompt=='fixture' and request.system_prompt.startswith('基础人格与检索资料')
            async def await_result(value):return await value
            fake=types.SimpleNamespace(run_context=types.SimpleNamespace(context=types.SimpleNamespace(event=e),messages=[]),req=types.SimpleNamespace(session_id='fixture',conversation=None,extra_user_content_parts=None,model=None),request_max_retries=0,streaming=False,provider=types.SimpleNamespace(text_chat=AsyncMock(return_value=object())),_sanitize_contexts_for_provider=lambda x:x,_func_tool_for_provider=lambda:None,_await_or_stop=await_result)
            fake._abort_signal=asyncio.Event()
            for content,expected in [('已恢复默认私聊人格','present'),('missing marker','missing')]:
                fake.run_context.messages=[{'role':'system','content':content}]
                with patch.object(gateway,'audit',new=Mock()) as audit:
                    async for _ in ToolLoopAgentRunner._iter_llm_responses(fake):pass
                    audit.assert_called_once_with('persona_wire',e,expected)
        finally:integration.gateway=original
    handler=types.SimpleNamespace(handler_module_path='fixture',handler_name='cancel',handler_full_name='fixture_cancel',event_type=EventType.OnLLMRequestEvent,handler=AsyncMock(side_effect=asyncio.CancelledError()))
    event=types.SimpleNamespace(plugins_name=None,trace=types.SimpleNamespace(record=lambda *args,**kwargs:None),is_stopped=lambda:False)
    with patch('astrbot.core.pipeline.context_utils.star_handlers_registry.get_handlers_by_event_type',return_value=[handler]):
        try:await call_event_hook(event,EventType.OnLLMRequestEvent,None)
        except asyncio.CancelledError:pass
        else:raise AssertionError('Native hook dispatcher swallowed cancellation')
    print({'native_STT_cancellation_propagates':True,'blocked_stale_and_mode_revoked_before_model':'rejected','QQ_sends':0})

asyncio.run(main())
