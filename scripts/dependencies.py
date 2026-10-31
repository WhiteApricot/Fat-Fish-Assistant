"""Pinned external acquisition and minimal, hash-checked compatibility patches.

Only this module creates third-party payloads, always outside the source repository.
"""
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

PROJECT=Path(__file__).resolve().parents[1]
DEFAULT_PLUGINS=['astrbot_plugin_livingmemory','astrbot_plugin_continuous_message','meme_manager']

def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def load(name):return json.loads((PROJECT/'manifests'/name).read_text(encoding='utf-8'))

def external(path):
    path=Path(path).resolve()
    if path==PROJECT or PROJECT in path.parents:raise ValueError('Third-party payloads must be outside the repository')
    return path

def safe_path(root,name):
    if '\\' in name or ':' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:raise ValueError('Unsafe archive path')
    path=(root/name).resolve();path.relative_to(root.resolve());return path

def acquire(spec,cache,offline=False,progress=None):
    cache=external(cache);cache.mkdir(parents=True,exist_ok=True)
    name=spec.get('filename',spec.get('file'))
    if not name or Path(name).name!=name:raise ValueError('Invalid cache filename')
    target=cache/name;expected=spec.get('archive_sha256',spec.get('sha256'))
    if not expected or len(expected)!=64:raise ValueError('Missing pinned SHA-256')
    if target.exists():
        if digest(target)!=expected:raise ValueError('Cached hash mismatch: '+name+'; quarantine it before retry')
        if progress:progress('cache-hit',name)
        return target
    if offline:raise FileNotFoundError('Offline cache miss: '+name)
    url=spec.get('url')
    if not url:
        metadata_url=spec['source']
        if urllib.parse.urlparse(metadata_url).hostname!='pypi.org':raise ValueError('Untrusted package resolver')
        with urllib.request.urlopen(metadata_url,timeout=30) as r:metadata=json.load(r)
        row=next((x for x in metadata['urls'] if x['filename']==name and x['digests']['sha256']==expected),None)
        if row is None:raise ValueError('Pinned artifact unavailable; substitution forbidden: '+name)
        url=row['url']
    if urllib.parse.urlparse(url).scheme!='https':raise ValueError('HTTPS source required')
    partial=target.with_name(target.name+'.part-'+secrets.token_hex(5))
    if progress:progress('download',name)
    with urllib.request.urlopen(url,timeout=90) as r,partial.open('xb') as out:
        if urllib.parse.urlparse(r.url).scheme!='https':raise ValueError('Insecure redirect refused')
        shutil.copyfileobj(r,out)
    if digest(partial)!=expected:
        partial.rename(partial.with_name(partial.name+'.rejected'))
        raise ValueError('Downloaded hash mismatch: '+name)
    partial.replace(target)
    return target

def unpack(archive,destination,prefix='',select=None):
    destination=external(destination)
    destination.mkdir(parents=True,exist_ok=False)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                if not item.filename.startswith(prefix):raise ValueError('Archive prefix mismatch')
                name=item.filename[len(prefix):]
                if not name or item.is_dir():continue
                if select is not None and name not in select:continue
                if stat.S_ISLNK(item.external_attr>>16):raise ValueError('Archive symlink refused')
                target=safe_path(destination,name);target.parent.mkdir(parents=True,exist_ok=True)
                with z.open(item) as r,target.open('xb') as out:shutil.copyfileobj(r,out)
    else:
        with tarfile.open(archive) as tar:
            for item in tar.getmembers():
                safe_path(destination,item.name)
                if item.issym() or item.islnk() or item.isdev():raise ValueError('Archive links/devices refused')
            tar.extractall(destination,filter='data')

def apply_patches(target,spec):
    if not spec.get('patches'):return
    patchfile=PROJECT/spec['patches']
    if digest(patchfile)!=spec['patch_sha256']:raise ValueError('Patch manifest hash mismatch')
    for change in json.loads(patchfile.read_text(encoding='utf-8')):
        path=safe_path(target,change['path'])
        if (digest(path) if path.exists() else None)!=change['before']:raise ValueError('Patch precondition mismatch: '+change['path'])
        lines=path.read_bytes().decode('utf-8').splitlines(keepends=True) if path.exists() else []
        last=len(lines)+1
        for edit in reversed(change['edits']):
            a,b=edit['start'],edit['end']
            if not 0<=a<=b<=len(lines) or b>last or lines[a:b]!=edit['old']:raise ValueError('Patch context mismatch')
            lines[a:b]=edit['new'];last=a
        data=''.join(lines).encode('utf-8')
        if hashlib.sha256(data).hexdigest()!=change['after']:raise ValueError('Patch result hash mismatch')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)

def build_release(destination,cache,plugins=None,offline=False,progress=None):
    destination=external(destination)
    if destination.exists():raise ValueError('Release destination exists')
    plugins=DEFAULT_PLUGINS if plugins is None else plugins
    if set(plugins)-set(DEFAULT_PLUGINS):raise ValueError('Unknown plugin selection')
    stage=destination.with_name(destination.name+'.staging-'+secrets.token_hex(5));stage.mkdir(parents=True)
    selected=[x for x in load('sources.json') if x['name'] in ['astrbot','selfreply-decision',*plugins]]
    try:
        shutil.copytree(PROJECT/'src',stage/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (stage/'plugins').mkdir()
        for spec in selected:
            archive=acquire(spec,cache,offline,progress)
            target=safe_path(stage,spec['target'])
            unpack(archive,target,spec['strip_prefix'],spec.get('select'))
            apply_patches(target,spec)
            if spec['name']=='selfreply-decision':(target/'__init__.py').write_text('"""Externally acquired GPL-3.0 decision subset; see LICENSE."""\n')
        dashboard=load('dashboard.json')
        archive=acquire(dashboard,cache,offline,progress)
        target=safe_path(stage,dashboard['target'])
        unpack(archive,target,dashboard['strip_prefix'])
        if (target/'assets/version').read_text(encoding='utf-8').strip()!=dashboard['version'] or not (target/'index.html').is_file():
            raise ValueError('Pinned dashboard version/content mismatch')
        shutil.copytree(PROJECT/'src/fat_fish_guard',stage/'plugins/fat_fish_guard',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copytree(PROJECT/'src/fat_fish_health',stage/'plugins/fat_fish_health',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        vendor=[x for x in selected if x['name']!='selfreply-decision']
        (stage/'release.json').write_text(json.dumps({'schema':1,'project':'Fat-Fish-Assistant','vendor':vendor,'external_sources':selected,'dashboard':dashboard}),encoding='utf-8')
        from instance import seal_release
        seal_release(stage);stage.rename(destination)
    except Exception:
        # Keep the failed stage for diagnostics, never expose it as an active release.
        (stage/'BUILD_FAILED').write_text('Not an activatable release; inspect local installation log')
        raise

def clean_environment():
    env=dict(os.environ,PYTHONNOUSERSITE='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',SOURCE_DATE_EPOCH='1704067200')
    env.pop('PYTHONHOME',None);env.pop('PYTHONPATH',None)
    return env

def profile_fingerprint(profile):
    # v2 builds the venv at its final versioned path: Windows console launchers
    # embed absolute interpreter paths and must never be moved after installation.
    data=(PROJECT/'manifests'/(profile+'.json')).read_bytes()+b'\0venv-layout-v2'
    if profile=='chat':data+=(PROJECT/'manifests/build.json').read_bytes()
    return hashlib.sha256(data).hexdigest()

def run_logged(args,log,env=None):
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('ab') as f:
        command=list(map(str,args))
        if Path(command[0]).name.lower() in {'python.exe','pythonw.exe','python','python3','python3.12'}:command[1:1]=['-X','utf8']
        proc=subprocess.Popen(command,stdout=f,stderr=subprocess.STDOUT,env=env or clean_environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        started=time.monotonic()
        while True:
            try:proc.wait(timeout=30);break
            except subprocess.TimeoutExpired:
                print(f'[Fat-Fish-Assistant] 依赖处理仍在运行（{int(time.monotonic()-started)} 秒）；日志：{log.name}',file=sys.stderr,flush=True)
    if proc.returncode:raise RuntimeError('Subprocess failed; inspect '+str(log))

def verify_environment_versions(python,rows,log):
    expected={row['name']:row['version'] for row in rows}
    code="""import importlib.metadata as m,json,re,sys
normalize=lambda name:re.sub(r'[-_.]+','-',name).lower()
installed={normalize(d.metadata['Name']):d.version for d in m.distributions()}
bad={name:{'expected':version,'installed':installed.get(normalize(name))} for name,version in json.loads(sys.argv[1]).items() if installed.get(normalize(name))!=version}
if bad:raise SystemExit('Pinned dependency version mismatch: '+json.dumps(bad,sort_keys=True))
print('All pinned dependency versions match')
"""
    run_logged([python,'-I','-c',code,json.dumps(expected)],log)

def install_environment(base_python,destination,cache,profile='chat',offline=False,progress=None):
    """Build pinned sdists locally; derive a local lock only from verified inputs."""
    destination=external(destination);cache=external(cache)
    fingerprint=profile_fingerprint(profile)
    marker=destination/'ffa-environment.json'
    if destination.exists():
        if marker.exists() and json.loads(marker.read_text()).get('lock_sha256')==fingerprint:
            python=destination/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            verify_environment_versions(python,load(profile+'.json'),destination/'validation.log')
            run_logged([python,'-I','-m','pip','--isolated','check'],destination/'validation.log')
            return python
        partial=destination/'ffa-environment-building.json'
        if not marker.exists() and partial.exists() and json.loads(partial.read_text()).get('lock_sha256')==fingerprint:
            ensure_build_quiet(destination)
            destination.rename(destination.with_name(destination.name+'.failed-'+secrets.token_hex(5)))
        else:raise ValueError('Existing environment is unmanaged or incompatible; no overwrite')
    stage=destination;stage.mkdir(parents=True)
    (stage/'ffa-environment-building.json').write_text(json.dumps({'project':'Fat-Fish-Assistant','lock_sha256':fingerprint}))
    log=stage.parent/(stage.name+'-'+secrets.token_hex(4)+'.log')
    run_logged([base_python,'-I','-m','venv',stage],log)
    python=stage/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    wheels=cache/(profile+'-wheels');wheels.mkdir(parents=True,exist_ok=True)
    rows=load(profile+'.json');sources=[x for x in rows if x['format']=='sdist'];resolved=[];built=[]
    for row in rows:
        path=acquire(row,cache/'artifacts',offline,progress)
        if row['format']=='wheel':
            target=wheels/path.name
            if target.exists() and digest(target)!=row['sha256']:raise ValueError('Wheel cache hash mismatch')
            if not target.exists():shutil.copy2(path,target)
            resolved.append((row['name'],row['version'],row['sha256']))
    if sources:
        tools=load('build.json');tool_cache=cache/'build-tools';tool_cache.mkdir(exist_ok=True)
        for row in tools:
            path=acquire(row,cache/'artifacts',offline,progress)
            target=tool_cache/path.name
            if not target.exists():shutil.copy2(path,target)
            if digest(target)!=row['sha256']:raise ValueError('Build tool hash mismatch')
        builder=stage.parent/(stage.name+'-builder')
        run_logged([base_python,'-I','-m','venv',builder],log)
        build_python=builder/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        tool_lock=builder/'build.lock.txt';tool_lock.write_text('\n'.join(f"{x['name']}=={x['version']} --hash=sha256:{x['sha256']}" for x in tools)+'\n')
        run_logged([build_python,'-I','-m','pip','--isolated','install','--no-index','--no-deps','--find-links',tool_cache,'--require-hashes','-r',tool_lock],log)
        for row in sources:
            if progress:progress('build-from-pinned-source',row['name']+'=='+row['version'])
            source=acquire(row,cache/'artifacts',offline,progress)
            out=stage/(row['name']+'-wheel');out.mkdir()
            run_logged([build_python,'-I','-m','pip','--isolated','wheel','--no-index','--no-deps','--no-build-isolation','--wheel-dir',out,source],log)
            wheel=next(out.glob('*.whl'));hashvalue=digest(wheel)
            shutil.copy2(wheel,wheels/wheel.name)
            resolved.append((row['name'],row['version'],hashvalue))
            built.append({'name':row['name'],'source_sha256':row['sha256'],'wheel_sha256':hashvalue,'file':wheel.name,'build_tools':tools,'source_date_epoch':1704067200})
    lock=stage/'resolved.lock.txt';lock.write_text('\n'.join(f'{n}=={v} --hash=sha256:{h}' for n,v,h in resolved)+'\n')
    if progress:progress('install-locked-packages',profile+': '+str(len(resolved)))
    run_logged([python,'-I','-m','pip','--isolated','install','--no-index','--no-deps','--find-links',wheels,'--require-hashes','-r',lock],log)
    run_logged([python,'-I','-m','pip','--isolated','check'],log)
    verify_environment_versions(python,rows,log)
    (stage/'ffa-environment.json').write_text(json.dumps({'schema_version':1,'profile':profile,'lock_sha256':fingerprint,'activation_layout':'final-versioned-path','locally_built':built}),encoding='utf-8')
    return destination/('Scripts/python.exe' if os.name=='nt' else 'bin/python')

def ensure_build_quiet(destination):
    """An interrupted install must not archive a directory still used by an orphan builder."""
    if os.name!='nt':raise RuntimeError('Interrupted environment recovery needs a supported writer probe')
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    proc=subprocess.run([str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT/'scripts/Test-Build-Writers.ps1'),'-Destination',str(destination),'-ControllerPid',str(os.getpid())],capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if proc.returncode:raise RuntimeError('Cannot verify interrupted build writers; partial environment retained')
    if json.loads(proc.stdout.removeprefix('\ufeff'))['active_count']:
        raise RuntimeError('An interrupted build still has active Python workers; wait for it to exit before retry; nothing was moved')
