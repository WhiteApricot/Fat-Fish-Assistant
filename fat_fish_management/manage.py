"""Manage a local, versioned AstrBot instance. Never manages unrelated processes."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os
import socket
import shutil
import secrets
import subprocess
import sys
import time
from pathlib import Path

import psutil
from fat_fish_management.instance import verify_release,verify_plugins,copy_plugin_resources
from fat_fish_management.lifecycle_lock import exclusive
from fat_fish_management.onebot_config import listener

def recover_stale(instance):
    """Caller holds the lifecycle lock; never clear an unverified dirty state."""
    import sqlite3
    from contextlib import closing
    if (instance/'private/upgrade-journal.json').exists():
        raise ValueError('Interrupted upgrade; use recover-upgrade before lifecycle recovery')
    marker=instance/'private/RUNNING'
    if marker.exists():
        # Even a reused PID is ambiguous: leave evidence and never control it.
        if psutil.pid_exists(int(marker.read_text())):
            process(instance)  # Verify identity even when the lock is unexpectedly free.
            raise ValueError('Recorded PID still exists; identity requires investigation')
    dirty=instance/'private/DIRTY'
    if not marker.exists() and not dirty.exists():return
    from fat_fish_management.data_integrity import validate,sqlite_checks,active_indexes
    pointer=instance/'instance.json'
    if not pointer.exists():raise ValueError('Instance metadata missing; lifecycle markers retained')
    release=Path(json.loads(pointer.read_text())['release'])
    verify_release(release)
    sqlite_checks(instance)
    try:validate(instance,release)
    except (ValueError,RuntimeError):
        # A failed vector check may be repaired only through the native adapter.
        cfgpath=instance/'data/config/astrbot_plugin_livingmemory_config.json'
        cfg=json.loads(cfgpath.read_text(encoding='utf-8-sig')) if cfgpath.exists() else {}
        if cfg.get('graph_memory',{}).get('enabled'):raise ValueError('graph_memory recovery is unsupported; original state retained')
        if not (instance/'data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db').exists():raise
        expected_index=instance/'data/plugin_data/astrbot_plugin_livingmemory/livingmemory.index'
        if any(p!=expected_index for p in active_indexes(instance)):
            raise ValueError('Unsupported vector state; originals retained')
        import asyncio
        from fat_fish_management.rebuild_memory import repair_index
        from fat_fish_management.service_health import probe
        try:probe(instance,'embedding')
        except Exception as exc:raise ValueError('Embedding health unavailable; native recovery blocked; original data retained') from exc
        provider_id=cfg.get('provider_settings',{}).get('embedding_provider_id')
        providers=json.loads((instance/'data/cmd_config.json').read_text(encoding='utf-8-sig')).get('provider',[])
        selected=next((p for p in providers if p.get('id')==provider_id and p.get('type')=='openai_embedding'),None)
        if selected is None:raise ValueError('Configured Embedding provider unavailable; state retained')
        sys.path[:0]=[str(release/'astrbot'),str(release/'plugins')]
        os.environ.update(ASTRBOT_ROOT=str(instance),FEIYU_RUNTIME_ROOT=str(instance))
        from astrbot.core.provider.sources.openai_embedding_source import OpenAIEmbeddingProvider
        async def repair():
            provider=OpenAIEmbeddingProvider(selected,{})
            try:await repair_index(instance,provider,already_locked=True,clear_markers=False)
            finally:await provider.client.close()
        asyncio.run(repair())
        validate(instance,release)
    history=instance/'private/lifecycle-history'/secrets.token_hex(12)
    history.mkdir(parents=True)
    modified={name:(instance/'private'/name).stat().st_mtime for name in ['RUNNING','DIRTY'] if (instance/'private'/name).exists()}
    (history/'reason.json').write_text(json.dumps({'recovered_at':time.time(),'marker_modified_at':modified,'fault_time':'unknown; marker modification times retained','reason':'dead PID; exclusive OS lock; SQLite checks passed; native data consistency verified'}))
    for name in ['RUNNING','DIRTY']:
        path=instance/'private'/name
        if path.exists():path.rename(history/name)

def process(instance):
    marker=instance/'private/RUNNING'
    if not marker.exists():return None
    try:
        proc=psutil.Process(int(marker.read_text()))
        command=proc.cmdline()
        if not any(Path(x).name=='run_chat.py' for x in command) or str(instance) not in command:
            raise ValueError('PID identity mismatch; refusing process control')
        return proc
    except psutil.NoSuchProcess:return None

def start(instance,python):
    with exclusive(instance,'.start-lock'):
        with exclusive(instance):
            recover_stale(instance)
        return _start_locked(instance,python)

def _start_locked(instance,python):
    if (instance/'private/MIGRATED-OUT.json').exists():raise ValueError('Source is suspended after migration export; do not run the same QQ identity on both hosts')
    if (instance/'private/upgrade-journal.json').exists():raise ValueError('Interrupted upgrade; recover-upgrade before launch')
    release=Path(json.loads((instance/'instance.json').read_text())['release'])
    verify_release(release)
    verify_plugins(instance,release)
    if process(instance):raise ValueError('Already running')
    if any((instance/'private'/x).exists() for x in ['DIRTY','RUNNING']):raise ValueError('Resolve dirty or stale lifecycle marker before launch')
    cfg=json.loads((instance/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    listener(cfg)
    if cfg.get('dashboard',{}).get('host')!='127.0.0.1':raise ValueError('Dashboard must bind loopback')
    for platform in cfg.get('platform',[]):
        for key in ['ws_reverse_host','callback_server_host']:
            if key in platform and platform[key] not in ['127.0.0.1','localhost']:raise ValueError('Platform bind must be loopback')
    port=cfg.get('dashboard',{}).get('port',6185)
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.2):
            raise ValueError('Dashboard port occupied; refusing to treat another service as readiness')
    except OSError:pass
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    environment=dict(os.environ)
    environment.pop('PYTHONPATH',None);environment.pop('PYTHONHOME',None)
    environment['PYTHONNOUSERSITE']='1'
    environment['PYTHONDONTWRITEBYTECODE']='1'
    with (instance/'logs/chat.stdout.log').open('ab') as stdout,(instance/'logs/chat.stderr.log').open('ab') as stderr:
        proc=subprocess.Popen([str(python),'-X','utf8','-u',str(Path(__file__).with_name('run_chat.py')),'--instance',str(instance)],cwd=instance,stdout=stdout,stderr=stderr,creationflags=flags,env=environment)
    # Locked native dependencies can take over a minute on a cold Windows cache.
    for attempt in range(1200):
        if attempt and attempt%60==0:print('[Fat-Fish-Assistant] 正在等待 Chat 冷启动及生命周期锁交接；请保留窗口，详细日志在 instance/logs。',file=sys.stderr,flush=True)
        if proc.poll() is not None:raise RuntimeError('Chat failed startup; inspect private logs')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.2):
                owner=process(instance)
                if owner is not None and (owner.pid==proc.pid or any(parent.pid==proc.pid for parent in owner.parents())):return
        except OSError:pass
        time.sleep(.5)
    raise RuntimeError('Chat readiness timed out; use stop and inspect private logs')

def stop(instance,force=False):
    proc=process(instance)
    if not proc:return
    (instance/'private/STOP').write_text('local maintenance request')
    try:proc.wait(timeout=45)
    except psutil.TimeoutExpired:
        if not force:raise RuntimeError('Native stop timed out; retry with --force for test instance')
        proc.terminate();proc.wait(timeout=10)
        (instance/'private/DIRTY').write_text('Forced stop; native migration consistency not established')

def activate(instance,release,fail=False,crash=False):
    with exclusive(instance):
        return _activate_locked(instance,release,fail,crash)

def _activate_locked(instance,release,fail=False,crash=False):
    verify_release(release)
    if process(instance) or any((instance/'private'/x).exists() for x in ['RUNNING','DIRTY']):raise ValueError('Stop cleanly before upgrade')
    metadata=json.loads((release/'release.json').read_text())
    if metadata['schema']!=1:raise ValueError('Unsupported release schema')
    pointer=instance/'instance.json';before=pointer.read_bytes()
    journal=instance/'private/upgrade-journal.json'
    if journal.exists():raise ValueError('Interrupted upgrade requires recovery')
    suffix=secrets.token_hex(6)
    stage=instance/('.plugin-stage-'+suffix)
    previous_plugins=instance/('.plugin-rollback-'+suffix)
    current_plugins=instance/'data/plugins'
    shutil.copytree(release/'plugins',stage)
    copy_plugin_resources(current_plugins,stage,Path(json.loads(before)['release']))
    journal.write_text(json.dumps({'previous':json.loads(before),'requested':str(release),'transaction':suffix}))
    current_plugins.rename(previous_plugins)
    try:stage.rename(current_plugins)
    except Exception:
        previous_plugins.rename(current_plugins)
        raise
    candidate=json.loads(before);candidate['release']=str(release)
    temp=pointer.with_suffix('.pending')
    try:
        temp.write_text(json.dumps(candidate));temp.replace(pointer)
        if crash:os._exit(88)
        if fail:raise RuntimeError('Injected upgrade failure')
        bridges=('astrbot/astrbot/core/fat_fish_gateway.py','astrbot/astrbot/core/feiyu_p2c.py')
        identity_modules=('src/fat_fish/core/instance_identity.py','src/ffa_components/core/instance_identity.py','src/p2c/instance_identity.py')
        if not any((release/name).is_file() for name in bridges) or not any((release/name).is_file() for name in identity_modules):raise ValueError('Incomplete release')
    except Exception:
        temp.write_bytes(before);temp.replace(pointer)
        current_plugins.rename(instance/('.plugin-failed-'+suffix))
        previous_plugins.rename(current_plugins)
        journal.rename(instance/'private'/('upgrade-reverted-'+suffix+'.json'))
        raise
    (instance/'private/previous-release.json').write_bytes(before)
    journal.rename(instance/'private'/('upgrade-completed-'+suffix+'.json'))

def recover_upgrade(instance):
    with exclusive(instance):
        if process(instance) or (instance/'private/RUNNING').exists():raise ValueError('Writer must be stopped before recovery')
        journal=instance/'private/upgrade-journal.json'
        record=json.loads(journal.read_text())
        previous=Path(record['previous']['release']);verify_release(previous)
        suffix=secrets.token_hex(6);plugins=instance/'data/plugins'
        staged=instance/('.plugin-recovery-'+suffix)
        shutil.copytree(previous/'plugins',staged)
        rollback=instance/('.plugin-rollback-'+record['transaction'])
        resource_source=rollback if rollback.exists() else plugins
        copy_plugin_resources(resource_source,staged,previous)
        if plugins.exists():plugins.rename(instance/('.plugin-quarantine-'+suffix))
        staged.rename(plugins)
        pointer=instance/'instance.json';pending=pointer.with_suffix('.pending')
        pending.write_text(json.dumps(record['previous']));pending.replace(pointer)
        verify_plugins(instance,previous)
        journal.rename(instance/'private'/('upgrade-recovered-'+suffix+'.json'))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['start','stop','status','upgrade','rollback','recover-upgrade','recover-lifecycle'])
    p.add_argument('--instance',required=True,type=Path)
    p.add_argument('--python',type=Path,default=Path(sys.executable))
    p.add_argument('--release',type=Path)
    p.add_argument('--force',action='store_true')
    p.add_argument('--simulate-failure',action='store_true')
    p.add_argument('--simulate-crash',action='store_true')
    a=p.parse_args();root=a.instance.resolve()
    if a.command=='start':start(root,a.python.resolve())
    elif a.command=='recover-lifecycle':
        with exclusive(root,'.start-lock'),exclusive(root):recover_stale(root)
    elif a.command=='stop':stop(root,a.force)
    elif a.command=='status':print(json.dumps({'running':process(root) is not None,'dirty':(root/'private/DIRTY').exists(),'clean_stop':(root/'private/STOPPED').exists()}))
    elif a.command=='upgrade':activate(root,a.release.resolve(),a.simulate_failure,a.simulate_crash)
    elif a.command=='recover-upgrade':recover_upgrade(root)
    else:activate(root,Path(json.loads((root/'private/previous-release.json').read_text())['release']))
