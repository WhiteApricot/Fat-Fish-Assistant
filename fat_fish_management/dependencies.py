"""Pinned external acquisition and minimal, hash-checked compatibility patches.

Only this module creates third-party payloads, always outside the source repository.
"""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hashlib
from html.parser import HTMLParser
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
RELEASE_LAYOUT='runtime-tree-v1'

def link_or_copy(source,target):
    try:os.link(source,target)
    except OSError:shutil.copy2(source,target)

def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def load(name):return json.loads((PROJECT/'manifests'/name).read_text(encoding='utf-8'))

def prune_download_cache(cache,built=()):
    """Evict only known, hash-verified inputs; leave unknown/user files alone."""
    cache=external(cache)
    removed=0
    for manifest in ['core.json','chat.json','speech.json','build.json','sources.json','assets.json','dashboard.json','python-runtime.json','starter-memes.json']:
        rows=load(manifest);rows=rows.get('files',[rows]) if isinstance(rows,dict) else rows
        for row in rows:
            name=row.get('file',row.get('filename'))
            expected=row.get('archive_sha256',row.get('sha256'))
            if not name or Path(name).name!=name:continue
            for group in ['', 'artifacts','core-wheels','chat-wheels','speech-wheels','build-tools','full-extra-wheels']:
                path=cache/group/name
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(cache) and digest(path)==expected:
                    removed+=path.stat().st_size;path.unlink()
    for row in built:
        name=row.get('file');expected=row.get('wheel_sha256')
        if not name or Path(name).name!=name or not expected:continue
        for group in ['core-wheels','chat-wheels']:
            path=cache/group/name
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(cache) and digest(path)==expected:
                removed+=path.stat().st_size;path.unlink()
    return removed

def acquisition_receipt(cache,name,source,sha256):
    # Only public pinned source URLs enter this log. No business credentials.
    with (cache/'acquisition.jsonl').open('a',encoding='utf-8') as log:
        log.write(json.dumps({'file':name,'source':source,'sha256':sha256,'time':time.time()})+'\n')

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
        acquisition_receipt(cache,name,'verified-cache',expected)
        return target
    if offline:raise FileNotFoundError('Offline cache miss: '+name)
    url=spec.get('url')
    candidates=[]
    if spec.get('source','').startswith('https://pypi.org/'):
        # Resolve only the exact locked filename; never let an index choose versions.
        class Links(HTMLParser):
            def handle_starttag(self,tag,attrs):
                href=dict(attrs).get('href','')
                if tag=='a':self.links.append(href)
        index='https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/'+spec['name'].lower().replace('_','-')+'/'
        try:
            parser=Links();parser.links=[]
            with urllib.request.urlopen(index,timeout=15) as r:parser.feed(r.read().decode('utf-8'))
            for href in parser.links:
                candidate=urllib.parse.urljoin(index,href)
                parsed=urllib.parse.urlparse(candidate)
                if parsed.scheme=='https' and parsed.hostname in {'mirrors.tuna.tsinghua.edu.cn','pypi.tuna.tsinghua.edu.cn'} and urllib.parse.unquote(parsed.path.rsplit('/',1)[-1])==name:
                    candidates.append(candidate)
        except Exception:pass  # Official metadata remains available as fallback.
    if url and url.startswith('https://huggingface.co/'):
        candidates.append(url.replace('https://huggingface.co/','https://hf-mirror.com/',1))
    # Upstream NapCat installer documents ghfast.top; public pinned downloads
    # only. Never send API tokens or route runtime business requests here.
    if url and url.startswith('https://github.com/') and not urllib.parse.urlparse(url).query:
        candidates.append('https://ghfast.top/'+url)
    if url and url.startswith('https://codeload.github.com/'):
        parts=urllib.parse.urlparse(url).path.strip('/').split('/')
        if len(parts)==4 and parts[2]=='zip':
            candidates.append('https://ghfast.top/https://github.com/'+parts[0]+'/'+parts[1]+'/archive/'+parts[3]+'.zip')
    candidates.extend(spec.get('mirrors',[]))
    # Resolve official PyPI lazily, so a working mirror needs no overseas request.
    candidates.append(url)
    if progress:progress('download',name)
    errors=[]
    for candidate in candidates:
        partial=target.with_name(target.name+'.part-'+secrets.token_hex(5))
        try:
            if candidate is None:
                metadata_url=spec['source']
                if urllib.parse.urlparse(metadata_url).hostname!='pypi.org':raise ValueError('Untrusted package resolver')
                with urllib.request.urlopen(metadata_url,timeout=30) as r:metadata=json.load(r)
                row=next((x for x in metadata['urls'] if x['filename']==name and x['digests']['sha256']==expected),None)
                if row is None:raise ValueError('Pinned artifact unavailable')
                candidate=row['url']
            if urllib.parse.urlparse(candidate).scheme!='https':raise ValueError('HTTPS source required')
            with urllib.request.urlopen(candidate,timeout=30) as r,partial.open('xb') as out:
                if urllib.parse.urlparse(r.url).scheme!='https':raise ValueError('Insecure redirect refused')
                total=int(getattr(r,'headers',{}).get('Content-Length',0) or 0)
                started=time.monotonic();received=0;last=0
                def notify(done=False):
                    if progress and os.environ.get('FFA_TRANSFER_PROGRESS')=='1':
                        elapsed=max(time.monotonic()-started,.001)
                        eta=round(max(0,total-received)*elapsed/received) if total>0 and received>0 else None
                        progress('transfer',json.dumps({'file':name,'source':urllib.parse.urlparse(candidate).hostname,'bytes':received,'total':total,'eta_seconds':eta,'done':done},ensure_ascii=False))
                notify()
                while True:
                    chunk=r.read(65536)
                    if not chunk:break
                    out.write(chunk);received+=len(chunk)
                    if time.monotonic()-last>=.25:notify();last=time.monotonic()
                notify(True)
            if digest(partial)!=expected:raise ValueError('Downloaded hash mismatch: '+name)
            partial.replace(target)
            acquisition_receipt(cache,name,candidate,expected)
            break
        except Exception as exc:
            errors.append(type(exc).__name__)
            partial.unlink(missing_ok=True)
    else:raise RuntimeError('All pinned sources failed: '+name+'; '+','.join(errors))

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

def build_release(destination,cache,plugins=None,offline=False,progress=None,*,reuse=None):
    destination=external(destination)
    if destination.exists():raise ValueError('Release destination exists')
    plugins=DEFAULT_PLUGINS if plugins is None else plugins
    if set(plugins)-set(DEFAULT_PLUGINS):raise ValueError('Unknown plugin selection')
    stage=destination.with_name(destination.name+'.staging-'+secrets.token_hex(5))
    selected=[x for x in load('sources.json') if x['name'] in ['astrbot','selfreply-decision',*plugins]]
    dashboard=load('dashboard.json')
    reusable_files=[]
    if reuse is not None:
        previous=json.loads((reuse/'release.json').read_text(encoding='utf-8'))
        if previous.get('external_sources')==selected and previous.get('dashboard')==dashboard:
            from fat_fish_management.instance import verify_release
            verify_release(reuse)
            reusable_files=json.loads((reuse/'payload-manifest.json').read_text(encoding='utf-8'))
    def copy_verified_tree(relative):
        prefix=relative.rstrip('/')+'/'
        matched=False
        for row in reusable_files:
            if row['path'].startswith(prefix):
                output=safe_path(stage,row['path'])
                output.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(safe_path(reuse,row['path']),output)
                matched=True
        if matched and progress:progress('reuse-installed',relative)
        return matched
    stage.mkdir(parents=True)
    try:
        shutil.copytree(PROJECT/'src',stage/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (stage/'plugins').mkdir()
        for spec in selected:
            if copy_verified_tree(spec['target']):continue
            archive=acquire(spec,cache,offline,progress)
            target=safe_path(stage,spec['target'])
            selection=spec.get('select')
            if spec['name']=='astrbot':
                # Ship runtime code, root metadata/licenses and every existing
                # patch target. Keep frontend source/docs/test tooling in the
                # pinned source archive, not each installed release.
                patch_paths={x['path'] for x in json.loads((PROJECT/spec['patches']).read_text(encoding='utf-8'))} if spec.get('patches') else set()
                with zipfile.ZipFile(archive) as package:
                    names=[item.filename[len(spec['strip_prefix']):] for item in package.infolist() if not item.is_dir()]
                selection={name for name in names if name.startswith('astrbot/') or '/' not in name or name in patch_paths}
            unpack(archive,target,spec['strip_prefix'],selection)
            apply_patches(target,spec)
            if spec['name']=='selfreply-decision':(target/'__init__.py').write_text('"""Externally acquired GPL-3.0 decision subset; see LICENSE."""\n')
        target=safe_path(stage,dashboard['target'])
        if not target.exists():
            archive=acquire(dashboard,cache,offline,progress)
            unpack(archive,target,dashboard['strip_prefix'])
        if (target/'assets/version').read_text(encoding='utf-8').strip()!=dashboard['version'] or not (target/'index.html').is_file():
            raise ValueError('Pinned dashboard version/content mismatch')
        shutil.copytree(PROJECT/'plugins/fat_fish_guard',stage/'plugins/fat_fish_guard',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copytree(PROJECT/'plugins/fat_fish_health',stage/'plugins/fat_fish_health',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for plugin in ('fat_fish_forwards','fat_fish_reminders'):
            shutil.copytree(PROJECT/'plugins'/plugin,stage/'plugins'/plugin,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        if 'meme_manager' in plugins:
            catalog=load('starter-memes.json')
            resources=stage/'resources/starter-memes';resources.mkdir(parents=True)
            for row in catalog['files']:
                relative='resources/starter-memes/'+row['path']
                previous_file=safe_path(reuse,relative) if reusable_files else None
                if previous_file and previous_file.is_file() and digest(previous_file)==row['sha256']:
                    acquired=previous_file
                else:
                    acquired=acquire(row,cache,offline,progress)
                output=safe_path(resources,row['path']);output.parent.mkdir(parents=True,exist_ok=True)
                if acquired==previous_file:
                    shutil.copy2(acquired,output)
                else:
                    link_or_copy(acquired,output)
            (resources/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
        vendor=[x for x in selected if x['name']!='selfreply-decision']
        (stage/'release.json').write_text(json.dumps({'schema':1,'project':'Fat-Fish-Assistant','vendor':vendor,'external_sources':selected,'dashboard':dashboard}),encoding='utf-8')
        from fat_fish_management.instance import seal_release
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
    if profile in {'chat','core'}:data+=(PROJECT/'manifests/build.json').read_bytes()
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
            if not target.exists():link_or_copy(path,target)
            resolved.append((row['name'],row['version'],row['sha256']))
    if sources:
        tools=load('build.json');tool_cache=cache/'build-tools';tool_cache.mkdir(exist_ok=True)
        for row in tools:
            path=acquire(row,cache/'artifacts',offline,progress)
            target=tool_cache/path.name
            if not target.exists():link_or_copy(path,target)
            if digest(target)!=row['sha256']:raise ValueError('Build tool hash mismatch')
        builder=stage.parent/(stage.name+'-builder')
        run_logged([base_python,'-I','-m','venv',builder],log)
        build_python=builder/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        tool_lock=builder/'build.lock.txt';tool_lock.write_text('\n'.join(f"{x['name']}=={x['version']} --hash=sha256:{x['sha256']}" for x in tools)+'\n')
        run_logged([build_python,'-I','-m','pip','--isolated','install','--no-cache-dir','--no-compile','--no-index','--no-deps','--find-links',tool_cache,'--require-hashes','-r',tool_lock],log)
        for row in sources:
            if progress:progress('build-from-pinned-source',row['name']+'=='+row['version'])
            source=acquire(row,cache/'artifacts',offline,progress)
            out=stage/(row['name']+'-wheel');out.mkdir()
            run_logged([build_python,'-I','-m','pip','--isolated','wheel','--no-cache-dir','--no-index','--no-deps','--no-build-isolation','--wheel-dir',out,source],log)
            wheel=next(out.glob('*.whl'));hashvalue=digest(wheel)
            shutil.copy2(wheel,wheels/wheel.name)
            resolved.append((row['name'],row['version'],hashvalue))
            built.append({'name':row['name'],'source_sha256':row['sha256'],'wheel_sha256':hashvalue,'file':wheel.name,'build_tools':tools,'source_date_epoch':1704067200})
    lock=stage/'resolved.lock.txt';lock.write_text('\n'.join(f'{n}=={v} --hash=sha256:{h}' for n,v,h in resolved)+'\n')
    if progress:progress('install-locked-packages',profile+': '+str(len(resolved)))
    run_logged([python,'-I','-m','pip','--isolated','install','--no-cache-dir','--no-compile','--no-index','--no-deps','--find-links',wheels,'--require-hashes','-r',lock],log)
    run_logged([python,'-I','-m','pip','--isolated','check'],log)
    verify_environment_versions(python,rows,log)
    (stage/'ffa-environment.json').write_text(json.dumps({'schema_version':1,'profile':profile,'lock_sha256':fingerprint,'activation_layout':'final-versioned-path','locally_built':built}),encoding='utf-8')
    if sources:
        shutil.rmtree(builder)
        for row in sources:shutil.rmtree(stage/(row['name']+'-wheel'))
    return destination/('Scripts/python.exe' if os.name=='nt' else 'bin/python')

def ensure_build_quiet(destination):
    """An interrupted install must not archive a directory still used by an orphan builder."""
    if os.name!='nt':raise RuntimeError('Interrupted environment recovery needs a supported writer probe')
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    proc=subprocess.run([str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT/'scripts/build/Assert-Build-Writers.ps1'),'-Destination',str(destination),'-ControllerPid',str(os.getpid())],capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if proc.returncode:raise RuntimeError('Cannot verify interrupted build writers; partial environment retained')
    if json.loads(proc.stdout.removeprefix('\ufeff'))['active_count']:
        raise RuntimeError('An interrupted build still has active Python workers; wait for it to exit before retry; nothing was moved')


def extend_core_environment(python,cache,offline=False,progress=None):
    """Add the locked optional wheels without re-downloading/replacing core.

    Caller holds the installation lock and has verified a cleanly stopped
    instance. Optional rows must be disjoint wheels at the same pinned versions.
    """
    python=external(python);destination=python.parents[1];cache=external(cache)
    marker=destination/'ffa-environment.json';before=marker.read_bytes();receipt=json.loads(before)
    if receipt.get('profile')!='core' or receipt.get('lock_sha256')!=profile_fingerprint('core'):raise ValueError('A verified core environment is required')
    core={r['name']:r for r in load('core.json')};full=load('chat.json');extra=[r for r in full if r['name'] not in core]
    if any(r!=core[r['name']] for r in full if r['name'] in core):raise ValueError('Core pins differ; no in-place extension')
    if any(r['format']!='wheel' for r in extra):raise ValueError('Optional source builds need a separate compatibility review')
    log=destination/'optional-install.log'
    verify_environment_versions(python,list(core.values()),log)
    code="import importlib.metadata as m,json;print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}))"
    probe=subprocess.run([str(python),'-I','-c',code],capture_output=True,text=True,check=True,env=clean_environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    installed=json.loads(probe.stdout);added=[]
    for row in extra:
        name=row['name'].lower().replace('_','-')
        if name in installed and installed[name]!=row['version']:raise ValueError('Unmanaged optional version conflict; no overwrite: '+name)
        if name not in installed:added.append(row['name'])
    wheels=cache/'full-extra-wheels';wheels.mkdir(parents=True,exist_ok=True)
    for row in extra:
        artifact=acquire(row,cache/'artifacts',offline,progress);target=wheels/artifact.name
        if not target.exists():link_or_copy(artifact,target)
        if digest(target)!=row['sha256']:raise ValueError('Optional wheel cache hash mismatch')
    lock=destination/'optional.lock.txt';lock.write_text('\n'.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in extra)+'\n')
    backup=destination/'ffa-environment-before-full.json'
    if not backup.exists():backup.write_bytes(before)
    plan_path=destination/'optional-extension.json'
    plan=json.loads(plan_path.read_text()) if plan_path.exists() else {}
    if plan.get('state')=='installing':
        if plan.get('core_lock')!=profile_fingerprint('core') or plan.get('full_lock')!=profile_fingerprint('chat') or set(plan.get('added',[]))-{r['name'] for r in extra}:raise ValueError('Interrupted extension lock mismatch')
        added=plan['added']
    plan={'state':'installing','core_lock':profile_fingerprint('core'),'full_lock':profile_fingerprint('chat'),'added':added}
    plan_path.write_text(json.dumps(plan))
    try:
        run_logged([python,'-I','-m','pip','--isolated','install','--no-cache-dir','--no-compile','--no-index','--no-deps','--find-links',wheels,'--require-hashes','-r',lock],log)
        verify_environment_versions(python,full,log)
        run_logged([python,'-I','-m','pip','--isolated','check'],log)
    except Exception:
        if added:run_logged([python,'-I','-m','pip','--isolated','uninstall','--yes',*added],log)
        verify_environment_versions(python,list(core.values()),log)
        run_logged([python,'-I','-m','pip','--isolated','check'],log)
        raise
    receipt.update(profile='chat',lock_sha256=profile_fingerprint('chat'),extension={'from':'core','added':added,'core_lock_sha256':profile_fingerprint('core')})
    pending=marker.with_suffix('.pending');pending.write_text(json.dumps(receipt),encoding='utf-8');pending.replace(marker)
    plan['state']='completed';plan_path.write_text(json.dumps(plan))
    return python


def rollback_optional_environment(python):
    """Caller must stop services and confirm optional features are disabled."""
    python=external(python);destination=python.parents[1];marker=destination/'ffa-environment.json'
    receipt=json.loads(marker.read_text());backup=destination/'ffa-environment-before-full.json';before=json.loads(backup.read_text())
    if receipt.get('lock_sha256')!=profile_fingerprint('chat') or before.get('lock_sha256')!=profile_fingerprint('core'):raise ValueError('Rollback lock mismatch; no removal')
    core={r['name'] for r in load('core.json')};allowed={r['name']:r for r in load('chat.json') if r['name'] not in core}
    added=receipt.get('extension',{}).get('added',[])
    if receipt.get('extension',{}).get('from')!='core' or set(added)-set(allowed):raise ValueError('Missing verified extension receipt; no removal')
    log=destination/'optional-rollback.log'
    verify_environment_versions(python,[allowed[n] for n in added],log)
    (destination/'ffa-environment-last-full.json').write_bytes(marker.read_bytes())
    if added:run_logged([python,'-I','-m','pip','--isolated','uninstall','--yes',*added],log)
    verify_environment_versions(python,load('core.json'),log)
    run_logged([python,'-I','-m','pip','--isolated','check'],log)
    pending=marker.with_suffix('.pending');pending.write_bytes(backup.read_bytes());pending.replace(marker)
    plan=destination/'optional-extension.json'
    if plan.exists():
        data=json.loads(plan.read_text());data['state']='rolled-back';plan.write_text(json.dumps(data))
