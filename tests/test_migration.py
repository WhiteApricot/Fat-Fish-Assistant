"""Repeatable native SQLite/FAISS migration fixture; no real QQ identities or keys."""
import fixture_runtime
import asyncio
from contextlib import closing
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

project=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(project/'scripts'))
release=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(release/'astrbot'),str(release/'plugins'),str(release/'src')]
spec=importlib.util.spec_from_file_location('instance',project/'scripts/instance.py')
ops=importlib.util.module_from_spec(spec);spec.loader.exec_module(ops)
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.validators.index_validator import IndexValidator
from astrbot_plugin_livingmemory.core.utils.faiss_path_bridge import read_index_safe
from astrbot_plugin_livingmemory.core.faiss_async_persist import install_async_persist,get_async_persister
from p2c.scoped_gateway import ScopedGateway
from rebuild_memory import repair_index
import numpy as np

class EmbeddingFixture:
    """Deterministic fixture vectors exercise persistence, not model quality."""
    provider_config={'embedding_model':'synthetic-fixture-v1'}
    def get_dim(self):return 8
    def get_model(self):return 'synthetic-fixture-v1'
    async def get_embedding(self,text):
        topics=['茶','桃','海洋','咖啡','猫','书','山','雨']
        v=np.array([1.0 if word in text else .01 for word in topics],dtype=np.float32)
        return (v/np.linalg.norm(v)).tolist()
    async def get_embeddings(self,texts):return [await self.get_embedding(text) for text in texts]

async def open_engine(instance):
    data=instance/'data/plugin_data/astrbot_plugin_livingmemory';data.mkdir(parents=True,exist_ok=True)
    db=FaissVecDB(str(data/'livingmemory.db'),str(data/'livingmemory.index'),EmbeddingFixture())
    await db.initialize()
    install_async_persist(db.embedding_storage,debounce_seconds=.01)
    eng=MemoryEngine(str(data/'livingmemory.db'),db,config={'graph_memory_enabled':False,'atom_enabled':False,
         'index_rebuild_batch_delay':0,'index_rebuild_request_delay':0,'index_rebuild_retry_base_delay':0})
    await eng.initialize()
    return eng,db

async def close(eng,db):
    await eng.close()
    await get_async_persister(db.embedding_storage).aclose()
    await db.close()

async def main():
    checks={}
    with tempfile.TemporaryDirectory(prefix='feiyu-migration-') as tmp:
        base=Path(tmp);first=base/'original';second=base/'迁移目标';snapshot=base/'snapshot'
        ops.initialize(first,'100',release)
        access=first/'private/p2c-access.json';permissions=json.loads(access.read_text())
        permissions.update(approved_groups=['900','901'],groups={'900':'mention','901':'mention'},group_admins={'900':['300']},blocked=['400'])
        access.write_text(json.dumps(permissions))
        asset=first/'data/plugin_data/meme_manager/packs/temp/fixture.txt';asset.parent.mkdir(parents=True);asset.write_text('synthetic retained asset')
        cache=first/'data/temp/cache.txt';cache.parent.mkdir(parents=True);cache.write_text('synthetic disposable cache')
        plugin_private=first/'data/plugins/meme_manager/image_host/config.json'
        plugin_private.write_text('{"synthetic_private_key":"synthetic-plugin-key-not-real"}')
        historical=first/'data/plugin_data/astrbot_plugin_livingmemory/backups/vunknown'
        historical.mkdir(parents=True);(historical/'livingmemory.index').write_bytes(b'opaque original historical index')
        chat=SQLiteDatabase(str(first/'data/data_v4.db'));await chat.initialize()
        conversation=await chat.create_conversation('111','fixture',content=[{'role':'user','content':'synthetic chat about tea'}],persona_id='fixture')
        cid=conversation.conversation_id
        eng,db=await open_engine(first)
        checks['empty_database']=await db.count_documents()==0
        running=first/'private/RUNNING';running.write_text('synthetic writer owns lifecycle')
        a='fixture:FriendMessage:111';b='fixture:FriendMessage:222';g='fixture:GroupMessage:900'
        ids=[]
        for text,scope in [('甲喜欢茶',a),('乙喜欢桃',b),('群里讨论海洋',g)]:
            ids.append(await eng.add_memory(text,session_id=scope,persona_id='fixture',importance=1))
        try:ops.backup(first,base/'bad-live-backup')
        except ValueError:checks['running_backup_refused']=True
        await close(eng,db);await chat.engine.dispose();running.unlink()
        (first/'private/embedding.pid').write_text('123456')
        ops.backup(first,snapshot)
        from migration_package import pack,unpack
        package=base/'native-synthetic.ffa.zip'
        pack(snapshot,package,'synthetic-native-migration-password')
        decoded=base/'decoded-snapshot'
        unpack(package,decoded,'synthetic-native-migration-password')
        checks['encrypted_native_snapshot_roundtrip']=True
        snapshot=decoded
        ops.restore(snapshot,second,release)
        checks['schema_scoped_cache_exclusion']=(second/asset.relative_to(first)).read_text()=='synthetic retained asset' and not (second/'data/temp/cache.txt').exists()
        checks['runtime_pid_not_migrated']=not (second/'private/Embedding.pid').exists()
        checks['plugin_directory_private_config_retained']=(second/plugin_private.relative_to(first)).read_bytes()==plugin_private.read_bytes()
        checks['historical_index_retained_without_active_confusion']=(second/historical.relative_to(first)/'livingmemory.index').read_bytes()==b'opaque original historical index'
        eng,db=await open_engine(second)
        found=await eng.search_memories('茶',session_id=a,persona_id='fixture')
        checks['recall_after_move']=any(x.doc_id==ids[0] for x in found)
        checks['scope_isolation']=all(x.doc_id not in ids[1:] for x in found)
        cfg,policy=ScopedGateway(second/'private/p2c-access.json').load()
        checks['identity_mapping']=cfg['owner']=='100' and cfg['approved_groups']==['900','901']
        checks['migrated_group_admin_scope']=policy.level('300',group='900')==2 and policy.level('300',group='901')==0
        checks['migrated_blacklist']=policy.level('400',friend=True,group='900')==-1
        chat=SQLiteDatabase(str(second/'data/data_v4.db'));await chat.initialize()
        restored_chat=await chat.get_conversation_by_id(cid)
        checks['native_chat_history']=restored_chat.user_id=='111' and restored_chat.content[0]['content']=='synthetic chat about tea'
        await chat.engine.dispose()
        await close(eng,db)
        # Corrupt only the test copy; the reusable repair interface retains the broken original.
        index=second/'data/plugin_data/astrbot_plugin_livingmemory/livingmemory.index'
        checks['unicode_faiss_read_before_corruption']=read_index_safe(str(index)).ntotal==len(ids)
        index.write_bytes(b'corrupt synthetic index')
        try:read_index_safe(str(index))
        except RuntimeError:checks['corruption_detected']=True
        result=await repair_index(second,EmbeddingFixture())
        checks['native_index_rebuild']=result['native_main_index_rebuilt'] and result['native_consistency']
        eng,db=await open_engine(second)
        found=await eng.search_memories('茶',session_id=a,persona_id='fixture')
        checks['recall_after_rebuild']=any(x.doc_id==ids[0] for x in found)
        await close(eng,db)
        prior=index.read_bytes()
        class FailingProvider(EmbeddingFixture):
            async def get_embedding(self,text):raise OSError('Synthetic provider outage')
            async def get_embeddings(self,texts):raise OSError('Synthetic provider outage')
        try:await repair_index(second,FailingProvider())
        except RuntimeError:checks['failed_rebuild_restores_index']=index.read_bytes()==prior and (second/'private/DIRTY').exists()
        result=await repair_index(second,EmbeddingFixture())
        checks['recovery_clears_dirty_after_validation']=result['native_consistency'] and not (second/'private/DIRTY').exists()
        for path in second.rglob('*.db'):
            with closing(sqlite3.connect(path)) as sql:assert sql.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        checks['sqlite_integrity']=True
        manifest=snapshot/'manifest.json';saved=manifest.read_bytes();bad=json.loads(saved);bad['schema']=999;manifest.write_text(json.dumps(bad))
        try:ops.restore(snapshot,base/'bad-version',release)
        except ValueError:checks['version_mismatch_no_destination']=not (base/'bad-version').exists()
        manifest.write_bytes(saved)
        memory_row=next(row for row in json.loads(saved)['files'] if row['path']=='data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db')
        memory_payload=snapshot/'payload'/memory_row['path'];saved_database=memory_payload.read_bytes()
        with closing(sqlite3.connect(memory_payload)) as sql:
            sql.execute('UPDATE db_version SET version=999');sql.commit()
        import hashlib
        changed=json.loads(saved)
        next(row for row in changed['files'] if row['path']==memory_row['path'])['sha256']=hashlib.sha256(memory_payload.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(changed))
        try:ops.restore(snapshot,base/'bad-native-schema',release)
        except ValueError:checks['native_data_version_mismatch_no_destination']=not (base/'bad-native-schema').exists()
        memory_payload.write_bytes(saved_database);manifest.write_bytes(saved)
        bad=json.loads(saved);bad['software']['astrbot']='incompatible-fixture';manifest.write_text(json.dumps(bad))
        try:ops.restore(snapshot,base/'bad-software',release)
        except ValueError:checks['software_version_mismatch_no_destination']=not (base/'bad-software').exists()
        manifest.write_bytes(saved)
        row=json.loads(saved)['files'][0];payload=snapshot/'payload'/row['path'];original=payload.read_bytes();payload.write_bytes(b'corrupt')
        try:ops.restore(snapshot,base/'bad-hash',release)
        except ValueError:checks['failed_restore_no_destination']=not (base/'bad-hash').exists()
        payload.write_bytes(original)
        try:ops.restore(snapshot,second,release)
        except ValueError:checks['newer_chats_not_overwritten']=True
        ops.initialize(base/'fresh-again','200',release)
        empty,newdb=await open_engine(base/'fresh-again')
        checks['second_fresh_instance_empty']=await newdb.count_documents()==0
        await close(empty,newdb)
    expected={'empty_database','running_backup_refused','recall_after_move','scope_isolation','identity_mapping',
              'migrated_group_admin_scope','migrated_blacklist','native_chat_history','corruption_detected',
              'native_index_rebuild','recall_after_rebuild','sqlite_integrity','version_mismatch_no_destination',
              'failed_restore_no_destination','newer_chats_not_overwritten','second_fresh_instance_empty','software_version_mismatch_no_destination',
              'failed_rebuild_restores_index','recovery_clears_dirty_after_validation','native_data_version_mismatch_no_destination','unicode_faiss_read_before_corruption',
              'schema_scoped_cache_exclusion','runtime_pid_not_migrated','encrypted_native_snapshot_roundtrip',
              'plugin_directory_private_config_retained','historical_index_retained_without_active_confusion'}
    assert set(checks)==expected and all(checks.values()),checks
    print('MIGRATION_RESULT='+json.dumps(checks))

from astrbot.core.db.vec_db.faiss_impl import embedding_storage as native_embedding_storage
from astrbot_plugin_livingmemory.core.utils import faiss_path_bridge as native_bridge
# Only redirect native temporary storage for test isolation; retain the actual
# FAISS reader/writer, Unicode path bridge and all persistence assertions.
with patch.object(native_embedding_storage,'_safe_temp_dir',return_value=tempfile.gettempdir()),patch.object(native_bridge,'safe_temp_dir',return_value=tempfile.gettempdir()):
    asyncio.run(main())
