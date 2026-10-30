"""Manage a local, versioned AstrBot instance. Never manages unrelated processes."""
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
from instance import verify_release,verify_plugins
from lifecycle_lock import exclusive

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
    if (instance/'private/upgrade-journal.json').exists():raise ValueError('Interrupted upgrade; recover-upgrade before launch')
    release=Path(json.loads((instance/'instance.json').read_text())['release'])
    verify_release(release)
    verify_plugins(instance,release)
    if process(instance):raise ValueError('Already running')
    if any((instance/'private'/x).exists() for x in ['DIRTY','RUNNING']):raise ValueError('Resolve dirty or stale lifecycle marker before launch')
    cfg=json.loads((instance/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
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
    with (instance/'logs/chat.stdout.log').open('ab') as stdout,(instance/'logs/chat.stderr.log').open('ab') as stderr:
        proc=subprocess.Popen([str(python),'-u',str(Path(__file__).with_name('run_chat.py')),'--instance',str(instance)],cwd=instance,stdout=stdout,stderr=stderr,creationflags=flags,env=environment)
    # Locked native dependencies can take over a minute on a cold Windows cache.
    for _ in range(360):
        if proc.poll() is not None:raise RuntimeError('Chat failed startup; inspect private logs')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.2):return
        except OSError:time.sleep(.5)
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
    journal.write_text(json.dumps({'previous':json.loads(before),'requested':str(release),'transaction':suffix}))
    stage=instance/('.plugin-stage-'+suffix)
    previous_plugins=instance/('.plugin-rollback-'+suffix)
    current_plugins=instance/'data/plugins'
    shutil.copytree(release/'plugins',stage)
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
        required=['astrbot/astrbot/core/feiyu_p2c.py','src/p2c/instance_identity.py']
        if any(not (release/name).is_file() for name in required):raise ValueError('Incomplete release')
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
        if plugins.exists():plugins.rename(instance/('.plugin-quarantine-'+suffix))
        shutil.copytree(previous/'plugins',plugins)
        pointer=instance/'instance.json';pending=pointer.with_suffix('.pending')
        pending.write_text(json.dumps(record['previous']));pending.replace(pointer)
        verify_plugins(instance,previous)
        journal.rename(instance/'private'/('upgrade-recovered-'+suffix+'.json'))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['start','stop','status','upgrade','rollback','recover-upgrade'])
    p.add_argument('--instance',required=True,type=Path)
    p.add_argument('--python',type=Path,default=Path(sys.executable))
    p.add_argument('--release',type=Path)
    p.add_argument('--force',action='store_true')
    p.add_argument('--simulate-failure',action='store_true')
    p.add_argument('--simulate-crash',action='store_true')
    a=p.parse_args();root=a.instance.resolve()
    if a.command=='start':start(root,a.python.resolve())
    elif a.command=='stop':stop(root,a.force)
    elif a.command=='status':print(json.dumps({'running':process(root) is not None,'dirty':(root/'private/DIRTY').exists(),'clean_stop':(root/'private/STOPPED').exists()}))
    elif a.command=='upgrade':activate(root,a.release.resolve(),a.simulate_failure,a.simulate_crash)
    elif a.command=='recover-upgrade':recover_upgrade(root)
    else:activate(root,Path(json.loads((root/'private/previous-release.json').read_text())['release']))
