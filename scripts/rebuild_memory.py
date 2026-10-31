"""Offline native main-memory index repair. Never operates on a live writer."""
import argparse
import asyncio
from contextlib import closing
from contextlib import nullcontext
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path
from lifecycle_lock import exclusive
from instance import verify_release

async def repair_index(root,provider,*,already_locked=False,clear_markers=True):
    release=Path(json.loads((root/'instance.json').read_text())['release'])
    verify_release(release)
    with (nullcontext() if already_locked else exclusive(root)):
        marker=root/'private/RUNNING'
        if marker.exists():
            import psutil
            if psutil.pid_exists(int(marker.read_text())):raise ValueError('Live or ambiguous writer PID; refusing repair')
        cfg_path=root/'data/config/astrbot_plugin_livingmemory_config.json'
        cfg=json.loads(cfg_path.read_text(encoding='utf-8-sig')) if cfg_path.exists() else {}
        if cfg.get('graph_memory',{}).get('enabled'):raise ValueError('Graph-memory repair requires its own native adapter; no writes performed')
        data=root/'data/plugin_data/astrbot_plugin_livingmemory'
        database=data/'livingmemory.db';index=data/'livingmemory.index'
        database.resolve(strict=True).relative_to(root.resolve())
        sys.path[:0]=[str(release/'astrbot'),str(release/'plugins')]
        from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
        from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
        from astrbot_plugin_livingmemory.core.validators.index_validator import IndexValidator
        from astrbot_plugin_livingmemory.storage.db_migration import DBMigration
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as sql:
            version=sql.execute('SELECT MAX(version) FROM db_version').fetchone()[0]
            if version!=DBMigration.CURRENT_VERSION:raise ValueError('Unsupported native memory schema')
            history=root/'private/index-repair-history'/secrets.token_hex(8)
            history.resolve().relative_to(root.resolve());history.mkdir(parents=True)
            for name in ['RUNNING','DIRTY']:
                if (root/'private'/name).exists():shutil.copy2(root/'private'/name,history/name)
            with closing(sqlite3.connect(history/'livingmemory.db')) as backup:sql.backup(backup)
        (root/'private/DIRTY').write_text('Native index repair in progress; do not launch')
        if index.exists():
            index.resolve().relative_to(root.resolve())
            shutil.copy2(index,history/'livingmemory.index')
            assert index.read_bytes()==(history/'livingmemory.index').read_bytes()
            index.unlink()
        db=None;eng=None;failed=True
        try:
            db=FaissVecDB(str(database),str(index),provider)
            await db.initialize()
            eng=MemoryEngine(str(database),db,config={'graph_memory_enabled':False,'atom_enabled':False,
                'index_rebuild_request_delay':0,'index_rebuild_batch_delay':0,'index_rebuild_max_retries':1,'index_rebuild_retry_base_delay':0})
            await eng.initialize()
            validator=IndexValidator(str(database),db)
            result=await validator.rebuild_indexes(eng,force_full_vector=True)
            if not result['success'] or result.get('partial'):raise RuntimeError('Native rebuild incomplete; originals retained in repair history')
            if not (await validator.check_consistency()).is_consistent:raise RuntimeError('Native rebuilt index failed consistency check')
            await eng.close();eng=None
            await db.embedding_storage.save_index()
            await db.close();db=None
            failed=False
        finally:
            try:
                if eng is not None:
                    try:await eng.close()
                    except Exception:pass
                if db is not None:
                    try:await db.close()
                    except Exception:pass
            finally:
                if failed:
                    with closing(sqlite3.connect(history/'livingmemory.db')) as original,closing(sqlite3.connect(database)) as restored:
                        original.backup(restored)
                    if (history/'livingmemory.index').exists():shutil.copy2(history/'livingmemory.index',index)
                    elif index.exists():index.unlink()
                    (history/'FAILED').write_text('Original database/index restored; DIRTY retained until verified recovery')
        for path in (root/'data').rglob('*.db'):
            from data_integrity import historical_memory
            if historical_memory(path,root):continue
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as sql:
                if sql.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Other database corrupt; dirty state retained')
        if clear_markers:
            marker.unlink(missing_ok=True)
            (root/'private/DIRTY').unlink(missing_ok=True)
        return {'native_main_index_rebuilt':True,'native_consistency':True,'prior_files_retained':True}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--instance',type=Path,required=True);p.add_argument('--embedding-provider',required=True);a=p.parse_args();root=a.instance.resolve()
    release=Path(json.loads((root/'instance.json').read_text())['release'])
    os.environ['ASTRBOT_ROOT']=str(root)
    sys.path.insert(0,str(release/'astrbot'))
    from astrbot.core.provider.sources.openai_embedding_source import OpenAIEmbeddingProvider
    config=json.loads((root/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    selected=next(x for x in config['provider'] if x['id']==a.embedding_provider and x['type']=='openai_embedding')
    async def main():
        provider=OpenAIEmbeddingProvider(selected,{})
        try:print(json.dumps(await repair_index(root,provider)))
        finally:await provider.client.close()
    asyncio.run(main())
