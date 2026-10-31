"""Synthetic native chat/memory fixture using only the test instance's local Embedding."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'scripts'))
from lifecycle_lock import exclusive
from service_health import probe

p=argparse.ArgumentParser();p.add_argument('mode',choices=['seed','check']);p.add_argument('--instance',type=Path,required=True);a=p.parse_args();root=a.instance.resolve()
root.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local/ux-validation')
os.environ.update(ASTRBOT_ROOT=str(root),FEIYU_RUNTIME_ROOT=str(root),PYTHONDONTWRITEBYTECODE='1')
probe(root,'embedding')
release=Path(json.loads((root/'instance.json').read_text())['release'])
sys.path[:0]=[str(release/'astrbot'),str(release/'plugins')]
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.provider.sources.openai_embedding_source import OpenAIEmbeddingProvider
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine

async def main():
    cfg=json.loads((root/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    selected=next(p for p in cfg['provider'] if p.get('id')=='feiyu-local-embedding')
    provider=OpenAIEmbeddingProvider(selected,{})
    data=root/'data/plugin_data/astrbot_plugin_livingmemory'
    db=FaissVecDB(str(data/'livingmemory.db'),str(data/'livingmemory.index'),provider)
    eng=MemoryEngine(str(data/'livingmemory.db'),db,config={'graph_memory_enabled':False,'atom_enabled':False,'index_rebuild_request_delay':0,'index_rebuild_batch_delay':0})
    chat=SQLiteDatabase(str(root/'data/data_v4.db'));await chat.initialize()
    await db.initialize();await eng.initialize()
    fixture=root/'private/ux-native-fixture.json'
    try:
        if a.mode=='seed':
            ids=[]
            for text,scope in [('合成用户甲喜欢喝茶','fixture:FriendMessage:111'),('合成用户乙喜欢咖啡','fixture:FriendMessage:222')]:
                ids.append(await eng.add_memory(text,session_id=scope,persona_id='fixture',importance=1))
            conversation=await chat.create_conversation('111','synthetic UX history',content=[{'role':'user','content':'synthetic native private history'}],persona_id='fixture')
            fixture.write_text(json.dumps({'ids':ids,'conversation':conversation.conversation_id}))
        else:
            expected=json.loads(fixture.read_text())
            found=await eng.search_memories('茶',session_id='fixture:FriendMessage:111',persona_id='fixture')
            assert any(x.doc_id==expected['ids'][0] for x in found)
            assert all(x.doc_id!=expected['ids'][1] for x in found)
            conversation=await chat.get_conversation_by_id(expected['conversation'])
            assert conversation.user_id=='111' and conversation.content[0]['content']=='synthetic native private history'
    finally:
        await eng.close();await db.embedding_storage.save_index();await db.close();await chat.engine.dispose();await provider.client.close()

with exclusive(root):asyncio.run(main())
print('UX_NATIVE_DATA='+a.mode+':pass')
