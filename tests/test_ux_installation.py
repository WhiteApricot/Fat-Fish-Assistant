"""Opt-in actual pinned installation/services and encrypted migration, synthetic identities only.

Requires Windows localhost/process access and existing hash-checked download caches.
No QQ login, cloud API request or production instance operation is performed.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'scripts'))
import controller as c
import psutil

p=argparse.ArgumentParser();p.add_argument('--workspace',required=True,type=Path);p.add_argument('--cache',required=True,type=Path);p.add_argument('--bootstrap-package',required=True,type=Path);p.add_argument('--resume',action='store_true');a=p.parse_args()
workspace=a.workspace.resolve()
workspace.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local')
if workspace.exists() and not a.resume:raise ValueError('Choose a new validation workspace; never overwrite a previous run')
workspace.mkdir(parents=True,exist_ok=a.resume)
os.environ.update(TEMP=str(workspace),TMP=str(workspace),PYTHONDONTWRITEBYTECODE='1')
previous=c.read(workspace/'results.json') if a.resume else {}
checks=previous.get('checks',{});events=previous.get('events',[])
if a.resume and not checks.get('fresh_independent_install'):raise ValueError('Only a verified synthetic installation can resume')

def mark(name,value=True):
    checks[name]=value
    (workspace/'results.json').write_text(json.dumps({'checks':checks,'events':events},indent=2))
    print(name,value,flush=True)

def call(root,*argv,payload=None,success=True):
    record=c.state(root);python=c.resolve(root,record.get('chat_python','bootstrap-python-3.12.3/tools/python.exe'))
    proc=subprocess.run([str(python),'-B',str(PROJECT/'ffa.py'),'--root',str(root),*map(str,argv)],input=json.dumps(payload) if payload is not None else None,text=True,capture_output=True,env=c.clean_environment(),timeout=1800)
    (workspace/('command-'+str(len(events))+'.log')).write_text(proc.stdout+'\n'+proc.stderr,encoding='utf-8')
    result=json.loads(proc.stdout)
    events.append({'command':list(map(str,argv)),'code':proc.returncode})
    if success and not result['ok']:raise AssertionError(result['errors'])
    return result

def prepare(name,no_instance=False):
    existing=workspace/name
    if a.resume and existing.exists():
        c.owned_root(existing)
        record=c.state(existing)
        if record.get('instance'):
            runtime=c.instance(existing,record)
            if c.read(runtime/'private/instance-identity.json')['owner']!='100' or (runtime/'private/MIGRATED-OUT.json').exists():raise ValueError('Refusing to reuse non-synthetic or transferred instance')
        return existing
    root=c.owned_root(workspace/name,True);(root/'cache').mkdir();shutil.copy2(a.bootstrap_package,root/'cache'/a.bootstrap_package.name)
    c.shell(root,'Bootstrap-Python.ps1','-Destination',root/'bootstrap-python-3.12.3','-Cache',root/'cache')
    opts=['install','--owner','100','--components','chat' if no_instance else 'chat,embedding,speech','--cache',a.cache,'--offline','--accept-external-licenses']
    if no_instance:opts+=['--no-instance']
    call(root,*opts)
    return root

def freeport():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

source=None;target=None
try:
    print('Resuming verified synthetic installation' if a.resume else 'Preparing new independent runtime and installation from verified cache',flush=True)
    source=prepare('source')
    mark('fresh_independent_install')
    record=c.state(source);runtime=c.instance(source,record);python=c.chat_python(source,record)
    before=(runtime/'private/instance-identity.json').read_bytes()
    call(source,'install','--owner','100','--components','chat,embedding,speech','--cache',a.cache,'--offline','--accept-external-licenses')
    mark('repeat_install_preserves_identity',before==(runtime/'private/instance-identity.json').read_bytes())
    if not (runtime/'private/onebot-token.txt').exists():call(source,'config','initialize-chat','--onebot-port',str(freeport()))
    call(source,'config','apply',payload={'dashboard_port':freeport(),'deepseek_api_key':'synthetic-ux-key-not-a-real-cloud-credential'})
    subprocess.run([str(python),'-B',str(PROJECT/'scripts/configure_local_models.py'),'--instance',str(runtime),'--embedding-port',str(freeport()),'--speech-port',str(freeport())],check=True,env=c.clean_environment())
    started=call(source,'daily-start')['data'];mark('actual_core_ready',started['core_ready'])
    mark('actual_embedding_speech_ready',not started['warnings'])
    pid=int((runtime/'private/RUNNING').read_text())
    call(source,'daily-start');mark('repeat_start_reuses_chat',pid==int((runtime/'private/RUNNING').read_text()))
    call(source,'stop');mark('native_clean_stop',(runtime/'private/STOPPED').exists() and not (runtime/'private/RUNNING').exists())
    call(source,'daily-start');mark('restart_after_clean_stop')
    from manage import process
    proc=process(runtime);proc.kill();proc.wait(timeout=30)
    mark('abrupt_termination_left_running',(runtime/'private/RUNNING').exists())
    call(source,'daily-start');mark('actual_stale_running_recovered',int((runtime/'private/RUNNING').read_text())!=pid and bool(list((runtime/'private/lifecycle-history').glob('*/RUNNING'))))
    call(source,'stop');(runtime/'private/DIRTY').write_text('synthetic interrupted shutdown after clean stop')
    call(source,'daily-start');mark('actual_valid_memory_dirty_recovered',not (runtime/'private/DIRTY').exists())
    call(source,'stop')
    # Two real manager processes race for the same installation and lifecycle.
    command=[str(python),'-B',str(PROJECT/'ffa.py'),'--root',str(source),'daily-start']
    processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=c.clean_environment()) for _ in range(2)]
    results=[json.loads(proc.communicate(timeout=600)[0]) for proc in processes]
    mark('concurrent_requests_safely_serialized',any(r['ok'] for r in results) and all(r['ok'] or r['code']==24 for r in results))
    active=process(runtime);mark('one_chat_lifecycle',active is not None)
    call(source,'stop')
    # Synthetic persisted data spans private, plugin configs/data/resources and custom roots.
    permissions=c.read(runtime/'private/p2c-access.json');permissions.update(admins=['200'],blocked=['400'],approved_groups=['900'],groups={'900':'mention'},group_admins={'900':['300']})
    c.atomic(runtime/'private/p2c-access.json',permissions)
    fixtures={'data/plugin_data/custom/settings.json':'{"synthetic":true}','data/plugins/astrbot_plugin_livingmemory/ux-resource.txt':'synthetic plugin-owned asset','assets/custom.txt':'synthetic resource','models/custom-user.txt':'synthetic custom model note'}
    for name,value in fixtures.items():path=runtime/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
    c.atomic(runtime/'data/config/ux-paths.json',{'asset':str(runtime/'assets/custom.txt')})
    with closing(sqlite3.connect(runtime/'data/ux-history.sqlite3')) as db:
        db.execute('create table chats(message text)');db.execute("insert into chats values ('synthetic private and group history')");db.commit()
    package=workspace/'synthetic.ffa.zip'
    exported=call(source,'migration-export','--destination',package,payload={'password':'synthetic-migration-password'})['data']
    mark('encrypted_single_package',exported['encrypted'] and package.exists())
    mark('package_sha256',exported['sha256']==c.digest(package))
    mark('source_suspended',call(source,'daily-start',success=False)['code']==24)
    target=prepare('target-new-host',True)
    imported=call(target,'migration-import','--package',package,'--confirm-source-offline','--cache',a.cache,'--offline',payload={'password':'synthetic-migration-password'})['data']
    restored=c.instance(target,c.state(target));mark('encrypted_import_complete',imported['restored'])
    mark('identity_and_permissions',c.read(restored/'private/p2c-access.json')==permissions and (restored/'private/instance-identity.json').read_bytes()==before)
    mark('private_api_key_preserved',(restored/'private/deepseek-key.txt').read_bytes()==(runtime/'private/deepseek-key.txt').read_bytes())
    mark('all_custom_fixtures',all((restored/name).read_text()==value for name,value in fixtures.items()))
    mark('active_paths_relocated',c.read(restored/'data/config/ux-paths.json')['asset']==str(restored/'assets/custom.txt'))
    with closing(sqlite3.connect(restored/'data/ux-history.sqlite3')) as db:mark('sqlite_history_preserved',db.execute('select message from chats').fetchone()[0]=='synthetic private and group history')
    mark('target_rejects_overwrite',call(target,'migration-import','--package',package,'--confirm-source-offline',payload={'password':'synthetic-migration-password'},success=False)['code']!=0)
    mark('restored_actual_services_ready',call(target,'daily-start')['data']['core_ready'])
    call(target,'stop')
    mark('passed',all(checks.values()))
finally:
    for root in [target,source]:
        if root is not None:
            try:call(root,'stop')
            except Exception as exc:print('Cleanup requires attention:',type(exc).__name__,str(root),flush=True)
    (workspace/'results.json').write_text(json.dumps({'checks':checks,'events':events},indent=2))
if not checks.get('passed'):raise SystemExit(1)
