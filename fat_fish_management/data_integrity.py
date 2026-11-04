"""Cold-data checks using pinned native memory schema and consistency validator."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import subprocess
import tempfile
from types import SimpleNamespace

def historical_memory(path,root):
    return path.relative_to(root).parts[:4]==('data','plugin_data','astrbot_plugin_livingmemory','backups')

def active_indexes(root):
    return [p for p in (root/'data').rglob('*') if p.suffix.lower() in {'.index','.faiss'} and not historical_memory(p,root)]

def sqlite_checks(root):
    checked=[]
    for base in ['private','data','persona']:
        for path in (root/base).rglob('*'):
            if historical_memory(path,root):continue
            if len(path.relative_to(root).parts)>1 and path.relative_to(root).parts[:2] in {('private','config-history'),('private','index-repair-history'),('private','lifecycle-history')}:continue
            if path.is_symlink() or path.is_junction():raise ValueError('Linked user data is not supported: '+str(path.relative_to(root)))
            if path.suffix.lower() not in {'.db','.sqlite','.sqlite3'}:continue
            if not path.is_file():continue
            path.resolve().relative_to(root.resolve())
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                    raise ValueError('SQLite integrity check failed: '+str(path.relative_to(root)))
            checked.append(path.relative_to(root).as_posix())
    return checked

async def native_memory(root,release):
    data=root/'data/plugin_data/astrbot_plugin_livingmemory'
    database=data/'livingmemory.db';index_path=data/'livingmemory.index'
    cfg=root/'data/config/astrbot_plugin_livingmemory_config.json'
    if cfg.exists() and json.loads(cfg.read_text(encoding='utf-8-sig')).get('graph_memory',{}).get('enabled'):
        raise ValueError('graph_memory requires a tested native recovery/migration adapter')
    indexes=active_indexes(root)
    if any(p!=index_path for p in indexes):raise ValueError('Unsupported vector index; explicit native adapter required')
    if not database.exists():
        if index_path.exists():raise ValueError('Memory index has no matching database')
        return {'native_memory':'absent'}
    import_root=os.environ.get('FFA_VALIDATOR_IMPORT_ROOT',str(root))
    os.environ.update(ASTRBOT_ROOT=import_root,FEIYU_RUNTIME_ROOT=import_root)
    sys.path[:0]=[str(release/'astrbot'),str(release/'plugins')]
    from astrbot_plugin_livingmemory.storage.db_migration import DBMigration
    from astrbot_plugin_livingmemory.core.validators.index_validator import IndexValidator
    from astrbot_plugin_livingmemory.core.utils.faiss_path_bridge import read_index_safe
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('SELECT MAX(version) FROM db_version').fetchone()[0]!=DBMigration.CURRENT_VERSION:
            raise ValueError('Unsupported native memory schema; adapter required')
    index=None
    if index_path.exists():
        if not str(index_path).isascii() and tempfile.gettempdir().isascii():
            # Keep the pinned native reader, but confine its ASCII path bridge to
            # the caller's private temp workspace instead of the Windows temp root.
            import shutil
            from fat_fish_management.instance import protect_boundary
            with tempfile.TemporaryDirectory(prefix='integrity-index-',dir=tempfile.gettempdir()) as temporary:
                stage=Path(temporary);protect_boundary(stage)
                copy=stage/'memory.index';shutil.copy2(index_path,copy)
                index=read_index_safe(str(copy))
        else:index=read_index_safe(str(index_path))
    # No FaissVecDB.initialize: a health probe must not create tables or indexes.
    native=SimpleNamespace(embedding_storage=SimpleNamespace(index=index))
    validator=IndexValidator(str(database),native)
    status=await validator.check_consistency()
    pending,_=await validator.get_migration_status()
    if pending or not status.is_consistent or status.bm25_count!=status.documents_count:raise ValueError('Native memory index requires rebuild: '+status.reason)
    # The native validator falls back to count-only checks on non-IDMap indexes.
    if status.documents_count and (index is None or not hasattr(index,'id_map')):
        raise ValueError('Vector ID mapping cannot be verified')
    return {'native_memory':'consistent','documents':status.documents_count,'vectors':status.vector_count}

def validate(root,release):
    checked=sqlite_checks(root)
    root=root.resolve();release=release.resolve()
    with tempfile.TemporaryDirectory(prefix='integrity-import-',dir=root/'private') as temporary:
        # Native module imports initialize AstrBot defaults. Confine these side
        # effects to disposable storage, while the validator reads the real DB path.
        environment=dict(os.environ,ASTRBOT_ROOT=temporary,FEIYU_RUNTIME_ROOT=temporary,FFA_VALIDATOR_IMPORT_ROOT=temporary,PYTHONDONTWRITEBYTECODE='1')
        environment.pop('PYTHONPATH',None);environment.pop('PYTHONHOME',None)
        proc=subprocess.run([sys.executable,'-X','utf8','-B',str(Path(__file__).resolve()),'--instance',str(root),'--release',str(release)],cwd=temporary,env=environment,capture_output=True,text=True,encoding='utf-8',timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if proc.returncode:
        (root/'private').mkdir(exist_ok=True)
        (root/'private/data-integrity.log').write_text(proc.stdout+'\n'+proc.stderr,encoding='utf-8')
        raise ValueError('Native data consistency check failed; inspect private/data-integrity.log')
    result=json.loads(next(line[len('DATA_INTEGRITY='):] for line in reversed(proc.stdout.splitlines()) if line.startswith('DATA_INTEGRITY=')))
    return {'sqlite':checked,**result}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);p.add_argument('--release',type=Path,required=True);a=p.parse_args()
    print('DATA_INTEGRITY='+json.dumps(asyncio.run(native_memory(a.instance,a.release))))
