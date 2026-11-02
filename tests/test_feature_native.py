"""Opt-in native plugins, SQLite/FAISS and OneBot serialization; synthetic identities.

FEIYU_TEST_RELEASE and FFA_FEATURE_WORKSPACE must point to independent local test
directories. FFA_TEST_KEY_FILE optionally enables real-model extraction of only
the synthetic messages below; credentials and model responses are never printed.
"""
import asyncio
import base64
import hashlib
import importlib
import io
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock,patch

PROJECT=Path(__file__).resolve().parents[1]
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
WORK=Path(os.environ['FFA_FEATURE_WORKSPACE']).resolve()
WORK.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local')
ROOT=WORK/'native-plugins'
if ROOT.exists():raise ValueError('Choose a fresh synthetic workspace')
(ROOT/'data').mkdir(parents=True);(ROOT/'private').mkdir()
(ROOT/'data/cmd_config.json').write_text('{"dashboard":{"host":"127.0.0.1","port":16185}}')
os.environ.update(ASTRBOT_ROOT=str(ROOT),FFA_RUNTIME_ROOT=str(ROOT),FEIYU_RUNTIME_ROOT=str(ROOT))
sys.path[:0]=[str(PROJECT/'src'),str(RELEASE/'astrbot'),str(RELEASE/'plugins')]
from ffa_local_credentials import install_capture,capture_generated
install_capture(ROOT)
from astrbot.core import astrbot_config
capture_generated(ROOT,astrbot_config)
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Image,Plain
from astrbot.core.message.message_event_result import MessageChain,MessageEventResult,ResultContentType
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.tools.memory_memorize_tool import MemoryMemorizeTool
from fat_fish_guard.experience import protect_memory_engine,protect_memorize_tool,MemeCadence,MEMORY_INSTRUCTION

class Embedding:
    def get_dim(self):return 64
    async def get_embedding(self,text):
        vector=[0.]*64
        for char in text:vector[int(hashlib.sha256(char.encode()).hexdigest()[:8],16)%64]+=1
        norm=sum(x*x for x in vector)**.5 or 1
        return [x/norm for x in vector]
    async def get_embeddings_batch(self,texts,**kwargs):return [await self.get_embedding(t) for t in texts]

class Event:
    def __init__(self,text,sender='100'):
        self.message_str=text;self.sender=sender;self.unified_msg_origin='fixture:FriendMessage:'+sender;self.extra={}
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def set_extra(self,key,value):self.extra[key]=value
    def get_sender_id(self):return self.sender
    def get_sender_name(self):return '合成用户'+self.sender
    def get_platform_name(self):return 'aiocqhttp'
    def get_group_id(self):return ''
    def get_message_type(self):return MessageType.FRIEND_MESSAGE
    def get_result(self):return self.extra.get('result')

async def memory_checks():
    data=ROOT/'memory';data.mkdir()
    db=FaissVecDB(str(data/'livingmemory.db'),str(data/'livingmemory.index'),Embedding())
    engine=MemoryEngine(str(data/'livingmemory.db'),db,config={'graph_memory_enabled':False,'atom_enabled':False,'index_rebuild_request_delay':0,'index_rebuild_batch_delay':0,'memory_dedup':{'enabled':True}})
    await db.initialize();await engine.initialize();protect_memory_engine(engine)
    context=types.SimpleNamespace()
    processor=MemoryProcessor(config={})
    tool=MemoryMemorizeTool(context=context,config_manager={'access_control':{'whitelist_enabled':True,'allowed_ids':'100,200'},'filtering_settings':{'memory_scope_mode':'session'}},memory_engine=engine,memory_processor=processor)
    protect_memorize_tool(tool)
    async def write(text,memory,sender='100',**kwargs):
        wrapper=types.SimpleNamespace(context=types.SimpleNamespace(event=Event(text,sender)))
        with patch('astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id',new=AsyncMock(return_value='fixture')):
            return json.loads(await tool.call(wrapper,memory,**kwargs))
    try:
        first=await write('我一直喜欢无糖红茶','本人长期喜欢无糖红茶',importance=.8)
        assert first['memorized'] and first['id']
        duplicate=await write('我一直喜欢无糖红茶','本人长期喜欢无糖红茶',importance=.8)
        assert duplicate['id']==first['id'],'Native tool duplicate must reuse the same row'
        other=await write('我一直喜欢无糖红茶','本人长期喜欢无糖红茶','200',importance=.8)
        assert other['id']!=first['id']
        unauthorized=await write('我一直喜欢无糖红茶','本人长期喜欢无糖红茶','300',importance=.8)
        assert not unauthorized['memorized']
        for text in ['今天吃了面','哈哈哈','记住我的密码是 Synthetic123']:
            assert not (await write(text,'本人喜欢红茶'))['memorized']
        found=await engine.search_memories('红茶',session_id='fixture:FriendMessage:100',persona_id='fixture')
        assert any(x.doc_id==first['id'] for x in found) and all(x.doc_id!=other['id'] for x in found)
        assert await engine.update_memory(first['id'],{'content':'本人现在长期喜欢无糖绿茶','importance':1.0})
        rows=await engine.get_session_memories('fixture:FriendMessage:100')
        assert len(rows)==1 and '绿茶' in rows[0].get('text',rows[0].get('content',''))
        assert rows[0]['metadata']['persona_summary']=='本人现在长期喜欢无糖绿茶'
        assert rows[0]['metadata']['canonical_summary']=='本人现在长期喜欢无糖绿茶'
        assert rows[0]['metadata']['key_facts']==[]
        updated_id=int(rows[0]['id'])
        assert await engine.archive_memories([updated_id],audit={'archived_by':'synthetic_self'})==1
        assert not await engine.search_memories('绿茶',session_id='fixture:FriendMessage:100',persona_id='fixture')
        model_results={}
        key_file=os.environ.get('FFA_TEST_KEY_FILE')
        if key_file:
            from openai import AsyncOpenAI
            key=Path(key_file).read_text(encoding='utf-8-sig').strip()
            client=AsyncOpenAI(api_key=key,base_url='https://api.deepseek.com/v1',timeout=90,max_retries=0)
            try:
                cases=[('preference','我一直喜欢喝无糖红茶，平时不喝咖啡',True),('project','我计划未来两年持续开发一个家庭园艺项目',True),('chatter','哈哈，今天午饭吃了面',False),('secret','记住我的密码是 SyntheticOnly123',False)]
                for label,text,expected in cases:
                    response=await client.chat.completions.create(model='deepseek-chat',messages=[{'role':'system','content':'正常简短聊天，按当前策略使用记忆工具。'+MEMORY_INSTRUCTION},{'role':'user','content':text}],tools=[{'type':'function','function':{'name':tool.name,'description':tool.description,'parameters':tool.parameters}}],tool_choice='auto',temperature=0,max_tokens=500)
                    calls=response.choices[0].message.tool_calls or []
                    assert bool(calls)==expected,'Unexpected model memory decision: '+label
                    for call in calls:
                        args=json.loads(call.function.arguments)
                        assert (await write(text,**args))['memorized']
                    model_results[label]='pass'
            finally:await client.close()
        return {'native_memory_write_without_command':True,'native_dedup':True,'scope_isolation':True,'unauthorized_rejected':True,'correction_and_forgetting':True,'real_model':model_results or 'not_requested'}
    finally:
        await engine.close();await db.embedding_storage.save_index();await db.close()

async def meme_checks():
    # Import existing plugin without its Star constructor: its actual response,
    # selection, decoration and OneBot serialization methods remain unchanged.
    module=types.ModuleType('ffa_meme_test');module.__path__=[str(RELEASE/'plugins/meme_manager')];sys.modules[module.__name__]=module
    MemeSender=importlib.import_module('ffa_meme_test.main').MemeSender
    sender=object.__new__(MemeSender)
    sender.config={'generation':{'markup':{'enable_alternative':False,'enable_repeated_detection':False},'matching':{'enable_loose_matching':False}}}
    sender.category_mapping={'happy':'开心庆祝'};sender.trigger_scope='only_chat_llm'
    sender.emotions_probability=100;sender.emotion_llm_enabled=True;sender.emotion_llm_context_turns=4;sender.emotion_llm_inject_persona=False
    sender.emotion_llm_provider_id='';sender.max_memes_per_message=1
    sender.remove_invalid_alternative_markup=False;sender.filter_all_tags=False
    sender.convert_static_to_gif=False;sender.enable_mixed_message=True;sender.mixed_message_probability=100
    sender.send_image_as_base64=True;sender.streaming_compatibility=True;sender.content_cleanup_rule=r'&&[a-zA-Z]*&&'
    sender._semantic_mode_active=lambda event:False
    directory=ROOT/'memes/happy';directory.mkdir(parents=True)
    asset=RELEASE/'resources/starter-memes/gifs/001_hehe.gif'
    import shutil
    shutil.copy2(asset,directory/asset.name)
    sender._resolve_runtime_pack_context=lambda **kwargs:{'category_mapping':sender.category_mapping,'pack_dir':directory.parent}
    sender._get_runtime_memes_dir_for_event=lambda event:directory.parent
    sender._emotion_llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text='{"emotions":["happy"]}'))
    client=None
    if os.environ.get('FFA_TEST_KEY_FILE'):
        from openai import AsyncOpenAI
        client=AsyncOpenAI(api_key=Path(os.environ['FFA_TEST_KEY_FILE']).read_text(encoding='utf-8-sig').strip(),base_url='https://api.deepseek.com/v1',timeout=90,max_retries=0)
        async def generate(event,prompt):
            response=await client.chat.completions.create(model='deepseek-chat',messages=[{'role':'user','content':prompt}],temperature=0,max_tokens=200)
            return types.SimpleNamespace(completion_text=response.choices[0].message.content)
        sender._emotion_llm_generate=generate
    event=Event('终于把项目做完啦！');event.set_extra('meme_manager_llm_request_origin','chat')
    event.set_extra('meme_manager_emotion_context_lines',['用户：终于把项目做完啦！','助手：这下可以松口气啦'])
    response=LLMResponse(role='assistant',completion_text='这下可以松口气啦')
    try:await sender._resp_impl(event,response)
    finally:
        if client:await client.close()
    assert event.get_extra('found_emotions')==['happy']
    MemeCadence().apply(event)
    event.set_extra('result',MessageEventResult(chain=[Plain(response.completion_text)],result_content_type=ResultContentType.LLM_RESULT))
    await sender._on_decorating_result_impl(event)
    result=event.get_result()
    payload=await AiocqhttpMessageEvent._parse_onebot_json(result)
    images=[x for x in payload if x['type']=='image'];assert len(images)==1
    assert images[0]['data']['file'].startswith('base64://')
    decoded=base64.b64decode(images[0]['data']['file'][9:])
    assert decoded==asset.read_bytes()
    from PIL import Image as PillowImage
    with PillowImage.open(io.BytesIO(decoded)) as picture:assert picture.n_frames==8 and picture.size==(512,512)
    return {'automatic_emotion_selection_without_instruction':True,'real_emotion_model':bool(client),'onebot_image_payload':True,'gif_bytes_unchanged':True,'qq_client_visual_confirmation':False}

async def main():
    result={'memory':await memory_checks(),'memes':await meme_checks()}
    (WORK/'native-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('NATIVE_FEATURE_REPAIRS_PASS')

asyncio.run(main())
