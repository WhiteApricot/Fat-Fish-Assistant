"""Actual native index recovery and fail-closed faults in a new synthetic instance."""
import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'scripts'))
import controller as c
from lifecycle_lock import exclusive

p=argparse.ArgumentParser();p.add_argument('--software-root',type=Path,required=True);p.add_argument('--workspace',type=Path,required=True);a=p.parse_args()
software=a.software_root.resolve();workspace=a.workspace.resolve()
for path in [software,workspace]:path.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local/ux-validation')
if workspace.exists():raise ValueError('Refusing to overwrite a previous test run')
root=c.owned_root(workspace,True);(root/'logs').mkdir()
os.environ.update(TEMP=str(root),TMP=str(root),PYTHONDONTWRITEBYTECODE='1')
original=c.state(software)
record={'schema_version':1,'project':'Fat-Fish-Assistant','release':str(c.resolve(software,original['release'])),'chat_python':str(c.chat_python(software,original)),'instance':'instance','components':{}}
for name in ['python','astrbot','plugins','embedding','embedding-engine','embedding-model']:
    info=dict(original['components'][name]);info['path']=str(c.resolve(software,info['path'])) if 'path' in info else 'instance'
    record['components'][name]=info
c.atomic(root/'installation.json',record)
python=c.chat_python(root,record);runtime=root/'instance';results={};number=0

def api(*args,success=True):
    global number
    number+=1
    proc=subprocess.run([str(python),'-B',str(PROJECT/'ffa.py'),'--root',str(root),*map(str,args)],capture_output=True,text=True,env=c.clean_environment(),timeout=900)
    (root/'logs'/('case-'+str(number)+'.log')).write_text(proc.stdout+'\n'+proc.stderr,encoding='utf-8')
    value=json.loads(proc.stdout)
    if success and not value['ok']:raise AssertionError(value['errors'])
    return value
def mark(name,value=True):
    results[name]=value;(root/'results.json').write_text(json.dumps(results,indent=2));print(name,value,flush=True)
def freeport():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
def fixture(mode):
    with (root/'logs'/('native-'+mode+'.log')).open('ab') as log:
        proc=subprocess.run([str(python),'-B',str(PROJECT/'tests/seed_ux_data.py'),mode,'--instance',str(runtime)],stdout=log,stderr=subprocess.STDOUT,env=c.clean_environment(),timeout=300)
    assert proc.returncode==0

try:
    c.invoke(root,python,'instance.py','init','--destination',runtime,'--owner','100','--release',record['release'])
    c.prepare_instance_assets(software,{'components':{k:original['components'][k] for k in ['embedding-engine','embedding-model']}},runtime)
    api('config','initialize-chat','--onebot-port',freeport())
    cfg=c.read(runtime/'data/cmd_config.json');cfg['dashboard']['port']=freeport();c.atomic(runtime/'data/cmd_config.json',cfg)
    subprocess.run([str(python),'-B',str(PROJECT/'scripts/configure_local_models.py'),'--instance',str(runtime),'--components','embedding','--embedding-port',str(freeport())],check=True,env=c.clean_environment())
    api('component','embedding','start')
    for attempt in range(60):
        try:api('component','embedding','health');break
        except AssertionError:
            if attempt==59:raise
            time.sleep(2)
    fixture('seed');mark('native_sqlite_faiss_seeded_with_actual_embedding')
    index=runtime/'data/plugin_data/astrbot_plugin_livingmemory/livingmemory.index'
    valid=index.read_bytes();(root/'known-good.index').write_bytes(valid)
    index.write_bytes(b'corrupt synthetic FAISS')
    (runtime/'private/DIRTY').write_text('synthetic incomplete native persistence')
    (runtime/'private/RUNNING').write_text('99999999')
    api('daily-start');mark('corrupt_index_automatically_rebuilt',not (runtime/'private/DIRTY').exists())
    backups=list((runtime/'private/index-repair-history').glob('*/livingmemory.index'))
    mark('corrupt_original_preserved',any(p.read_bytes()==b'corrupt synthetic FAISS' for p in backups))
    api('component','astrbot','stop');fixture('check');mark('native_recall_scope_and_history_after_repair')
    bad=runtime/'data/sqlite-corrupt-fixture.db';bad.write_bytes(b'corrupt SQLite synthetic fixture')
    (runtime/'private/DIRTY').write_text('synthetic SQLite interruption')
    mark('sqlite_corruption_refused',not api('daily-start',success=False)['ok'] and bad.read_bytes()==b'corrupt SQLite synthetic fixture' and (runtime/'private/DIRTY').exists())
    bad.unlink()  # Remove only this test's deliberately injected extra database.
    api('stop')
    prior=len(list((runtime/'private/index-repair-history').glob('*')))
    index.write_bytes(b'corrupt index with unavailable Embedding')
    record=c.state(root);record['components']['embedding']['installed']=False;c.atomic(root/'installation.json',record)
    mark('missing_embedding_refuses_dirty_recovery',not api('daily-start',success=False)['ok'] and (runtime/'private/DIRTY').exists() and len(list((runtime/'private/index-repair-history').glob('*')))==prior)
    record['components']['embedding']['installed']=True;c.atomic(root/'installation.json',record)
    api('daily-start');api('stop');mark('recovery_after_embedding_returns')
    cfgpath=runtime/'data/config/astrbot_plugin_livingmemory_config.json';config=c.read(cfgpath);config['graph_memory']={'enabled':True};c.atomic(cfgpath,config)
    (runtime/'private/DIRTY').write_text('synthetic graph-memory ambiguity')
    mark('graph_memory_refuses_auto_recovery',not api('daily-start',success=False)['ok'] and (runtime/'private/DIRTY').exists())
    config['graph_memory']['enabled']=False;c.atomic(cfgpath,config)
    with exclusive(runtime):mark('held_os_lock_refuses_start',not api('daily-start',success=False)['ok'])
    api('daily-start');api('stop')
    (runtime/'private/RUNNING').write_text(str(os.getpid()))
    mark('reused_unrelated_pid_refused',not api('daily-start',success=False)['ok'] and (runtime/'private/RUNNING').read_text()==str(os.getpid()))
    (runtime/'private/RUNNING').unlink()  # Revert the synthetic PID injection after asserting refusal.
    mark('passed',all(results.values()))
finally:
    try:api('stop')
    except Exception:print('Inspect retained test instance before cleanup',flush=True)
if not results.get('passed'):raise SystemExit(1)
