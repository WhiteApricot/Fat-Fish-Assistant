"""Portable migration orchestration over existing install, stop, backup and restore APIs."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import closing
import json
from pathlib import Path
import secrets
import tempfile
import time
import socket
import urllib.parse

from fat_fish_management import controller as c
from fat_fish_management.instance import digest,protect_boundary
from fat_fish_management.lifecycle_lock import exclusive
from fat_fish_management.migration_package import pack,unpack

LOCKS=['sources.json','dashboard.json','chat.json','speech.json','assets.json','python-runtime.json','model-runtime-lock.json','napcat-runtime-lock.json','napcat-node-lock.json']

def check_configuration_boundary(instance):
    """Do not silently claim to migrate resources held outside managed user storage."""
    from pathlib import PureWindowsPath
    def strings(value):
        if isinstance(value,str):yield value
        elif isinstance(value,dict):
            for child in value.values():yield from strings(child)
        elif isinstance(value,list):
            for child in value:yield from strings(child)
    for path in [instance/'data/cmd_config.json',instance/'data/plugins/meme_manager/image_host/config.json',*list((instance/'data/config').rglob('*.json'))]:
        if not path.exists():continue
        for value in strings(c.read(path)):
            if not PureWindowsPath(value).is_absolute():continue
            candidate=Path(value).resolve()
            try:candidate.relative_to(instance.resolve())
            except ValueError:raise ValueError('Configuration references an external resource; migration cannot guarantee completeness: '+path.relative_to(instance).as_posix())

def relocate_configuration(stage,destination,root,source):
    mappings=[(source.get('instance'),str(destination)),(source.get('root'),str(root))]
    def rebase(value):
        if isinstance(value,dict):return {k:rebase(v) for k,v in value.items()}
        if isinstance(value,list):return [rebase(v) for v in value]
        if isinstance(value,str):
            for before,after in mappings:
                if not before:continue
                normalized=value.replace('\\','/');prefix=before.replace('\\','/').rstrip('/')
                if normalized.casefold()==prefix.casefold() or normalized.casefold().startswith(prefix.casefold()+'/'):
                    return after+normalized[len(prefix):].replace('/',__import__('os').sep)
        return value
    paths=[stage/'data/cmd_config.json',*list((stage/'data/config').rglob('*.json'))]
    for path in paths:
        if path.exists():c.atomic(path,rebase(c.read(path)))
    cfgpath=stage/'data/cmd_config.json';cfg=c.read(cfgpath);used=set()
    def port(preferred):
        with socket.socket() as probe:
            try:
                if preferred in used:raise OSError('Duplicate local port')
                probe.bind(('127.0.0.1',preferred))
            except OSError:probe.bind(('127.0.0.1',0))
            selected=probe.getsockname()[1];used.add(selected);return selected
    cfg['dashboard']['port']=port(cfg['dashboard']['port'])
    for platform in cfg.get('platform',[]):
        if platform.get('id')=='feiyu-onebot':platform['ws_reverse_port']=port(platform['ws_reverse_port'])
    for provider in cfg.get('provider',[]):
        field='embedding_api_base' if provider.get('id')=='feiyu-local-embedding' else 'api_base' if provider.get('id')=='feiyu-local-stt' else None
        if not field:continue
        endpoint=urllib.parse.urlsplit(provider[field])
        if endpoint.scheme!='http' or endpoint.hostname not in {'127.0.0.1','localhost'}:raise ValueError('Restored local provider has a non-loopback endpoint')
        provider[field]=urllib.parse.urlunsplit(('http','127.0.0.1:'+str(port(endpoint.port or 80)),endpoint.path,'',''))
    c.atomic(cfgpath,cfg)

def validate_snapshot(snapshot):
    manifest=c.read(snapshot/'manifest.json')
    if manifest.get('schema')!=2:raise ValueError('Unsupported data schema')
    seen=set()
    from fat_fish_management.migration_package import portable_name
    for row in manifest['files']:
        name=portable_name(row['path'])
        if name.casefold() in seen:raise ValueError('Duplicate snapshot path')
        seen.add(name.casefold())
        path=snapshot/'payload'/name
        path.resolve().relative_to((snapshot/'payload').resolve())
        if digest(path)!=row['sha256']:raise ValueError('Snapshot SHA256 mismatch')
    actual={p.relative_to(snapshot/'payload').as_posix().casefold() for p in (snapshot/'payload').rglob('*') if p.is_file()}
    if actual!=seen:raise ValueError('Snapshot file inventory mismatch')
    plan=c.read(snapshot/'installation-plan.json')
    if plan.get('schema')!=1:raise ValueError('Unsupported installation plan')
    for name in LOCKS:
        accepted={digest(c.PROJECT/'manifests'/name)}
        if name=='sources.json':
            accepted.add('6bc2f780fc30a1c8d8c6d5d221aa19d920f28fef6bce514e03257b92ed797f44')
            accepted.add('edb17b62829418773d2fe2f97f5769710717c55128725862c390342b5d173fc6')
            accepted.add('668fee833ed350cc0d6612c58a8892e759bc23abbca462f2a9289e0a84e35dea')
        if plan['locks'].get(name) not in accepted:raise ValueError('Pinned dependency compatibility mismatch: '+name)
    for name,value in plan.get('profile_locks',{}).items():
        if name not in {'core.json'} or value!=digest(c.PROJECT/'manifests'/name):raise ValueError('Dependency profile lock mismatch')
    if plan.get('dependency_profile','chat') not in {'core','chat'}:raise ValueError('Unknown migration dependency profile')
    if plan.get('dependency_profile')=='core' and 'core.json' not in plan.get('profile_locks',{}):raise ValueError('Missing core dependency lock')
    if set(plan['components'])-{'python','astrbot','plugins','embedding','speech','napcat'}:raise ValueError('Unsupported components in migration')
    return manifest,plan

def export(root,record,destination,password):
    if destination.exists():raise ValueError('Migration output exists; choose a new file')
    target=c.instance(root,record)
    with c.installation_lock(root):
        release=Path(c.read(target/'instance.json')['release'])
        metadata=c.read(release/'release.json')
        expected={item['name']:item for item in c.load('sources.json')}
        if any(not c.source_descriptor_compatible(item,expected.get(item['name'])) for item in metadata['vendor']):
            raise ValueError('Installed upstream versions differ from this engineering package; export with the matching package and manifests')
        for profile in ['chat','speech']:
            if not record.get(profile+'_python'):continue
            receipt=c.resolve(root,record[profile+'_python']).parents[1]/'ffa-environment.json'
            if not receipt.exists() or c.read(receipt).get('lock_sha256')!=c.profile_fingerprint(c.environment_profile(root,record,profile)):
                raise ValueError('Installed environment lock differs from the current dependency manifest: '+profile)
        # Stop all managed services through the existing controller first.
        c.operate(root,record,'stop')
        c.invoke(root,c.chat_python(root,record),'manage.py','recover-lifecycle','--instance',target)
        with exclusive(target,'.start-lock'):
            check_configuration_boundary(target)
            for name in ['astrbot','napcat','embedding','speech']:
                if record.get('components',{}).get(name,{}).get('installed') and c.component_status(root,record,name)['running']:
                    raise ValueError('Writer did not stop: '+name)
            with tempfile.TemporaryDirectory(prefix='migration-export-',dir=root) as tmp:
                work=Path(tmp);protect_boundary(work);snapshot=work/'snapshot'
                c.invoke(root,c.chat_python(root,record),'instance.py','backup','--instance',target,'--destination',snapshot)
                release=Path(c.read(target/'instance.json')['release'])
                metadata=c.read(release/'release.json')
                plugins=[x['name'] for x in metadata['vendor'] if x['name'] in {'astrbot_plugin_livingmemory','astrbot_plugin_continuous_message','meme_manager'}]
                components=[name for name in ['python','astrbot','plugins','napcat','embedding','speech'] if record['components'].get(name,{}).get('installed')]
                dependencies={'python':c.load('python-runtime.json'),'sources':metadata['vendor'],'chat':c.load(record.get('dependency_profile','chat')+'.json'),'dashboard':c.load('dashboard.json'),'assets':c.load('assets.json')}
                if record.get('speech_python'):dependencies['speech']=c.load('speech.json')
                states={name:{key:record['components'][name].get(key) for key in ['installed','version','ownership']} for name in components}
                c.atomic(snapshot/'installation-plan.json',{'schema':1,'components':components,'component_state':states,'dependencies':dependencies,'plugins':plugins,'locks':{name:digest(c.PROJECT/'manifests'/name) for name in LOCKS},'source_suspended':True,'dependency_profile':record.get('dependency_profile','chat'),'profile_locks':{name:digest(c.PROJECT/'manifests'/name) for name in ['core.json']}})
                manifest,_=validate_snapshot(snapshot)
                manifest['relocation']={'root':str(root),'instance':str(target)}
                c.atomic(snapshot/'manifest.json',manifest)
                c.atomic(target/'private/MIGRATED-OUT.json',{'time':time.time(),'reason':'Migration export in progress; source suspended for safe host transfer'})
                pack(snapshot,destination,password)
                result={'path':str(destination),'bytes':destination.stat().st_size,'sha256':digest(destination),'files':len(manifest['files']),'encrypted':True,'source_stopped':True}
                c.atomic(target/'private/MIGRATED-OUT.json',{'time':time.time(),'sha256':result['sha256'],'reason':'Source suspended; activate only one host at a time'})
                return result

def import_package(root,package,password,confirm_source_offline=False,cache=None,offline=False):
    if not confirm_source_offline:raise ValueError('Confirm source remains stopped before importing')
    if c.state(root).get('instance') or (root/'instance').exists():raise ValueError('Target already has an instance; choose a new empty installation root')
    with tempfile.TemporaryDirectory(prefix='migration-import-',dir=root) as tmp:
        work=Path(tmp);protect_boundary(work);snapshot=work/'snapshot'
        unpack(package,snapshot,password)
        manifest,plan=validate_snapshot(snapshot)
        # Reuse the single pinned installer, including model and plugin dependencies.
        base=c.resolve(root,c.state(root)['components']['python']['path'])
        args=c.parser().parse_args(['--root',str(root),'install','--no-instance','--python',str(base),'--components',','.join(plan['components']),'--plugins',','.join(plan['plugins']),'--accept-external-licenses'])
        args.dependency_profile=plan.get('dependency_profile','chat')
        args.cache=cache;args.offline=offline
        c.install(root,args)
        with c.installation_lock(root):
            record=c.state(root)
            if record.get('instance') or (root/'instance').exists():raise ValueError('Target changed during import; no overwrite')
            release=c.resolve(root,record['release'])
            software={item['name']:item['commit'] for item in c.read(release/'release.json')['vendor']}
            if manifest.get('software')!=software:raise ValueError('软件/数据版本不兼容：迁移已拒绝，没有创建目标实例。')
            destination=root/'instance'
            c.invoke(root,c.chat_python(root,record),'instance.py','restore','--snapshot',snapshot,'--destination',destination,'--release',release,'--installation-root',root)
            record['instance']='instance'
            for name in ['embedding','speech']:
                if record['components'].get(name,{}).get('installed'):record['components'][name]['path']='instance'
            # Do not carry a machine-specific QQ path or any QQ login state.
            record.pop('qq_executable',None)
            c.atomic(root/'installation.json',record)
        # Existing installer rehydrates engines/models using locked hashes; it never
        # replaces restored keys, permissions, chats or provider settings.
        c.invoke(root,c.chat_python(root,c.state(root)),'doctor.py','--instance',root/'instance',json_output=True)
        return {'restored':True,'files':len(manifest['files']),'sha256':digest(package),'source_must_remain_stopped':True,'qq_login_required':True,'live_service_acceptance':'pending manual login and health check'}
