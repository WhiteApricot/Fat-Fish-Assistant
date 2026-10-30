"""Offline instance management. Private snapshots must never be published."""
import argparse
import ast
from contextlib import closing
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from lifecycle_lock import exclusive

SCHEMA = 1
DATA_SCHEMA = 2
PROJECT = Path(__file__).resolve().parents[1]

def protect_boundary(path):
    (path/'.fat-fish-assistant-private').write_text('Private instance or snapshot; never publish')
    if os.name=='nt':
        shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        environment=dict(os.environ)
        environment['PSMODULEPATH']=str(shell.parent/'Modules')
        result=subprocess.run([str(shell),'-NoProfile','-File',str(PROJECT/'scripts/Protect-Private.ps1'),'-Path',str(path)],
                       env=environment,capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise RuntimeError('Private boundary protection failed: '+result.stderr.strip())
    else:path.chmod(0o700)

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def seal_release(release):
    rows=[{'path':p.relative_to(release).as_posix(),'sha256':digest(p)} for p in release.rglob('*')
          if p.is_file() and '__pycache__' not in p.parts and p.name!='payload-manifest.json']
    (release/'payload-manifest.json').write_text(json.dumps(rows,indent=2))

def verify_release(release):
    for row in json.loads((release/'payload-manifest.json').read_text()):
        p=(release/row['path']).resolve();p.relative_to(release.resolve())
        if digest(p)!=row['sha256']:raise ValueError('Release integrity mismatch; refusing activation')

def verify_plugins(instance,release):
    expected=set()
    for row in json.loads((release/'payload-manifest.json').read_text()):
        if not row['path'].startswith('plugins/'):continue
        relative=Path(row['path']).relative_to('plugins')
        if relative.suffix not in {'.py','.json','.yaml','.yml','.toml','.js'}:continue
        expected.add(relative.as_posix())
        if digest(instance/'data/plugins'/relative)!=row['sha256']:raise ValueError('Instance plugin differs from locked release')
    for path in (instance/'data/plugins').rglob('*.py'):
        if path.relative_to(instance/'data/plugins').as_posix() not in expected:raise ValueError('Unmanaged plugin code; refusing launch')

def materialize(destination,cache=None,plugins=None,offline=False):
    from dependencies import build_release
    cache=cache or Path(os.environ.get('FFA_CACHE',PROJECT.parent/'Fat-Fish-Assistant.local/cache'))
    build_release(destination,cache,plugins,offline)

def initialize(destination, owner, release,seed_persona=True):
    verify_release(release)
    if destination.exists(): raise ValueError('Instance destination exists')
    if not owner.isascii() or not owner.isdecimal(): raise ValueError('Invalid owner')
    destination.mkdir(parents=True)
    for name in ['private','data','logs','persona']:(destination/name).mkdir()
    protect_boundary(destination)
    (destination/'private/dashboard-password.txt').write_text(secrets.token_urlsafe(24))
    import imageio_ffmpeg
    (destination/'bin').mkdir()
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(),destination/'bin/ffmpeg.exe')
    cfg = {'owner':owner,'admins':[],'blocked':[],'approved_groups':[],'groups':{},
           'group_admins':{},'group_blocked':{},'identity_salt':secrets.token_hex(32),'epoch':0}
    (destination/'private/p2c-access.json').write_text(json.dumps(cfg))
    (destination/'private/instance-identity.json').write_text(json.dumps({'owner':owner}))
    (destination/'data/cmd_config.json').write_text(json.dumps({'dashboard':{'host':'127.0.0.1','port':6185},'platform':[],'provider':[],'admins_id':[owner],
        'provider_settings':{'persona_pool':['feiyu-v1']},'agent_runner':{'config':{'persona':{'persona_id':'feiyu-v1'}}}}))
    (destination/'instance.json').write_text(json.dumps({'schema':SCHEMA,'project':'Fat-Fish-Assistant','deployment_id':secrets.token_hex(16),'release':str(release.resolve())}))
    shutil.copytree(release/'plugins', destination/'data/plugins')
    shutil.copytree(PROJECT/'examples/config/plugins',destination/'data/config')
    for name in ['Persona_Master.md','Persona_Examples.json']:
        shutil.copy2(PROJECT/'examples/persona'/name,destination/'persona'/name)
    if seed_persona:
        subprocess.run([sys.executable,str(PROJECT/'scripts/seed_persona.py'),'--instance',str(destination),'--release',str(release)],check=True)

def backup(instance, destination):
    with exclusive(instance):
        _backup_locked(instance,destination)

def _backup_locked(instance, destination):
    if any((instance/'private'/name).exists() for name in ['RUNNING','DIRTY','upgrade-journal.json']): raise ValueError('Stop native writers and resolve interrupted maintenance before backup')
    if destination.exists(): raise ValueError('Snapshot destination exists')
    destination.mkdir(parents=True)
    protect_boundary(destination)
    rows=[]
    for base in ['private','data','persona']:
        for path in (instance/base).rglob('*'):
            rel=path.relative_to(instance)
            if not path.is_file():continue
            parts=tuple(part.casefold() for part in rel.parts)
            if parts[:2] in {('data','plugins'),('data','temp')}:continue
            if parts[:4]==('data','plugin_data','meme_manager','temp'):continue
            if path.name.endswith(('-wal','-shm')):continue
            if len(parts)==2 and parts[0]=='private' and path.name.casefold() in {'.instance-lock','running','stop','stopped','dirty','previous-release.json','embedding.pid','speech.pid'}:continue
            path.resolve().relative_to(instance.resolve())
            target=destination/'payload'/rel
            target.parent.mkdir(parents=True,exist_ok=True)
            if path.suffix=='.db':
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(target)) as copy:
                    original.backup(copy)
                    if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Invalid database')
            else:shutil.copy2(path,target)
            rows.append({'path':rel.as_posix(),'sha256':digest(target)})
    release=Path(json.loads((instance/'instance.json').read_text())['release'])
    software={item['name']:item['commit'] for item in json.loads((release/'release.json').read_text())['vendor']}
    (destination/'manifest.json').write_text(json.dumps({'schema':DATA_SCHEMA,'software':software,'files':rows,'consistency':'cold instance; all managed writers stopped','private':True},indent=2))

def restore(snapshot,destination,release):
    verify_release(release)
    if destination.exists():raise ValueError('Refusing to overwrite existing instance or newer chats')
    manifest=json.loads((snapshot/'manifest.json').read_text())
    if manifest['schema']!=DATA_SCHEMA:raise ValueError('Unsupported data schema; no writes performed')
    software={item['name']:item['commit'] for item in json.loads((release/'release.json').read_text())['vendor']}
    if manifest.get('software')!=software:raise ValueError('Software/data compatibility mismatch; explicit migration adapter required')
    for row in manifest['files']:
        path=(snapshot/'payload'/row['path']).resolve()
        path.relative_to((snapshot/'payload').resolve())
        if digest(path)!=row['sha256']:raise ValueError('Snapshot hash mismatch; no writes performed')
        if row['path']=='data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db':
            tree=ast.parse((release/'plugins/astrbot_plugin_livingmemory/storage/db_migration.py').read_text(encoding='utf-8'))
            declaration=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='DBMigration')
            expected=next(ast.literal_eval(node.value) for node in declaration.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CURRENT_VERSION' for t in node.targets))
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                actual=db.execute('SELECT MAX(version) FROM db_version').fetchone()[0]
                if actual!=expected:raise ValueError('Unsupported native memory data version; no writes performed')
    stage=destination.with_name(destination.name+'.staging-'+secrets.token_hex(4))
    stage.mkdir(parents=True)
    protect_boundary(stage)
    for row in manifest['files']:
        target=stage/row['path'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(snapshot/'payload'/row['path'],target)
        if target.suffix=='.db':
            with closing(sqlite3.connect(target)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restore database check failed; staging quarantined')
    shutil.copytree(release/'plugins',stage/'data/plugins')
    import imageio_ffmpeg
    (stage/'bin').mkdir()
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(),stage/'bin/ffmpeg.exe')
    (stage/'logs').mkdir(exist_ok=True)
    (stage/'instance.json').write_text(json.dumps({'schema':SCHEMA,'release':str(release.resolve())}))
    stage.rename(destination)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['materialize','init','backup','restore'])
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--instance',type=Path)
    p.add_argument('--snapshot',type=Path)
    p.add_argument('--release',type=Path)
    p.add_argument('--owner')
    p.add_argument('--cache',type=Path)
    p.add_argument('--offline',action='store_true')
    a=p.parse_args()
    if a.command=='materialize':materialize(a.destination.resolve(),a.cache,offline=a.offline)
    elif a.command=='init':initialize(a.destination.resolve(),a.owner,a.release)
    elif a.command=='backup':backup(a.instance.resolve(),a.destination.resolve())
    else:restore(a.snapshot.resolve(),a.destination.resolve(),a.release)
    print('Completed:',a.command)
