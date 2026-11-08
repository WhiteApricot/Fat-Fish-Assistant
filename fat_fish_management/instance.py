"""Offline instance management. Private snapshots must never be published."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
from fat_fish_management.lifecycle_lock import exclusive
from fat_fish_management.dependencies import link_or_copy
from fat_fish_management.data_integrity import historical_memory

SCHEMA = 1
DATA_SCHEMA = 2
PROJECT = Path(__file__).resolve().parents[1]
PLUGIN_MUTABLE_CONFIGS={'meme_manager/image_host/config.json'}

def protect_boundary(path):
    (path/'.fat-fish-assistant-private').write_text('Private instance or snapshot; never publish')
    if os.name=='nt':
        shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        environment=dict(os.environ)
        environment['PSMODULEPATH']=str(shell.parent/'Modules')
        result=subprocess.run([str(shell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT/'scripts/process/Protect-Private.ps1'),'-Path',str(path)],
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
        if relative.as_posix() in PLUGIN_MUTABLE_CONFIGS:continue
        if digest(instance/'data/plugins'/relative)!=row['sha256']:raise ValueError('Instance plugin differs from locked release')
    for path in (instance/'data/plugins').rglob('*.py'):
        if path.relative_to(instance/'data/plugins').as_posix() not in expected:raise ValueError('Unmanaged plugin code; refusing launch')

def copy_plugin_resources(source,destination,release):
    """Retain user resources when locked plugin programs are replaced or recovered."""
    locked={row['path'][len('plugins/'):]:row['sha256'] for row in json.loads((release/'payload-manifest.json').read_text()) if row['path'].startswith('plugins/')}
    for name,sha in locked.items():
        if name not in PLUGIN_MUTABLE_CONFIGS and digest(source/name)!=sha and plugin_program(Path(name)):raise ValueError('Previous plugin code differs from its locked release')
    for path in source.rglob('*'):
        if path.is_symlink() or path.is_junction():raise ValueError('Linked plugin resources need an explicit adapter')
        if not path.is_file():continue
        relative=path.relative_to(source)
        if '__pycache__' in relative.parts or path.suffix=='.pyc':continue
        if relative.as_posix() in locked and digest(path)==locked[relative.as_posix()]:continue
        if path.suffix.lower() in {'.py','.js','.mjs','.exe','.dll','.pyd','.so'} or path.name.casefold() in {'requirements.txt','pyproject.toml','package.json','package-lock.json','uv.lock'}:raise ValueError('Unmanaged plugin code requires compatibility review')
        output=destination/relative
        if output.exists():
            if relative.as_posix() not in PLUGIN_MUTABLE_CONFIGS and plugin_program(relative):raise ValueError('User plugin resource conflicts with the new program release')
        output.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,output)

def plugin_program(relative):
    return relative.suffix.lower() in {'.py','.json','.yaml','.yml','.toml','.js','.mjs','.exe','.dll','.pyd','.so'} or relative.name.casefold() in {'requirements.txt','requirements.lock','uv.lock','package-lock.json'}

def materialize(destination,cache=None,plugins=None,offline=False):
    from fat_fish_management.dependencies import build_release
    cache=cache or Path(os.environ.get('FFA_CACHE',PROJECT.parent/'Fat-Fish-Assistant.local/cache'))
    build_release(destination,cache,plugins,offline)

def initialize(destination, owner, release,seed_persona=True):
    verify_release(release)
    if destination.exists(): raise ValueError('Instance destination exists')
    if not owner.isascii() or not owner.isdecimal(): raise ValueError('Invalid owner')
    destination.mkdir(parents=True)
    for name in ['private','data','logs','persona']:(destination/name).mkdir()
    protect_boundary(destination)
    import imageio_ffmpeg
    (destination/'bin').mkdir()
    link_or_copy(imageio_ffmpeg.get_ffmpeg_exe(),destination/'bin'/('ffmpeg.exe' if os.name=='nt' else 'ffmpeg'))
    cfg = {'owner':owner,'global_admins':[],'admins':[],'blocked':[],'approved_groups':[],'groups':{},
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
        subprocess.run([sys.executable,'-X','utf8',str(PROJECT/'fat_fish_management/seed_persona.py'),'--instance',str(destination),'--release',str(release)],check=True)
    subprocess.run([sys.executable,'-X','utf8','-B',str(PROJECT/'fat_fish_management/configure_experience.py'),'--instance',str(destination),'--release',str(release)],check=True)

def backup(instance, destination):
    with exclusive(instance):
        _backup_locked(instance,destination)

def _backup_locked(instance, destination):
    if any((instance/'private'/name).exists() for name in ['RUNNING','DIRTY','upgrade-journal.json']): raise ValueError('Stop native writers and resolve interrupted maintenance before backup')
    if destination.exists(): raise ValueError('Snapshot destination exists')
    release=Path(json.loads((instance/'instance.json').read_text())['release'])
    verify_release(release);verify_plugins(instance,release)
    from fat_fish_management.data_integrity import validate
    integrity=validate(instance,release)
    locked_plugins={row['path'][len('plugins/'):]:row['sha256'] for row in json.loads((release/'payload-manifest.json').read_text()) if row['path'].startswith('plugins/')}
    fixed_assets={row['path']:row['sha256'] for row in json.loads((PROJECT/'manifests/model-runtime-lock.json').read_text())}
    environment={'private','data','persona','logs','bin','engines','models','instance.json','.fat-fish-assistant-private','.feiyu-private-boundary'}
    custom=[p.name for p in instance.iterdir() if p.name not in environment and not p.name.startswith(('.plugin-','.staging-'))]
    destination.mkdir(parents=True)
    protect_boundary(destination)
    rows=[]
    for base in ['private','data','persona','models','engines','bin',*custom]:
        origin=instance/base
        for path in ([origin] if origin.is_file() else origin.rglob('*')):
            rel=path.relative_to(instance)
            if path.is_symlink() or path.is_junction():raise ValueError('Linked data requires an explicit migration adapter')
            if not path.is_file():continue
            parts=tuple(part.casefold() for part in rel.parts)
            if path.is_symlink() or path.is_junction():raise ValueError('Linked data requires an explicit migration adapter')
            if '__pycache__' in parts or path.suffix=='.pyc':continue
            if parts==('bin','ffmpeg.exe' if os.name=='nt' else 'ffmpeg'):
                import imageio_ffmpeg
                if digest(path)!=digest(Path(imageio_ffmpeg.get_ffmpeg_exe())):raise ValueError('Managed FFmpeg was modified; explicit compatibility review required')
                continue
            if parts[0] in {'engines','models'} and rel.as_posix() in fixed_assets:
                if digest(path)!=fixed_assets[rel.as_posix()]:raise ValueError('Installed dependency asset differs from pinned version')
                continue
            if parts[:2] in {('private','config-history'),('private','index-repair-history'),('private','lifecycle-history')}:continue
            if parts[0]=='private' and path.suffix=='.log':continue
            if parts==('private','install-origin.json'):continue
            if parts==('private','runtime-health.json'):continue
            if parts[0]=='private' and path.name.casefold() in {'embedding.process.json','speech.process.json','embedding.process.json.pending','speech.process.json.pending'}:continue
            if parts[:2]==('data','plugins'):
                plugin_relative=Path(*rel.parts[2:]).as_posix()
                if plugin_relative in locked_plugins and plugin_relative not in PLUGIN_MUTABLE_CONFIGS:
                    if digest(path)==locked_plugins[plugin_relative]:continue
                    if plugin_program(Path(plugin_relative)):raise ValueError('Modified locked plugin program cannot be migrated')
                if path.suffix.lower() in {'.py','.js','.mjs','.exe','.dll','.pyd','.so'}:raise ValueError('Unmanaged plugin code cannot be migrated')
            if parts[:2]==('data','temp'):continue
            if parts[:4]==('data','plugin_data','meme_manager','temp'):continue
            if path.name.endswith(('-wal','-shm')):continue
            if len(parts)==2 and parts[0]=='private' and path.name.casefold() in {'.instance-lock','.start-lock','running','stop','stopped','dirty','previous-release.json','embedding.pid','speech.pid','migrated-out.json'}:continue
            path.resolve().relative_to(instance.resolve())
            target=destination/'payload'/rel
            target.parent.mkdir(parents=True,exist_ok=True)
            if path.suffix.lower() in {'.db','.sqlite','.sqlite3'} and not historical_memory(path,instance):
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(target)) as copy:
                    original.backup(copy)
                    if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Invalid database')
            else:shutil.copy2(path,target)
            rows.append({'path':rel.as_posix(),'sha256':digest(target)})
    software={item['name']:item['commit'] for item in json.loads((release/'release.json').read_text())['vendor']}
    identity=json.loads((instance/'instance.json').read_text());identity.pop('release',None)
    (destination/'manifest.json').write_text(json.dumps({'schema':DATA_SCHEMA,'software':software,'instance_identity':identity,'files':rows,'integrity':integrity,'consistency':'cold instance; all managed writers stopped','private':True},indent=2))

def restore(snapshot,destination,release,installation_root=None):
    verify_release(release)
    if destination.exists():raise ValueError('Refusing to overwrite existing instance or newer chats')
    manifest=json.loads((snapshot/'manifest.json').read_text())
    if manifest['schema']!=DATA_SCHEMA:raise ValueError('Unsupported data schema; no writes performed')
    software={item['name']:item['commit'] for item in json.loads((release/'release.json').read_text())['vendor']}
    if manifest.get('software')!=software:raise ValueError('Software/data compatibility mismatch; explicit migration adapter required')
    from fat_fish_management.migration_package import portable_name
    seen=set()
    for row in manifest['files']:
        name=portable_name(row['path'])
        if name.casefold() in seen:raise ValueError('Duplicate snapshot path')
        seen.add(name.casefold())
        if name.casefold() in {'instance.json','private/running','private/dirty','private/stop','private/stopped','private/.instance-lock','private/.start-lock','private/migrated-out.json','private/upgrade-journal.json'}:
            raise ValueError('Snapshot contains forbidden runtime metadata')
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
    shutil.copytree(release/'plugins',stage/'data/plugins')
    for row in manifest['files']:
        target=stage/row['path'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(snapshot/'payload'/row['path'],target)
        if target.suffix.lower() in {'.db','.sqlite','.sqlite3'} and not historical_memory(target,stage):
            with closing(sqlite3.connect(target)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restore database check failed; staging quarantined')
    import imageio_ffmpeg
    (stage/'bin').mkdir(exist_ok=True)
    link_or_copy(imageio_ffmpeg.get_ffmpeg_exe(),stage/'bin'/('ffmpeg.exe' if os.name=='nt' else 'ffmpeg'))
    (stage/'logs').mkdir(exist_ok=True)
    identity=manifest.get('instance_identity',{'schema':SCHEMA})
    if identity.get('schema')!=SCHEMA:raise ValueError('Unsupported instance identity schema')
    (stage/'instance.json').write_text(json.dumps({**identity,'release':str(release.resolve())}))
    if installation_root is not None:
        from fat_fish_management.controller import prepare_instance_assets,read
        prepare_instance_assets(installation_root,read(installation_root/'installation.json'),stage)
        from fat_fish_management.migration import relocate_configuration
        relocate_configuration(stage,destination,installation_root,manifest.get('relocation',{}))
    from fat_fish_management.data_integrity import validate
    validate(stage,release)
    owner=json.loads((stage/'private/instance-identity.json').read_text())['owner']
    if json.loads((stage/'private/p2c-access.json').read_text())['owner']!=owner:raise ValueError('Owner/access identity mismatch')
    verify_plugins(stage,release)
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
    p.add_argument('--installation-root',type=Path)
    a=p.parse_args()
    if a.command=='materialize':materialize(a.destination.resolve(),a.cache,offline=a.offline)
    elif a.command=='init':initialize(a.destination.resolve(),a.owner,a.release)
    elif a.command=='backup':backup(a.instance.resolve(),a.destination.resolve())
    else:restore(a.snapshot.resolve(),a.destination.resolve(),a.release,a.installation_root)
    print('Completed:',a.command)
