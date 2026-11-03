"""Version 1 local management contract. No GUI, HTTP listener or QQ sender."""
import argparse
import contextlib
import getpass
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid
import tempfile
from pathlib import Path

from dependencies import PROJECT,acquire,build_release,clean_environment,digest,external,install_environment,load,profile_fingerprint,run_logged,unpack,verify_environment_versions,link_or_copy,RELEASE_LAYOUT,extend_core_environment

SCHEMA=1
COMPONENTS=load('components.json')['components']
EXIT={'ok':0,'invalid':20,'needs_input':21,'missing':22,'integrity':23,'busy':24,'incompatible':25,'read_only':26,'failed':30}

class Fault(Exception):
    def __init__(self,kind,message,hint=''):super().__init__(message);self.kind=kind;self.hint=hint

class Parser(argparse.ArgumentParser):
    def error(self,message):raise Fault('invalid',message,'Run the command with --help for accepted options.')

# The tested P2F-R1 transition changes only the project's AstrBot tool guard.
P2F_R1_PATCH_TRANSITION=(
    '48ab060e53723c3b5f320c6085073d87ac432510a014ba356700db6e1dab313d',
    'dfc27563a18dc90360d91ba7513694a3f3a5fad5eb5c4abd77716a9be547ac38',
)

def repair_release_compatible(before,after):
    if before.get('schema')!=after.get('schema'):return False
    if before.get('vendor')==after.get('vendor'):return True
    for key in ('vendor','external_sources'):
        rows=before.get(key)
        if not isinstance(rows,list):return False
        astrbot=[row for row in rows if row['name']=='astrbot']
        if len(astrbot)!=1 or astrbot[0].get('patch_sha256')!=P2F_R1_PATCH_TRANSITION[0]:return False
        expected=[dict(row,patch_sha256=P2F_R1_PATCH_TRANSITION[1]) if row['name']=='astrbot' else row for row in rows]
        if expected!=after.get(key):return False
    return before.get('dashboard')==after.get('dashboard')

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def read_stdin():return json.loads(sys.stdin.read().removeprefix('\ufeff'))
def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.pending-'+secrets.token_hex(4))
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temp.replace(path)

def progress(step,item):
    labels={'download':'下载','cache-hit':'复用已校验缓存','build-from-pinned-source':'构建固定版本','install-locked-packages':'安装锁定依赖'}
    details={'preflight and pinned component plan':'检查环境和固定版本计划','independent Python environments':'准备独立 Python 环境','verified upstream sources and compatibility patches':'校验外部源码及既有兼容补丁','optional external assets':'准备已选外部组件','independent instance initialization':'初始化独立实例'}
    print(f'[Fat-Fish-Assistant] {labels.get(step,step)}: {details.get(item,item)}',file=sys.stderr,flush=True)

def available_loopback_port(preferred):
    with socket.socket() as listener:
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        try:listener.bind(('127.0.0.1',preferred))
        except OSError:listener.bind(('127.0.0.1',0))
        return listener.getsockname()[1]

def default_root():return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'Fat-Fish-Assistant'

def owned_root(root,create=False):
    root=external(root)
    marker=root/'.ffa-managed.json'
    if not marker.exists():
        if not create:raise Fault('read_only','Root is not a managed Fat-Fish-Assistant installation','Use install with a new empty root; external components remain read-only.')
        if root.exists() and any(root.iterdir()):raise Fault('read_only','Refusing to adopt a nonempty unmanaged directory')
        root.mkdir(parents=True,exist_ok=True)
        atomic(marker,{'schema_version':SCHEMA,'project':'Fat-Fish-Assistant','deployment_id':uuid.uuid4().hex})
    identity=read(marker)
    if identity.get('schema_version')!=SCHEMA or identity.get('project')!='Fat-Fish-Assistant':raise Fault('incompatible','Unknown installation marker')
    return root

def state(root):
    value=read(root/'installation.json') if (root/'installation.json').exists() else {'schema_version':SCHEMA,'project':'Fat-Fish-Assistant','components':{}}
    if value.get('schema_version')!=SCHEMA:raise Fault('incompatible','Unknown management state version')
    return value

def resolve(root,value):
    path=Path(value)
    if path.is_absolute():return path
    target=(root/path).resolve();target.relative_to(root.resolve());return target

def rel(root,path):
    try:return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:return str(Path(path).resolve())

def chat_python(root,record):
    value=record.get('chat_python')
    if not value:raise Fault('missing','Chat Python environment is not installed')
    return resolve(root,value)

def instance(root,record):
    value=record.get('instance')
    if not value:raise Fault('needs_input','No initialized instance','Install with --owner, or restore into a new empty instance.')
    path=resolve(root,value)
    if root.resolve() not in path.resolve().parents:raise Fault('read_only','Instance is outside this managed root')
    return path

def invoke(root,python,script,*args,json_output=False,input_data=None):
    logfile=root/'logs'/(script.replace('.py','')+'-management.log');logfile.parent.mkdir(exist_ok=True)
    command=[str(python),'-X','utf8','-B',str(PROJECT/'scripts'/script),*map(str,args)]
    if script in {'migration_cli.py','manage.py','instance.py'}:
        with tempfile.TemporaryFile(dir=logfile.parent) as output,logfile.open('a',encoding='utf-8') as log:
            child=subprocess.Popen(command,stdout=output,stderr=subprocess.PIPE,stdin=subprocess.PIPE,text=True,encoding='utf-8',env=clean_environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            child.stdin.write(input_data or '');child.stdin.close()
            for line in child.stderr:
                log.write(line);log.flush()
                if line.startswith('[Fat-Fish-Assistant]'):print(line.rstrip(),file=sys.stderr,flush=True)
            child.wait();output.seek(0);stdout=output.read().decode('utf-8',errors='replace');log.write(stdout)
            proc=subprocess.CompletedProcess(command,child.returncode,stdout,'')
    else:
        proc=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',env=clean_environment(),input=input_data,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        with logfile.open('a',encoding='utf-8') as log:log.write(proc.stdout+'\n'+proc.stderr)
    if proc.returncode:
        if script=='migration_cli.py':
            try:
                error=json.loads(proc.stdout)
                if 'user_error' in error:raise Fault(error.get('kind','integrity'),error['user_error'],f'Diagnostics: {logfile}')
            except (json.JSONDecodeError,TypeError):pass
        if script=='manage.py':
            details=logfile.read_text(encoding='utf-8')[-12000:]
            reasons={
                'PID identity mismatch':'运行标记中的进程号已被其他程序复用，已拒绝操作该进程。',
                'Recorded PID still exists':'运行标记指向仍存在的进程，不能自动接管或重复启动。',
                'Instance is owned by another writer':'本实例仍被服务或维护操作占用，请等待现有操作完成。',
                'Embedding health unavailable':'恢复记忆所需的 Embedding 服务不可用，原数据和故障标记已保留。',
                'graph_memory':'图记忆状态需要专用恢复适配，当前不能自动恢复。',
                'Unsupported native memory schema':'记忆数据库版本不兼容，已拒绝自动恢复。',
                'Unsupported vector state':'存在未支持的向量状态，原始数据已保留。',
                'Interrupted upgrade':'检测到未完成升级，需要先通过原有升级恢复流程处理。',
                'SQLite integrity':'SQLite 数据完整性检查失败，已保留故障状态。',
                'file is not a database':'SQLite 数据文件损坏，原文件和故障标记已保留。',
                'database disk image is malformed':'SQLite 数据文件损坏，原文件和故障标记已保留。',
                'Dashboard port occupied':'本机管理端口已被占用，没有启动重复服务或终止其他程序。',
                'Chat failed startup':'Chat 启动失败，请查看实例日志。',
                'Chat readiness timed out':'Chat 尚未达到就绪条件，请查看日志和进程状态后重试。',
            }
            for line in reversed(details.splitlines()):
                if not line.startswith(('ValueError:','RuntimeError:','sqlite3.DatabaseError:','sqlite3.OperationalError:','psutil.AccessDenied:')):continue
                for pattern,message in reasons.items():
                    if pattern in line:raise Fault('failed',message,f'Diagnostics: {logfile}')
                break
        raise Fault('failed',f'{script} failed (exit {proc.returncode})',f'Inspect the private log: {logfile}')
    return json.loads(proc.stdout) if json_output else {'completed':True}

def shell(root,script,*args,json_output=False):
    executable=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    if os.name!='nt':raise Fault('incompatible','Windows service management requires Windows')
    env=clean_environment();env['PSMODULEPATH']=str(executable.parent/'Modules')
    # .NET Start-Process can leave a captured pipe inherited by the detached
    # service after PowerShell exits. Wait for the launcher, never for pipe EOF.
    log=root/'logs'/(script+'-'+uuid.uuid4().hex+'.log');log.parent.mkdir(exist_ok=True)
    with log.open('wb') as stream:
        proc=subprocess.run([str(executable),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT/'scripts'/script),*map(str,args)],stdout=stream,stderr=subprocess.STDOUT,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    output=log.read_text(encoding='utf-8-sig',errors='replace')
    if proc.returncode:raise Fault('failed',script+' failed',f'Inspect the private log: {log}')
    if json_output:
        text=output.strip()
        if text in {'True','False'}:return text=='True'
        return json.loads(text)
    return {'completed':True}

def inspect_environment(root,network=False):
    checks=[]
    def add(name,ok,detail,hint='',severity='error'):checks.append({'name':name,'ok':bool(ok),'severity':severity,'detail':detail,'hint':hint})
    add('windows_x64',os.name=='nt' and platform.machine().lower() in {'amd64','x86_64'},platform.platform(),'Use Windows x64; this release does not claim Linux validation.')
    if os.name=='nt':add('windows_version',sys.getwindowsversion().major>=10,str(sys.getwindowsversion()),'Use Windows 10/11 x64; older Windows versions are not supported.')
    add('python',sys.version_info[:3]==(3,12,3),platform.python_version(),'Use the pinned bootstrap interpreter or explicitly provide Python 3.12.3 x64.')
    parent=root
    while not parent.exists():parent=parent.parent
    free=shutil.disk_usage(parent).free
    add('disk_space',free>=2*1024**3,{'free_bytes':free,'minimum_core_bytes':2*1024**3},'Allow additional disk space for optional models and retained failed stages.')
    add('permissions',os.access(parent,os.W_OK),{'parent_writable':os.access(parent,os.W_OK)},'Choose a directory owned by your user; no administrator privileges are required for core files.')
    if os.name=='nt':
        import ctypes
        missing=[]
        for name in ['VCRUNTIME140.dll','VCRUNTIME140_1.dll','MSVCP140.dll']:
            try:ctypes.WinDLL(str(Path(os.environ['SystemRoot'])/'System32'/name))
            except OSError:missing.append(name)
        add('microsoft_runtime',not missing,{'missing':missing},'Review the pinned official Microsoft x64 runtime in manifests/assets.json; install manually if needed.')
    ports={}
    for port in [6185,6199,6202,6203,6099]:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.15):ports[str(port)]='occupied'
        except ConnectionRefusedError:ports[str(port)]='available'
        except OSError:ports[str(port)]='unknown (permission, timeout or network error)'
    add('ports',not any(v=='occupied' for v in ports.values()),ports,'Use config apply with another dashboard port; never kill a process just because its port is occupied.','warning')
    if network:
        for host in ['https://pypi.org/simple/','https://codeload.github.com/']:
            try:
                with urllib.request.urlopen(urllib.request.Request(host,method='HEAD'),timeout=10) as r:ok=r.status<500
            except urllib.error.HTTPError as exc:ok=exc.code<500
            except OSError:ok=False
            add('network:'+host,ok,{'reachable':ok},'Check DNS, proxy and TLS settings, or prepare a complete offline cache.','warning')
    return {'checks':checks,'ready':all(x['ok'] for x in checks if x['severity']=='error'),'network_tested':network,'installed':state(root).get('components',{}) if (root/'installation.json').exists() else {}}

@contextlib.contextmanager
def installation_lock(root):
    # Kernel lock, released even if the installer is interrupted.
    path=root/'.installation-lock';stream=path.open('a+b');stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise Fault('busy','Another installation or configuration operation is running')
        else:
            import fcntl
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise Fault('busy','Another operation is running')
        yield
    finally:stream.close()

def selection(text):
    values=set(text.split(','))
    if 'chat' in values:values.remove('chat');values.update(['python','astrbot','plugins'])
    if not values or values-set(COMPONENTS):raise Fault('invalid','Unknown component selection')
    if any(COMPONENTS[x].get('reserved') for x in values):raise Fault('incompatible','Dagu and Work are reserved descriptors, not installed functionality')
    while True:
        expanded=values|{dep for x in values for dep in COMPONENTS[x]['depends']}
        if expanded==values:return values
        values=expanded

def install_asset(root,name,spec,target,cache,offline=False,notify=progress):
    """Only a verified, completed stage can become an installed asset."""
    marker=root/'asset-receipts'/(name+'.json')
    def mutable(relative):
        parts=Path(relative).parts
        if name=='napcat-node':
            # Upstream Node mode stores account/runtime data here, never code.
            return relative in {'beacon_report.log','guild1.db','guild1.db-shm','guild1.db-wal'} or parts[0].lower() in {'logs','cache','temp'} or (parts[0]=='napcat' and mutable_napcat(parts[1:]))
        return name=='napcat' and (parts[0].lower() in {'config','logs','cache','temp'} or relative in {'.feiyu-process.json','.feiyu-process.lock','guild1.db','guild1.db-shm','guild1.db-wal'})
    def mutable_napcat(parts):
        return bool(parts) and (parts[0].lower() in {'config','logs','cache','temp'} or '/'.join(parts) in {'.feiyu-process.json','.feiyu-process.lock','guild1.db','guild1.db-shm','guild1.db-wal'})
    def contents(path):
        if path.is_file():return {'.':digest(path)}
        return {p.relative_to(path).as_posix():digest(p) for p in sorted(path.rglob('*')) if p.is_file() and not mutable(p.relative_to(path).as_posix())}
    if marker.exists():
        receipt=read(marker)
        expected={key:value for key,value in receipt.get('files',{}).items() if not mutable(key)}
        if receipt.get('sha256')!=spec['sha256'] or not target.exists() or expected!=contents(target):
            raise Fault('integrity','Installed asset receipt or content mismatch: '+name)
        return
    pending=marker.with_suffix('.pending.json')
    if target.exists():
        if pending.exists():
            receipt=read(pending)
            if receipt.get('id')==spec['id'] and receipt.get('sha256')==spec['sha256'] and receipt.get('path')==rel(root,target) and receipt.get('files')==contents(target):
                atomic(marker,receipt);return
        raise Fault('read_only','Unmanaged asset target exists; no overwrite')
    archive=acquire(spec,cache,offline,notify)
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=target.with_name(target.name+'.staging-'+secrets.token_hex(5))
    if spec['format']=='file':link_or_copy(archive,stage)
    else:unpack(archive,stage)
    receipt={'id':spec['id'],'sha256':spec['sha256'],'path':rel(root,target),'license_accepted':True,'files':contents(stage)}
    # Resume a completed rename only when pending receipt and all bytes match.
    atomic(pending,receipt)
    stage.rename(target)
    atomic(marker,receipt)

def activate_napcat_node(root,record,destination):
    """Switch only an owned, stopped service; retain config and the old payload."""
    previous=record.get('components',{}).get('napcat',{})
    service=destination/'napcat'
    for name in ['node.exe','index.js','wrapper.node','package.json','config.json']:
        if not (destination/name).is_file():raise Fault('integrity','Incomplete independent QQ runtime: '+name)
    if previous.get('installed'):
        if previous.get('ownership')!='managed':raise Fault('read_only','Refusing to replace an external NapCat')
        if component_status(root,record,'napcat').get('running'):raise Fault('busy','Stop owned NapCat before changing runtime')
        old=resolve(root,previous['path'])
        if old!=service and (old/'config').exists():
            for source in (old/'config').rglob('*'):
                if not source.is_file():continue
                if source.is_symlink():raise Fault('integrity','NapCat config symlink refused')
                target=service/'config'/source.relative_to(old/'config')
                if target.exists() and target.read_bytes()!=source.read_bytes():raise Fault('integrity','QQ runtime config conflict; original config retained')
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():shutil.copy2(source,target)
            webui=service/'config/webui.json'
            if webui.exists():
                value=read(webui);value['autoLoginAccount']='';atomic(webui,value)
    record['components']['napcat']={'installed':True,'version':'napcat-node-v4.18.41','path':rel(root,service),'ownership':'managed','depends':[],'runtime':'node'}
    record.pop('qq_executable',None)


def environment_profile(root,record,role):
    python=resolve(root,record[role+'_python'])
    receipt=read(python.parents[1]/'ffa-environment.json')
    profile=receipt.get('profile',role)
    if profile not in {'chat','core','speech'}:raise Fault('incompatible','Unknown dependency profile')
    if role=='chat' and profile not in {'chat','core'}:raise Fault('incompatible','Chat profile required')
    if role=='speech' and profile not in {'speech'}:raise Fault('incompatible','Speech profile required')
    return profile

def install(root,args):
    root=owned_root(root,True)
    with installation_lock(root):
        from instance import protect_boundary
        protect_boundary(root)
        record=state(root);cache=Path(args.cache).resolve() if args.cache else root/'cache'
        requested_profile=getattr(args,'dependency_profile',None)
        existing_profile=environment_profile(root,record,'chat') if record.get('chat_python') else None
        dependency_profile=requested_profile or (existing_profile.split('-speech')[0] if existing_profile else 'core')
        if record.get('instance') and existing_profile and existing_profile.startswith('chat') and dependency_profile=='core':raise Fault('incompatible','Existing full installation is preserved; slimming needs a new instance and verified migration')
        if record.get('instance'):
            runtime=instance(root,record)
            if args.repair and any((runtime/'private'/name).exists() for name in ['RUNNING','DIRTY']):
                invoke(root,chat_python(root,record),'manage.py','recover-lifecycle','--instance',runtime)
            if any((runtime/'private'/name).exists() for name in ['RUNNING','DIRTY','upgrade-journal.json']):
                raise Fault('busy','Stop the owned instance cleanly before installation or component changes')
            if args.repair:
                for profile in ['chat','speech']:
                    if not record.get(profile+'_python'):continue
                    marker=resolve(root,record[profile+'_python']).parents[1]/'ffa-environment.json'
                    if not marker.exists() or read(marker).get('lock_sha256')!=profile_fingerprint(environment_profile(root,record,profile)):
                        raise Fault('incompatible','Repair will not switch a data instance to changed dependency versions; use a tested upgrade adapter')
        chosen=selection(args.components);record.setdefault('components',{})
        journal=root/'install-journal.json'
        if journal.exists():atomic(root/'install-history'/(uuid.uuid4().hex+'.json'),read(journal))
        atomic(journal,{'schema_version':SCHEMA,'requested':sorted(chosen),'state':'running','started_at':time.time()})
        def save():atomic(root/'installation.json',record)
        progress('1/5','preflight and pinned component plan')
        check=inspect_environment(root,network=not args.offline and not (args.repair and record.get('instance')))
        missing_runtime=next((x for x in check['checks'] if x['name']=='microsoft_runtime' and not x['ok']),None)
        if missing_runtime and args.accept_external_licenses:
            progress('runtime','缺少 Microsoft 运行库；将下载已固定哈希的官方安装程序，仅此必要步骤可能请求系统确认')
            spec=next(x for x in load('assets.json') if x['id']=='msvc-runtime-x64-14.51.36247')
            installer=acquire(spec,cache,args.offline,progress)
            shell(root,'Install-Runtime.ps1','-Installer',installer)
            check=inspect_environment(root)
        if not check['ready']:raise Fault('needs_input','Environment preflight failed','Run check and resolve the error checks. Optional port warnings do not stop installation.')
        base=Path(args.python).resolve() if args.python else resolve(root,record['components']['python']['path']) if record.get('components',{}).get('python',{}).get('path') else Path(sys.executable)
        probe=subprocess.run([str(base),'-I','-c','import sys,struct; assert sys.version_info[:3]==(3,12,3) and struct.calcsize("P")==8'],capture_output=True)
        if probe.returncode:raise Fault('incompatible','Base interpreter must be Python 3.12.3 x64')
        record['components']['python']={'installed':True,'version':'3.12.3','path':rel(root,base),'ownership':'managed' if root in base.parents else 'external-readonly','depends':[]}
        progress('2/5','independent Python environments')
        if 'astrbot' in chosen:
            effective_profile=dependency_profile
            tag=profile_fingerprint(effective_profile)[:12]
            if effective_profile=='chat' and existing_profile=='core':
                python=extend_core_environment(chat_python(root,record),cache,args.offline,progress)
            elif record.get('chat_python') and existing_profile==effective_profile:
                python=install_environment(base,chat_python(root,record).parents[1],cache,effective_profile,args.offline,progress)
            else:
                python=install_environment(base,root/'envs'/(effective_profile+'-'+tag),cache,effective_profile,args.offline,progress)
            record['dependency_profile']=dependency_profile
            record['chat_python']=rel(root,python);save()
        if 'speech' in chosen:
            tag=profile_fingerprint('speech')[:12]
            python=install_environment(base,root/'envs'/('speech-'+tag),cache,'speech',args.offline,progress)
            record['speech_python']=rel(root,python);save()
        progress('3/5','verified upstream sources and compatibility patches')
        if 'astrbot' in chosen:
            plugins=args.plugins.split(',') if args.plugins else []
            if 'plugins' not in chosen:plugins=[]
            h=hashlib.sha256((PROJECT/'manifests/sources.json').read_bytes()+(PROJECT/'manifests/dashboard.json').read_bytes()+','.join(plugins).encode())
            h.update(RELEASE_LAYOUT.encode('ascii'))
            if 'meme_manager' in plugins:h.update((PROJECT/'manifests/starter-memes.json').read_bytes())
            for path in sorted((PROJECT/'src').rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:h.update(path.read_bytes())
            release=root/'releases'/('chat-'+h.hexdigest()[:16])
            if release.exists():
                from instance import verify_release
                verify_release(release)
            else:
                reuse=None
                if args.repair and record.get('instance'):
                    reuse=Path(read(instance(root,record)/'instance.json')['release']).resolve()
                    reuse.relative_to((root/'releases').resolve())
                build_release(release,cache,plugins,args.offline,progress,reuse=reuse)
            if record.get('instance'):
                active=Path(read(instance(root,record)/'instance.json')['release']).resolve()
                if active!=release:
                    if not args.repair:raise Fault('busy','Existing instance uses a different release','Use explicit repair after a clean stop; ordinary install never switches existing instances.')
                    before=read(active/'release.json');after=read(release/'release.json')
                    if not repair_release_compatible(before,after):
                        raise Fault('incompatible','Repair refuses changes to upstream versions or patches; a tested upgrade is required')
                    invoke(root,chat_python(root,record),'manage.py','upgrade','--instance',instance(root,record),'--release',release)
            record['release']=rel(root,release)
            record['components']['astrbot']={'installed':True,'version':load('sources.json')[0]['commit'],'path':rel(root,release/'astrbot'),'ownership':'managed','depends':['python']}
            record['components']['plugins']={'installed':bool(plugins),'version':{x['name']:x['commit'] for x in load('sources.json') if x['name'] in plugins},'path':rel(root,release/'plugins'),'ownership':'managed','depends':['astrbot']}
            save()
        progress('4/5','optional external assets')
        specs={x['id']:x for x in load('assets.json')}
        wanted=[]
        if 'napcat' in chosen:wanted.append(('napcat-node',specs['napcat-node-v4.18.41'],root/'services/napcat-node-v4.18.41'))
        if 'embedding' in chosen:
            wanted.extend([('embedding-engine',specs['llama-b11946-win-cpu'],root/'assets/llama-b11946'),('embedding-model',specs['embedding-bge-small-zh-v1.5'],root/'assets/bge-small-zh-v1.5-f16.gguf')])
        if 'speech' in chosen:wanted.append(('speech-model',specs['sensevoice-int8-20250909'],root/'assets/sensevoice-int8-20250909'))
        for name,spec,target in wanted:
            if not args.accept_external_licenses:raise Fault('needs_input','Optional asset requires review: '+spec['id'],'Review manifests/assets.json and use --accept-external-licenses only after accepting applicable external conditions.')
            install_asset(root,name,spec,target,cache,args.offline)
            record['components'][name]={'installed':True,'version':spec['id'],'path':rel(root,target),'ownership':'managed','depends':[]};save()
            if name=='napcat-node':
                activate_napcat_node(root,record,target);save()
        for component in ['embedding','speech']:
            if component in chosen:
                record['components'][component]={'installed':True,'version':COMPONENTS[component].get('asset',COMPONENTS[component].get('assets')),'path':'assets','ownership':'managed','depends':COMPONENTS[component]['depends']}
        progress('5/5','independent instance initialization')
        if 'astrbot' in chosen and not args.no_instance:
            if not record.get('instance'):
                if not args.owner:raise Fault('needs_input','A new instance needs your own QQ owner identity','Repeat install with --owner; downloaded software will be reused.')
                target=root/'instance'
                receipt=root/'instance-initialization.json'
                if target.exists():
                    origin=target/'private/install-origin.json'
                    if not receipt.exists() or not origin.exists():raise Fault('read_only','Unregistered instance directory exists; no ownership proof; no overwrite')
                    proof=read(receipt)
                    if proof!=read(origin) or proof.get('root_deployment')!=read(root/'.ffa-managed.json')['deployment_id'] or proof.get('owner')!=args.owner or proof.get('release')!=str(release):
                        raise Fault('read_only','Interrupted initialization ownership mismatch; no overwrite')
                    invoke(root,chat_python(root,record),'doctor.py','--instance',target,json_output=True)
                else:
                    stage=root/('instance.staging-'+secrets.token_hex(5))
                    proof={'transaction':secrets.token_hex(16),'root_deployment':read(root/'.ffa-managed.json')['deployment_id'],'owner':args.owner,'release':str(release)}
                    atomic(receipt,proof)
                    invoke(root,chat_python(root,record),'instance.py','init','--destination',stage,'--release',resolve(root,record['release']),'--owner',args.owner)
                    atomic(stage/'private/install-origin.json',proof)
                    stage.rename(target)
                record['instance']=rel(root,target);save()
            if 'embedding' in chosen or 'speech' in chosen:
                runtime=instance(root,record)
                existing_ids={x.get('id') for x in read(runtime/'data/cmd_config.json').get('provider',[])}
                missing=[x for x in ['embedding','speech'] if x in chosen and ('feiyu-local-embedding' if x=='embedding' else 'feiyu-local-stt') not in existing_ids]
                if missing:invoke(root,chat_python(root,record),'configure_local_models.py','--instance',runtime,'--components',','.join(missing),'--embedding-port',available_loopback_port(6203),'--speech-port',available_loopback_port(6202))
                prepare_instance_assets(root,record,runtime)
                for component in ['embedding','speech']:
                    if component in chosen:record['components'][component]={'installed':True,'version':COMPONENTS[component].get('asset',COMPONENTS[component].get('assets')),'path':rel(root,runtime),'ownership':'managed','depends':COMPONENTS[component]['depends']}
        save();atomic(journal,{'schema_version':SCHEMA,'state':'completed','requested':sorted(chosen)})
        if not getattr(args,'retain_cache',False) and cache==root/'cache' and cache.resolve().parent==root and not cache.is_symlink() and not cache.is_junction():
            from dependencies import prune_download_cache
            receipt=read(chat_python(root,record).parents[1]/'ffa-environment.json') if record.get('chat_python') else {}
            prune_download_cache(cache,receipt.get('locally_built',[]))
        return {'components':sorted(chosen),'instance_initialized':bool(record.get('instance')),'next_steps':guidance(root,record),'repeated_install_preserves_instance':True}

def prepare_instance_assets(root,record,runtime):
    for source,target in [('embedding-engine','engines/llama'),('embedding-model','models/embedding/bge-small-zh-v1.5-f16.gguf'),('speech-model','models/sensevoice')]:
        if source not in record['components']:continue
        origin=resolve(root,record['components'][source]['path']);destination=runtime/target
        files=list(origin.rglob('*')) if origin.is_dir() else [origin]
        for path in files:
            if not path.is_file():continue
            output=destination/path.relative_to(origin) if origin.is_dir() else destination
            output.resolve().relative_to(runtime.resolve())
            if output.exists():
                if digest(output)!=digest(path):raise Fault('integrity','Restored resource conflicts with pinned runtime asset')
                continue
            output.parent.mkdir(parents=True,exist_ok=True);link_or_copy(path,output)

def guidance(root,record):
    if not record.get('instance'):return ['Software is ready; initialize with your own QQ identity or restore a verified backup into a new instance.']
    target=instance(root,record);cfg=read(target/'data/cmd_config.json');port=cfg['dashboard']['port']
    return [f'肥鱼助手 / Fat-Fish-Assistant: start opens only the local service at http://127.0.0.1:{port}.',
            'Dashboard credentials are saved with Windows DPAPI; use the desktop launcher to securely update the saved password after changing it.',
            'The pinned official Node/QQ runtime is prepared automatically; NapCat QR login and device authorization remain manual. Never copy another user\'s login session, owner identity, key or approved groups.',
            'No group is approved by default. Dagu, Work and the P6 GUI are not implemented in this stage.']

def component_status(root,record,name):
    definition=COMPONENTS.get(name,{})
    info=dict(record.get('components',{}).get(name,{}))
    result={'id':name,'installed':bool(info.get('installed')),'running':None,'version':info.get('version'),'path':info.get('path'),'ownership':info.get('ownership'),'depends':definition.get('depends',[]),'reserved':definition.get('reserved',False)}
    result['manifest_refs']=['manifests/components.json','manifests/sources.json' if name in {'astrbot','plugins'} else 'manifests/python-runtime.json' if name=='python' else 'manifests/assets.json']
    if not result['installed']:return result
    if name=='python':
        result['environments']=[{'profile':profile,'path':record[profile+'_python'],'completion_receipt':(resolve(root,record[profile+'_python']).parents[1]/'ffa-environment.json').exists()} for profile in ['chat','speech'] if record.get(profile+'_python')]
    if name=='astrbot' and record.get('instance'):
        active=Path(read(instance(root,record)/'instance.json')['release'])
        metadata=read(active/'release.json')
        result['path']=rel(root,active/'astrbot');result['version']=next(x['commit'] for x in metadata['vendor'] if x['name']=='astrbot')
        result.update(invoke(root,chat_python(root,record),'manage.py','status','--instance',instance(root,record),json_output=True))
        result['maintenance_pending']=(instance(root,record)/'private/upgrade-journal.json').exists()
    elif name=='napcat':
        result.update(shell(root,'NapCat.ps1','-Action','Status','-Root',resolve(root,info['path']),json_output=True))
    elif name in {'embedding','speech'}:
        if not record.get('instance'):
            result.update({'running':False,'ready':False,'needs_input':'Initialize an instance before configuring local provider credentials.'});return result
        marker=instance(root,record)/'private'/(name.title()+'.pid')
        if marker.exists() or (marker.parent/(name.title()+'.process.json')).exists():
            result['running']=bool(shell(root,'Local-Models.ps1','-Action','Status','-Runtime',instance(root,record),'-Component',name.title(),json_output=True))
        else:result['running']=False
    if name=='plugins':
        if record.get('instance'):
            active=Path(read(instance(root,record)/'instance.json')['release'])
            result['path']=rel(root,instance(root,record)/'data/plugins')
            result['version']={x['name']:x['commit'] for x in read(active/'release.json')['vendor'] if x['name']!='astrbot'}
        result['capabilities']=['status','version','integrity','upgrade-with-release','rollback-with-release']
    return result

def operate(root,record,action,selected=None):
    if action in {'start','restart'} and record.get('instance') and (instance(root,record)/'private/MIGRATED-OUT.json').exists():
        raise Fault('busy','Source is suspended after migration; target must be stopped before explicit source reactivation')
    selected=selected or [x for x in ['napcat','embedding','speech','astrbot'] if record.get('components',{}).get(x,{}).get('installed')]
    if not selected:raise Fault('needs_input','No service components are installed')
    if action=='restart':
        operate(root,record,'stop',selected)
        return operate(root,record,'start',selected)
    if action=='stop':selected=list(reversed(selected))
    results=[]
    for name in selected:
        info=record.get('components',{}).get(name,{})
        if info.get('ownership')=='external-readonly':raise Fault('read_only','External component cannot be modified: '+name)
        if not info.get('installed'):raise Fault('missing','Component is not installed: '+name)
        current=component_status(root,record,name)
        # NapCat Stop also clears a stale managed launch record.
        cleanup_napcat=action=='stop' and name=='napcat' and current.get('managed',False)
        if current['running'] is (action=='start') and not cleanup_napcat:
            results.append({'id':name,'already_in_requested_state':True});continue
        if name=='astrbot':
            target=instance(root,record)
            if action=='start' and not (target/'private/experience-v3.json').exists():
                # The normal start path recovers stale markers inside manage.py.
                # Configuration migration must run only after that same recovery.
                invoke(root,chat_python(root,record),'manage.py','recover-lifecycle','--instance',target)
                invoke(root,chat_python(root,record),'configure_experience.py','--instance',target,'--release',resolve(root,record['release']),json_output=True)
            result=invoke(root,chat_python(root,record),'manage.py',action,'--instance',target)
        elif name=='napcat':
            argv=['-Action',action.title(),'-Root',resolve(root,info['path'])]
            if action=='start':
                if info.get('runtime')!='node':
                    if not record.get('qq_executable'):raise Fault('needs_input','Prepare the independent QQ runtime with prepare-napcat')
                    argv+=['-QQExecutable',record['qq_executable']]
            result=shell(root,'NapCat.ps1',*argv)
        elif name in {'embedding','speech'}:
            argv=['-Action',action.title(),'-Runtime',instance(root,record),'-Component',name.title()]
            cfg=read(instance(root,record)/'data/cmd_config.json')
            provider=next((p for p in cfg.get('provider',[]) if p.get('id')==('feiyu-local-embedding' if name=='embedding' else 'feiyu-local-stt')),None)
            if action=='start' and provider:
                endpoint=urllib.parse.urlsplit(provider.get('embedding_api_base' if name=='embedding' else 'api_base',''))
                if endpoint.scheme!='http' or endpoint.hostname not in {'127.0.0.1','localhost'}:raise Fault('invalid','Local model endpoint must use loopback')
                argv+=['-Port',endpoint.port or 80]
            if name=='speech' and action=='start':argv+=['-SpeechPython',resolve(root,record['speech_python'])]
            result=shell(root,'Local-Models.ps1',*argv)
        else:raise Fault('incompatible','This component has no service lifecycle: '+name)
        results.append({'id':name,**result})
    return {'operations':results}

def verify_runtime_environment(root,record,profile):
    if not record.get(profile+'_python'):raise Fault('missing','Runtime environment is not installed: '+profile)
    python=resolve(root,record[profile+'_python'])
    receipt=python.parents[1]/'ffa-environment.json'
    actual=environment_profile(root,record,profile)
    if not receipt.exists() or read(receipt).get('lock_sha256')!=profile_fingerprint(actual):raise Fault('incompatible','Runtime environment completion receipt or dependency lock mismatch: '+profile)
    log=root/'logs'/(profile+'-startup-environment.log')
    run_logged([python,'-I','-c','import sys,struct; assert sys.version_info[:3]==(3,12,3) and struct.calcsize("P")==8'],log)
    verify_environment_versions(python,load(actual+'.json'),log)
    run_logged([python,'-I','-m','pip','--isolated','check'],log)

def recover_napcat_login(root,record):
    info=record.get('components',{}).get('napcat',{})
    if info.get('runtime')!='node' or info.get('ownership')!='managed':raise Fault('read_only','Only the owned independent QQ runtime supports login recovery')
    def probe():return invoke(root,chat_python(root,record),'napcat_health.py','--root',resolve(root,info['path']),'--allow-pending',json_output=True)
    def stuck(value):return value.get('loginPhase')=='reconnecting' and not value.get('isLogin') and not value.get('qrLoginAccepted')
    first=probe()
    if not stuck(first):return {'restarted':False,'status':first}
    time.sleep(2)
    second=probe()
    if not stuck(second):return {'restarted':False,'status':second}
    operations=operate(root,record,'restart',['napcat'])
    for attempt in range(30):
        try:return {'restarted':True,'status':probe(),**operations}
        except Fault:
            if attempt==29:raise
            time.sleep(1)


def daily_start(root,record):
    """UX orchestration over the existing lifecycle APIs; optional failures are explicit."""
    warnings=[];operations=[]
    if record.get('instance'):
        runtime=instance(root,record)
        if (runtime/'private/MIGRATED-OUT.json').exists():raise Fault('busy','Source is suspended after migration; stop the target before explicitly resuming this source')
        if (runtime/'private/upgrade-journal.json').exists():
            invoke(root,chat_python(root,record),'manage.py','recover-upgrade','--instance',runtime)
            record['release']=rel(root,Path(read(runtime/'instance.json')['release']))
            atomic(root/'installation.json',record)
    verify_runtime_environment(root,record,'chat')
    for name in ['napcat','embedding','speech','astrbot']:
        if not record.get('components',{}).get(name,{}).get('installed'):continue
        try:
            if name=='speech':verify_runtime_environment(root,record,'speech')
            if name in {'embedding','speech'} and not component_status(root,record,name)['running']:
                from local_model_ports import repair_port
                # The live chat holds this lock; never rewrite endpoints mid-turn.
                try:reassigned=repair_port(instance(root,record),name)
                except RuntimeError as exc:
                    if 'Instance is owned by another writer' not in str(exc):raise
                    reassigned=None
                if reassigned:
                    progress('local-model-port',name+': '+str(reassigned['from'])+' -> '+str(reassigned['to']))
                    operations.append({'id':name,'local_port_reassigned':reassigned})
            operations.extend(operate(root,record,'start',[name])['operations'])
            if name in {'embedding','speech'}:
                for attempt in range(30):
                    try:
                        invoke(root,chat_python(root,record),'service_health.py','--component',name,'--instance',instance(root,record),json_output=True)
                        break
                    except Exception:
                        if attempt==29:raise
                        time.sleep(2)
        except Exception as exc:
            if name=='astrbot':raise
            warnings.append({'component':name,'message':str(exc),'hint':getattr(exc,'hint','Inspect installation and instance logs')})
    # A listening port alone does not establish dashboard identity or QQ login.
    dashboard=invoke(root,chat_python(root,record),'authenticated_health.py','--instance',instance(root,record),'--allow-login-required',json_output=True)
    connection={'napcat_installed':bool(record.get('components',{}).get('napcat',{}).get('installed')),'qq_login':False,'onebot':dashboard.get('onebot_connection',{'connected':False,'reason':'unavailable'})}
    if connection['napcat_installed']:
        try:
            connection.update(invoke(root,chat_python(root,record),'napcat_health.py','--root',resolve(root,record['components']['napcat']['path']),json_output=True))
            connection['qq_login']=True
        except Exception as exc:warnings.append({'component':'napcat-login','message':str(exc),'hint':getattr(exc,'hint','Complete manual login in the owned local WebUI')})
    return {'operations':operations,'dashboard':dashboard,'connection':connection,'warnings':warnings,'core_ready':True}

def config_view(root,record):
    target=instance(root,record);cfg=read(target/'data/cmd_config.json');identity=read(target/'private/instance-identity.json');access=read(target/'private/p2c-access.json')
    return {'project':'Fat-Fish-Assistant','dashboard':{'host':cfg['dashboard']['host'],'port':cfg['dashboard']['port']},'identity_valid':identity['owner']==access['owner'],
            'approved_group_count':len(access.get('approved_groups',[])),'providers':[{'id':x.get('id'),'type':x.get('type')} for x in cfg.get('provider',[])],
            'dashboard_password_file':rel(root,target/'private/dashboard-credential.dpapi'),'secrets_redacted':True,'qq_executable_configured':bool(record.get('qq_executable')),
            'experience':{'segmented_reply_enabled':bool(cfg.get('platform_settings',{}).get('segmented_reply',{}).get('enable')),
                'meme_plugin_installed':(target/'data/plugins/meme_manager').is_dir(),
                'meme_images':sum(1 for p in (target/'data/plugin_data/meme_manager/packs').rglob('*') if p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg','.gif','.webp'})}}

def configure(root,record,args):
    view=config_view(root,record)
    if args.config_action=='initialize-local-models':
        selected=[x for x in ['embedding','speech'] if record.get('components',{}).get(x,{}).get('installed')]
        if not selected:raise Fault('missing','No local model components are installed')
        with installation_lock(root):
            target=instance(root,record);cfg=read(target/'data/cmd_config.json')
            ports={}
            for name,default in [('embedding',6203),('speech',6202)]:
                provider=next((x for x in cfg.get('provider',[]) if x.get('id')==('feiyu-local-embedding' if name=='embedding' else 'feiyu-local-stt')),None)
                endpoint=urllib.parse.urlsplit(provider.get('embedding_api_base' if name=='embedding' else 'api_base','')) if provider else None
                if endpoint and (endpoint.scheme!='http' or endpoint.hostname not in {'127.0.0.1','localhost'}):raise Fault('invalid','Local model endpoint is not loopback; refusing to replace custom remote configuration')
                preferred=endpoint.port if endpoint and endpoint.port else default
                ports[name]=preferred if name in selected and component_status(root,record,name)['running'] else available_loopback_port(preferred)
            invoke(root,chat_python(root,record),'configure_local_models.py','--instance',target,'--components',','.join(selected),'--embedding-port',ports['embedding'],'--speech-port',ports['speech'])
        return {'configured':selected,'ports':ports,'credentials_changed':False,'permissions_changed':False}
    if args.config_action=='initialize-experience':
        with installation_lock(root):
            invoke(root,chat_python(root,record),'manage.py','recover-lifecycle','--instance',instance(root,record))
            return invoke(root,chat_python(root,record),'configure_experience.py','--instance',instance(root,record),'--release',resolve(root,record['release']),json_output=True)
    if args.config_action=='initialize-chat':
        with installation_lock(root):
            return invoke(root,chat_python(root,record),'configure_chat.py','--instance',instance(root,record),'--onebot-port',args.onebot_port,'--model',args.model,json_output=True)
    if args.config_action=='initialize-onebot':
        if not args.bot_id:raise Fault('needs_input','Provide your own Bot identity with --bot-id; no default account is imported')
        if component_status(root,record,'napcat')['running']:raise Fault('busy','Stop owned NapCat before connection initialization')
        with installation_lock(root):
            return invoke(root,chat_python(root,record),'configure_onebot.py','--root',root,'--bot-id',args.bot_id,json_output=True)
    if args.config_action=='show':return view
    if args.config_action=='validate':
        if not view['identity_valid'] or view['dashboard']['host']!='127.0.0.1':raise Fault('integrity','Identity or loopback configuration mismatch')
        return {'configuration_valid':True,'secrets_redacted':True}
    data=read_stdin() if args.file=='-' else read(args.file)
    if set(data)-{'dashboard_port','deepseek_api_key','qq_executable','napcat_webui_port'}:raise Fault('invalid','Only dashboard_port, deepseek_api_key, qq_executable and napcat_webui_port are writable here; owner/groups are not generic settings')
    target=instance(root,record)
    with installation_lock(root):
        from lifecycle_lock import exclusive
        with exclusive(target):
            if any((target/'private'/x).exists() for x in ['RUNNING','DIRTY','upgrade-journal.json']):raise Fault('busy','Stop cleanly before changing configuration')
            if 'dashboard_port' in data and (type(data['dashboard_port']) is not int or not 1024<=data['dashboard_port']<=65535):raise Fault('invalid','Dashboard port must be an integer from 1024 to 65535')
            if 'qq_executable' in data and (not Path(data['qq_executable']).is_file() or Path(data['qq_executable']).name.lower()!='qq.exe'):raise Fault('invalid','Provide an existing QQ.exe path')
            if 'deepseek_api_key' in data and (not isinstance(data['deepseek_api_key'],str) or not data['deepseek_api_key'].strip()):raise Fault('invalid','API key must be a nonempty string')
            if 'napcat_webui_port' in data:
                if type(data['napcat_webui_port']) is not int or not 1024<=data['napcat_webui_port']<=65535:raise Fault('invalid','Invalid NapCat WebUI port')
                info=record.get('components',{}).get('napcat',{})
                if not info.get('installed') or info.get('ownership')!='managed':raise Fault('read_only','Only an owned NapCat installation can be configured')
                if component_status(root,record,'napcat')['running']:raise Fault('busy','Stop the owned NapCat before configuration')
                nap_root=resolve(root,info['path']);nap_root.resolve().relative_to(root.resolve())
                nap_path=nap_root/'config/webui.json'
                nap_cfg=read(nap_path) if nap_path.exists() else {'token':secrets.token_hex(32),'loginRate':10,'autoLoginAccount':''}
                nap_cfg.update(host='127.0.0.1',port=data['napcat_webui_port'],disableWebUI=False,accessControlMode='whitelist',ipWhitelist=['127.0.0.1','::1'],enableXForwardedFor=False)
            history=target/'private/config-history'/uuid.uuid4().hex;history.mkdir(parents=True)
            shutil.copy2(target/'data/cmd_config.json',history/'cmd_config.json')
            if (target/'private/deepseek-key.txt').exists():shutil.copy2(target/'private/deepseek-key.txt',history/'deepseek-key.txt')
            cfg=read(target/'data/cmd_config.json')
            if 'dashboard_port' in data:cfg['dashboard']['port']=data['dashboard_port'];atomic(target/'data/cmd_config.json',cfg)
            if 'deepseek_api_key' in data:(target/'private/deepseek-key.txt').write_text(data['deepseek_api_key'].strip(),encoding='utf-8')
            if 'qq_executable' in data:record['qq_executable']=str(Path(data['qq_executable']).resolve());atomic(root/'installation.json',record)
            if 'napcat_webui_port' in data:
                if nap_path.exists():shutil.copy2(nap_path,history/'napcat-webui.json')
                atomic(nap_path,nap_cfg)
    return {'changed_fields':sorted(data),'secrets_redacted':True,'restart_required':True}

def parser():
    p=Parser(description='肥鱼助手 / Fat-Fish-Assistant management API v1')
    p.add_argument('--root',type=Path,default=default_root());p.add_argument('--json',action='store_true',help='JSON is already the default output contract')
    sub=p.add_subparsers(dest='command',required=True)
    check=sub.add_parser('check');check.add_argument('--network',action='store_true')
    setup=sub.add_parser('install');setup.add_argument('--components',default='chat');setup.add_argument('--plugins',default='astrbot_plugin_livingmemory,astrbot_plugin_continuous_message,meme_manager')
    setup.add_argument('--owner');setup.add_argument('--python',type=Path);setup.add_argument('--cache',type=Path);setup.add_argument('--offline',action='store_true');setup.add_argument('--accept-external-licenses',action='store_true');setup.add_argument('--no-instance',action='store_true')
    setup.add_argument('--dependency-profile',choices=['core','chat'])
    setup.add_argument('--retain-cache',action='store_true')
    setup.add_argument('--repair',action='store_true',help='Explicit same-dependency repair through the existing transactional upgrade mechanism')
    nap=sub.add_parser('prepare-napcat');nap.add_argument('--accept-external-licenses',action='store_true');nap.add_argument('--cache',type=Path);nap.add_argument('--offline',action='store_true')
    for name in ['status','components','start','daily-start','stop','restart','health','diagnose','wizard','rollback']:sub.add_parser(name)
    component=sub.add_parser('component');component.add_argument('name',choices=list(COMPONENTS));component.add_argument('action',choices=['status','version','dependencies','start','stop','restart','health','integrity','login-status','recover-login','connection-health'])
    conf=sub.add_parser('config');conf.add_argument('config_action',choices=['show','validate','apply','initialize-chat','initialize-onebot','initialize-experience','initialize-local-models']);conf.add_argument('--file',default='-');conf.add_argument('--onebot-port',type=int,default=6199);conf.add_argument('--model',choices=['deepseek-chat','deepseek-reasoner'],default='deepseek-chat');conf.add_argument('--bot-id')
    backup=sub.add_parser('backup');backup.add_argument('--destination',required=True,type=Path)
    restore=sub.add_parser('restore');restore.add_argument('--snapshot',required=True,type=Path);restore.add_argument('--destination',type=Path)
    upgrade=sub.add_parser('upgrade');upgrade.add_argument('--release',required=True,type=Path)
    recover=sub.add_parser('recover-upgrade')
    export=sub.add_parser('migration-export');export.add_argument('--destination',required=True,type=Path)
    migration=sub.add_parser('migration-import');migration.add_argument('--package',required=True,type=Path);migration.add_argument('--confirm-source-offline',action='store_true');migration.add_argument('--cache',type=Path);migration.add_argument('--offline',action='store_true')
    resume=sub.add_parser('resume-source');resume.add_argument('--confirm-target-offline',action='store_true')
    register=sub.add_parser('register-external');register.add_argument('name',choices=list(COMPONENTS));register.add_argument('--path',required=True,type=Path);register.add_argument('--version',required=True)
    return p

def dispatch(args):
    root=args.root.resolve()
    if args.command=='check':return inspect_environment(root,args.network)
    if args.command=='install':return install(root,args)
    root=owned_root(root);record=state(root)
    if args.command=='prepare-napcat':
        if not args.accept_external_licenses:raise Fault('needs_input','Accept the QQ, Node and NapCat licenses before preparing the independent runtime')
        with installation_lock(root):
            target=instance(root,record)
            from lifecycle_lock import exclusive
            with exclusive(target,'.start-lock'),exclusive(target):
                if any((target/'private'/x).exists() for x in ['RUNNING','DIRTY','upgrade-journal.json']):raise Fault('busy','Stop cleanly before preparing the QQ runtime')
                if component_status(root,record,'napcat').get('running'):raise Fault('busy','Stop owned NapCat before changing its runtime')
                spec=next(x for x in load('assets.json') if x['id']=='napcat-node-v4.18.41')
                destination=root/'services/napcat-node-v4.18.41'
                install_asset(root,'napcat-node',spec,destination,args.cache or root/'cache',args.offline)
                activate_napcat_node(root,record,destination)
                atomic(root/'installation.json',record)
        return {'prepared':True,'runtime':'official-node','system_qq_modified':False,'login_migrated':False,'manual_login_required':True}
    if args.command=='resume-source':
        if not args.confirm_target_offline:raise Fault('needs_input','Confirm the target host is stopped before resuming the source')
        from lifecycle_lock import exclusive
        target=instance(root,record)
        with installation_lock(root),exclusive(target,'.start-lock'),exclusive(target):
            marker=target/'private/MIGRATED-OUT.json'
            if marker.exists():marker.rename(target/'private'/('migration-resumed-'+secrets.token_hex(8)+'.json'))
        return {'source_reactivated':True,'target_must_remain_stopped':True}
    if args.command in {'migration-export','migration-import'}:
        password=read_stdin().get('password')
        if not isinstance(password,str):raise Fault('invalid','A migration password is required on stdin')
        argv=['--root',root,args.command]
        if args.command=='migration-export':argv+=['--destination',args.destination]
        else:
            argv+=['--package',args.package]
            if args.confirm_source_offline:argv+=['--confirm-source-offline']
            if args.cache:argv+=['--cache',args.cache]
            if args.offline:argv+=['--offline']
        return invoke(root,chat_python(root,record),'migration_cli.py',*argv,json_output=True,input_data=json.dumps({'password':password}))
    if args.command in {'status','components'}:return {'components':[component_status(root,record,x) for x in COMPONENTS],'schema_version':SCHEMA}
    if args.command=='register-external':
        if not args.path.exists():raise Fault('missing','External path does not exist')
        with installation_lock(root):
            if args.name in record.get('components',{}):raise Fault('read_only','Existing component record will not be replaced')
            record.setdefault('components',{})[args.name]={'installed':True,'ownership':'external-readonly','version':args.version,'path':str(args.path.resolve())};atomic(root/'installation.json',record)
        return {'registered_read_only':True,'id':args.name}
    if args.command=='daily-start':
        with installation_lock(root):return daily_start(root,record)
    if args.command in {'start','stop','restart'}:
        with installation_lock(root):return operate(root,record,args.command)
    if args.command=='component':
        if args.action in {'login-status','recover-login'}:
            if args.name!='napcat':raise Fault('invalid','Login state is only available for NapCat')
            if args.action=='recover-login':
                with installation_lock(root):return recover_napcat_login(root,record)
            info=record.get('components',{}).get('napcat',{})
            if not info.get('installed') or info.get('ownership')!='managed':raise Fault('read_only','Only the owned NapCat login state can be queried')
            return invoke(root,chat_python(root,record),'napcat_health.py','--root',resolve(root,info['path']),'--allow-pending',json_output=True)
        if args.action=='connection-health':
            if args.name!='astrbot':raise Fault('invalid','Connection evidence is only available for AstrBot')
            from connection_health import read_connection
            return {'onebot_connection':read_connection(instance(root,record))}
        if args.action in {'status','version','dependencies'}:return component_status(root,record,args.name)
        if args.action in {'start','stop','restart'}:
            with installation_lock(root):return operate(root,record,args.action,[args.name])
        if args.name in {'dagu','work'}:raise Fault('incompatible','Reserved component has no implementation')
        if not record.get('components',{}).get(args.name,{}).get('installed'):raise Fault('missing','Component is not installed: '+args.name)
        if args.action=='health' and args.name=='astrbot':
            return invoke(root,chat_python(root,record),'authenticated_health.py','--instance',instance(root,record),json_output=True)
        if args.action=='health' and args.name in {'embedding','speech'}:
            return invoke(root,chat_python(root,record),'service_health.py','--component',args.name,'--instance',instance(root,record),json_output=True)
        if args.action=='health' and args.name=='napcat':
            return invoke(root,chat_python(root,record),'napcat_health.py','--root',resolve(root,record['components']['napcat']['path']),json_output=True)
        if args.name in {'astrbot','plugins'}:
            return invoke(root,chat_python(root,record),'doctor.py','--instance',instance(root,record),json_output=True)
        if args.name=='napcat':
            return shell(root,'NapCat.ps1','-Action','Diagnose','-Root',resolve(root,record['components']['napcat']['path']))
        if args.name in {'embedding','speech'}:
            runtime=instance(root,record)
            for row in load('model-runtime-lock.json'):
                if row['component'].lower()!=args.name:continue
                path=(runtime/row['path']).resolve();path.relative_to(runtime.resolve())
                if digest(path)!=row['sha256']:raise Fault('integrity','Model artifact hash mismatch')
            return {'component':args.name,'pinned_artifact_integrity':'pass'}
        if args.name=='python':
            profiles=[x for x in ['chat','speech'] if record.get(x+'_python')]
            if 'chat' in profiles:
                argv=['-ChatPython',resolve(root,record['chat_python'])]
                if 'speech' in profiles:argv+=['-SpeechPython',resolve(root,record['speech_python'])]
                return shell(root,'Check-Environment.ps1',*argv)
            base=resolve(root,record['components']['python']['path'])
            run_logged([base,'-I','-c','import sys,ssl,sqlite3; assert sys.version_info[:3]==(3,12,3)'],root/'logs/python-probe.log')
            return {'python_stdlib_probe':'pass','scope':'Base interpreter only; no Chat environment installed'}
        raise Fault('incompatible','Unsupported probe for this component')
    if args.command=='config':return configure(root,record,args)
    if args.command=='wizard':return {'manual_steps':guidance(root,record),'secret_input':'Use config apply --file - with JSON on stdin; secrets are never command-line arguments or echoed in results.'}
    if args.command in {'health','diagnose'}:
        result={'components':[component_status(root,record,x) for x in COMPONENTS]}
        if args.command=='diagnose':result['environment']=inspect_environment(root)
        if record.get('instance'):
            result['configuration']=config_view(root,record)
            if component_status(root,record,'astrbot')['running']:result['dashboard']=invoke(root,chat_python(root,record),'authenticated_health.py','--instance',instance(root,record),json_output=True)
            else:raise Fault('needs_input','AstrBot is stopped; configuration checks do not imply service health','Use start, then health.')
        else:raise Fault('needs_input','No instance is initialized; health is not passed')
        for component in ['embedding','speech']:
            if record.get('components',{}).get(component,{}).get('installed'):
                result[component]=invoke(root,chat_python(root,record),'service_health.py','--component',component,'--instance',instance(root,record),json_output=True)
        if record.get('components',{}).get('napcat',{}).get('installed'):
            result['napcat']=invoke(root,chat_python(root,record),'napcat_health.py','--root',resolve(root,record['components']['napcat']['path']),json_output=True)
        return result
    if args.command=='backup':return invoke(root,chat_python(root,record),'instance.py','backup','--instance',instance(root,record),'--destination',args.destination.resolve())
    if args.command=='restore':
        destination=args.destination.resolve() if args.destination else root/'instance'
        if root not in destination.parents:raise Fault('read_only','Restore destination must be inside this managed root')
        result=invoke(root,chat_python(root,record),'instance.py','restore','--snapshot',args.snapshot.resolve(),'--destination',destination,'--release',resolve(root,record['release']))
        record['instance']=rel(root,destination);atomic(root/'installation.json',record);return result
    if args.command=='upgrade':
        release=args.release.resolve()
        if root not in release.parents:raise Fault('read_only','Upgrade release must be inside this managed root')
        result=invoke(root,chat_python(root,record),'manage.py','upgrade','--instance',instance(root,record),'--release',release)
        record['release']=rel(root,release);atomic(root/'installation.json',record);return result
    if args.command in {'rollback','recover-upgrade'}:
        result=invoke(root,chat_python(root,record),'manage.py',args.command,'--instance',instance(root,record))
        release=Path(read(instance(root,record)/'instance.json')['release']);record['release']=rel(root,release);atomic(root/'installation.json',record);return result
    raise Fault('invalid','Unknown operation')

def main(argv=None):
    request=uuid.uuid4().hex
    try:
        args=parser().parse_args(argv);data=dispatch(args)
        value={'schema_version':SCHEMA,'project':'Fat-Fish-Assistant','request_id':request,'ok':True,'code':0,'data':data,'errors':[]}
    except Fault as exc:value={'schema_version':SCHEMA,'project':'Fat-Fish-Assistant','request_id':request,'ok':False,'code':EXIT[exc.kind],'data':None,'errors':[{'type':exc.kind,'message':str(exc),'hint':exc.hint}]}
    except Exception as exc:
        kind='integrity' if 'hash' in str(exc).lower() else 'missing' if isinstance(exc,FileNotFoundError) else 'invalid' if isinstance(exc,(ValueError,KeyError)) else 'failed'
        value={'schema_version':SCHEMA,'project':'Fat-Fish-Assistant','request_id':request,'ok':False,'code':EXIT[kind],'data':None,'errors':[{'type':kind,'message':str(exc),'hint':'Inspect local logs and retained failed stages; current instances are never overwritten by retry.'}]}
    if not value['ok'] and 'args' in locals() and args.command=='install':
        try:
            journal=args.root.resolve()/'install-journal.json'
            if journal.exists():
                details=read(journal);details.update(state='failed',error_code=value['code'],errors=value['errors'],failed_at=time.time());atomic(journal,details)
        except OSError:pass
    print(json.dumps(value,ensure_ascii=True))
    return value['code']
