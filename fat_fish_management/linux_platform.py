"""Ubuntu installation and systemd adapter for the shared controller."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from fat_fish_management import controller as c
from fat_fish_management.dependencies import PROJECT, acquire, digest, load, unpack

NAPCAT_OUTPUTS=('napcat/napcat.mjs','qq/opt/QQ/qq','qq/opt/QQ/resources/app/wrapper.node','libnapcat_launcher.so')


def install_napcat(root, cache, offline, progress):
    specs = load('linux-x86_64/napcat.json')
    shell, qq, launcher = specs
    parent = root / 'services'
    parent.mkdir(exist_ok=True)
    target = parent / ('napcat-shell-' + shell['version'])
    receipt = {row['file']: row['sha256'] for row in specs}
    marker = target / 'assembly.json'
    if target.exists():
        if not marker.is_file() or c.read(marker)['inputs'] != receipt:
            raise c.Fault('integrity', 'Existing NapCat assembly is incompatible')
        verify_napcat(target)
    else:
        paths = [acquire(row, cache, offline, progress) for row in specs]
        with tempfile.TemporaryDirectory(prefix='.napcat-stage-', dir=parent) as directory:
            stage = Path(directory) / 'runtime'
            stage.mkdir(mode=0o700)
            unpack(paths[0], stage/'napcat')
            version = subprocess.check_output(['dpkg-deb', '-f', paths[1], 'Version'], text=True).strip()
            arch = subprocess.check_output(['dpkg-deb', '-f', paths[1], 'Architecture'], text=True).strip()
            if (version, arch) != (qq['version'], qq['architecture']):
                raise c.Fault('integrity', 'QQ package version or architecture mismatch')
            subprocess.run(['dpkg-deb', '-x', paths[1], stage/'qq'], check=True)
            # g++ requires a C++ suffix even if the verified cache stores an alternative name.
            subprocess.run(['g++','-x','c++','-shared','-fPIC',paths[2],'-o',stage/'libnapcat_launcher.so','-ldl'],check=True)
            for path in (stage/'qq/opt/QQ/qq', stage/'qq/opt/QQ/resources/app/wrapper.node'):
                report=subprocess.check_output(['ldd',path],text=True)
                if 'not found' in report:
                    raise c.Fault('needs_input','Linux QQ runtime libraries are missing',report)
            config = stage/'napcat/config/webui.json'
            config.parent.mkdir(exist_ok=True)
            c.atomic(config, {
                'host':'127.0.0.1','port':c.available_loopback_port(6099),
                'token':secrets.token_hex(32),'loginRate':10,'autoLoginAccount':'',
                'disableWebUI':False,'accessControlMode':'whitelist',
                'ipWhitelist':['127.0.0.1','::1'],'enableXForwardedFor':False,
            })
            config.chmod(0o600)
            c.atomic(stage/'assembly.json', {'inputs':receipt,'outputs':{name:digest(stage/name) for name in NAPCAT_OUTPUTS}})
            verify_napcat(stage)
            stage.rename(target)
    return {'installed':True,'version':shell['version'],'qq_version':qq['version'],
            'runtime':'linux-shell','path':c.rel(root,target),'ownership':'managed','depends':[]}


def verify_napcat(target):
    receipt=c.read(target/'assembly.json')
    expected_inputs={row['file']:row['sha256'] for row in load('linux-x86_64/napcat.json')}
    if receipt.get('inputs')!=expected_inputs or set(receipt.get('outputs',{}))!=set(NAPCAT_OUTPUTS):
        raise c.Fault('integrity','NapCat assembly receipt differs from the platform lock')
    for name, expected in receipt['outputs'].items():
        if digest(target/name) != expected:
            raise c.Fault('integrity', 'NapCat assembly hash mismatch: '+name)
    webui_config(target)


def webui_config(target):
    path = target/'napcat/config/webui.json'
    cfg = c.read(path)
    if cfg.get('host') != '127.0.0.1' or not 1024 <= int(cfg['port']) <= 65535:
        raise c.Fault('integrity', 'NapCat WebUI must use a loopback port')
    if path.is_symlink() or path.stat().st_mode & 0o077 or not cfg.get('token'):
        raise c.Fault('integrity', 'NapCat WebUI token file is not private')
    return cfg


def unit(root, service):
    deployment = c.read(root/'.ffa-managed.json')['deployment_id']
    return f'ffa-{deployment}-{service}.service'


def systemctl(*args, check=True):
    result = subprocess.run(['systemctl','--user',*args],capture_output=True,text=True)
    if check and result.returncode:
        raise c.Fault('failed','systemd user service operation failed',result.stderr.strip())
    return result


def active(root, service):
    return systemctl('is-active','--quiet',unit(root,service),check=False).returncode == 0


def loopback_opener():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            raise ValueError('Local service redirected the request')
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())


def api(config):
    opener = loopback_opener()
    base = f"http://127.0.0.1:{config['port']}/api/"
    def call(path, body=None, credential=None):
        headers={'Content-Type':'application/json'}
        if credential:headers['Authorization']='Bearer '+credential
        request=urllib.request.Request(base+path,json.dumps(body or {}).encode(),headers)
        with opener.open(request,timeout=3) as response:
            result=json.load(response)
        if result['code'] != 0:raise c.Fault('needs_input','NapCat rejected '+path)
        return result.get('data')
    password_hash=hashlib.sha256((config['token']+'.napcat').encode()).hexdigest()
    credential=call('auth/login',{'hash':password_hash})['Credential']
    return lambda path,body=None:call(path,body,credential)


def napcat_status(root, target):
    config=webui_config(target)
    result={'running':active(root,'napcat'),'qq_logged_in':False,'webui_ready':False,
            'webui_port':config['port']}
    if result['running']:
        try:
            state=api(config)('QQLogin/CheckLoginStatus')
            result.update(webui_ready=True,qq_logged_in=bool(state.get('isLogin') and state.get('coreReady')),
                          login_phase=state.get('loginPhase'))
        except (OSError,ValueError,KeyError,c.Fault):
            pass
    return result


def astrbot_status(root,record,running):
    instance=c.instance(root,record)
    cfg=c.read(instance/'data/cmd_config.json')['dashboard']
    result={'systemd_active':active(root,'astrbot'),'dashboard_port':cfg['port'],
            'astrbot_ready':False,'onebot_connection':{'connected':False,'reason':'service_not_ready'}}
    if running and result['systemd_active']:
        try:
            # This worker checks Dashboard authentication and current OneBot identity evidence.
            dashboard=c.invoke(root,c.chat_python(root,record),'authenticated_health.py','--instance',instance,json_output=True)
            result['astrbot_ready']=dashboard.get('authenticated_dashboard')=='pass'
            result['onebot_connection']=dashboard['onebot_connection']
        except (OSError,ValueError,c.Fault):
            pass
    return result


def health(root,record,diagnose=False):
    components=[c.component_status(root,record,name) for name in c.COMPONENTS]
    result={'components':components}
    if diagnose:result['environment']=c.inspect_environment(root)
    return result


def napcat_path(root, record):
    info=record['components'].get('napcat',{})
    if not info.get('installed') or info.get('ownership')!='managed':
        raise c.Fault('missing','Managed Linux NapCat is not installed')
    path=c.resolve(root,info['path'])
    path.resolve().relative_to(root.resolve())
    return path


def stop(root, record, service):
    if service == 'astrbot' and active(root,service):
        instance=c.instance(root,record)
        if (instance/'private/RUNNING').exists():
            (instance/'private/STOP').touch()
            for _ in range(60):
                if not active(root,service):break
                time.sleep(.5)
            else:raise c.Fault('busy','AstrBot did not stop cleanly; inspect service logs')
    if active(root,service):systemctl('stop',unit(root,service))


def require_ports(root,record,instance,services):
    ports=[]
    if 'astrbot' in services and not active(root,'astrbot'):
        config=c.read(instance/'data/cmd_config.json')
        if config['dashboard']['host']!='127.0.0.1':
            raise c.Fault('integrity','Dashboard must bind loopback')
        ports.append(config['dashboard']['port'])
        for item in config.get('platform',[]):
            if item.get('id')=='feiyu-onebot' and item.get('enable'):
                if item.get('ws_reverse_host')!='127.0.0.1':
                    raise c.Fault('integrity','OneBot must bind loopback')
                ports.append(item['ws_reverse_port'])
    if 'napcat' in services and not active(root,'napcat'):
        ports.append(webui_config(napcat_path(root,record))['port'])
    if len(set(ports))!=len(ports):raise c.Fault('invalid','Service ports must be distinct')
    for port in ports:
        with socket.socket() as probe:
            try:probe.bind(('127.0.0.1',port))
            except OSError as exc:
                raise c.Fault('busy',f'Loopback port {port} is unavailable','Use config apply on the stopped instance to choose another port.') from exc


def operate(root, record, action, selected):
    services=[name for name in ('astrbot','napcat') if name in selected]
    if len(services)!=len(selected):raise c.Fault('incompatible','No Linux lifecycle for the requested component')
    systemctl('show-environment')
    if action in {'stop','restart'}:
        for name in reversed(services):stop(root,record,name)
    if action in {'start','restart'}:
        instance=c.instance(root,record)
        if (instance/'private/MIGRATED-OUT.json').exists() or (instance/'private/upgrade-journal.json').exists():
            raise c.Fault('busy','Instance is paused or requires maintenance')
        require_ports(root,record,instance,services)
        for name in services:
            if active(root,name):continue
            properties=['--property=UMask=0077','--property=KillMode=control-group']
            if name=='astrbot':
                c.verify_runtime_environment(root,record,'chat')
                python=c.chat_python(root,record)
                c.invoke(root,python,'manage.py','recover-lifecycle','--instance',instance)
                from fat_fish_management.instance import verify_release,verify_plugins
                release=c.resolve(root,record['release'])
                verify_release(release)
                verify_plugins(instance,release)
                command=[python,'-B',PROJECT/'fat_fish_management/run_chat.py','--instance',instance]
            else:
                target=napcat_path(root,record)
                verify_napcat(target)
                properties+=['--property=StandardOutput=null','--property=StandardError=null']
                if os.environ.get('FFA_SOFTWARE_RENDER')=='1':properties+=['--setenv=FFA_SOFTWARE_RENDER=1']
                command=[sys.executable,'-B',PROJECT/'fat_fish_management/linux_platform.py',target]
            result=subprocess.run(['systemd-run','--user','--collect','--service-type=exec',
                                   '--unit='+unit(root,name),*properties,'--',*map(str,command)],
                                  capture_output=True,text=True)
            if result.returncode:raise c.Fault('failed','Could not start '+name,result.stderr.strip())
    return {'operations':[{'id':name,'running':active(root,name)} for name in services]}


def connect_qq(root, record):
    call=api(webui_config(napcat_path(root,record)))
    login=call('QQLogin/CheckLoginStatus')
    if not login.get('isLogin') or not login.get('coreReady'):
        raise c.Fault('needs_input','Complete the QQ login before connecting OneBot')
    identity=str(call('QQLogin/GetQQLoginInfo').get('uin',''))
    if not identity.isascii() or not identity.isdecimal():
        raise c.Fault('integrity','NapCat did not return a valid account identity')
    instance=c.instance(root,record)
    path=instance/'private/bot-identity.json'
    if path.exists() and c.read(path).get('bot_id')!=identity:
        raise c.Fault('integrity','NapCat account differs from the bound instance')
    platform=next(item for item in c.read(instance/'data/cmd_config.json')['platform'] if item.get('id')=='feiyu-onebot')
    if platform['ws_reverse_host']!='127.0.0.1':
        raise c.Fault('integrity','OneBot must bind loopback')
    token=(instance/'private/onebot-token.txt').read_text().strip()
    if token!=platform['ws_reverse_token']:
        raise c.Fault('integrity','OneBot credential mismatch')
    config=call('OB11Config/GetConfig')
    if isinstance(config,str):config=json.loads(config)
    if not isinstance(config,dict) or 'network' not in config:
        raise c.Fault('integrity','Unexpected NapCat OneBot configuration format')
    clients=config['network'].setdefault('websocketClients',[])
    entry={'name':'Fat-Fish-Assistant','enable':True,'messagePostFormat':'array','token':token,
           'url':f"ws://127.0.0.1:{platform['ws_reverse_port']}/ws",'reportSelfMessage':False,
           'reconnectInterval':3000,'heartInterval':30000}
    matches=[i for i,item in enumerate(clients) if item.get('name')==entry['name']]
    if len(matches)>1:raise c.Fault('integrity','Duplicate managed OneBot clients')
    if matches:clients[matches[0]]=entry
    else:clients.append(entry)
    call('OB11Config/SetConfig',{'config':json.dumps(config)})
    c.atomic(path,{'bot_id':identity})
    return {'configured':True,'qq_logged_in':True}


def open_webui(root, record, service, browser_port=None):
    if service=='napcat':
        cfg=webui_config(napcat_path(root,record))
        route='/webui/web_login?'+urllib.parse.urlencode({'token':cfg['token']})
    else:
        cfg=c.read(c.instance(root,record)/'data/cmd_config.json')['dashboard']
        if cfg.get('host')!='127.0.0.1':raise c.Fault('integrity','Dashboard must bind loopback')
        from fat_fish_management.dashboard_login import DashboardLoginRequired,login_url
        try:
            parsed=urllib.parse.urlsplit(login_url(c.instance(root,record)))
            route=urllib.parse.urlunsplit(('','',parsed.path,parsed.query,parsed.fragment))
        except DashboardLoginRequired:
            route='/'
    port=int(cfg['port']) if browser_port is None else browser_port
    if not 1024<=port<=65535:raise c.Fault('invalid','Invalid local browser port')
    url=f'http://127.0.0.1:{port}'+route
    if 'microsoft' in os.uname().release.lower() and shutil.which('powershell.exe'):
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                               'Start-Process ([Console]::In.ReadToEnd())'],input=url,text=True,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        opened=result.returncode==0
    elif os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        opened=webbrowser.open(url)
    else:
        opened=False
    if not opened:raise c.Fault('needs_input','No local browser is available',f'Forward the service loopback port {port} to a trusted local browser.')
    return {'opened':service,'port':port}


def manage(root, record, args):
    if args.command=='connect-qq':
        with c.installation_lock(root):return connect_qq(root,record)
    if args.command=='open-webui':return open_webui(root,record,args.service,args.browser_port)
    if args.command=='logs':
        result=subprocess.run(['journalctl','--user','-n','100','--no-pager',
                               '-u',unit(root,'astrbot'),'-u',unit(root,'napcat')],capture_output=True,text=True)
        return {'journal':result.stdout,'available':result.returncode==0}
    if not sys.stdin.isatty():raise c.Fault('needs_input','Menu requires an interactive terminal')
    commands=[['start'],['stop'],['restart'],['status'],['logs'],['open-webui','napcat'],
              ['open-webui','astrbot'],['connect-qq'],['health']]
    for index,port in ((5,args.napcat_browser_port),(6,args.astrbot_browser_port)):
        if port is not None:commands[index]+= ['--browser-port',str(port)]
    while True:
        for i,words in enumerate(commands,1):print(f'{i}. {" ".join(words)}',file=sys.stderr)
        try:value=input('Select 1–9, or Enter to exit: ').strip()
        except EOFError:break
        if not value:break
        if value in tuple(map(str,range(1,len(commands)+1))):
            words=commands[int(value)-1]
            if words==['logs']:
                print(manage(root,record,c.parser().parse_args(['logs']))['journal'],file=sys.stderr)
            else:
                c.main(['--root',str(root),*words])
    return {'menu':'closed'}


def run_napcat(target):
    webui_config(target)
    profile=target/'profile'
    profile.mkdir(mode=0o700,exist_ok=True)
    environment=dict(os.environ,HOME=str(profile),XDG_CONFIG_HOME=str(profile/'config'),
                     XDG_DATA_HOME=str(profile/'data'),XDG_CACHE_HOME=str(profile/'cache'))
    environment.pop('DBUS_SESSION_BUS_ADDRESS',None)
    for key in list(environment):
        if key.startswith('NAPCAT_'):environment.pop(key)
    argv=['dbus-run-session','--','xvfb-run','-a','env','LD_PRELOAD=./libnapcat_launcher.so',
          str(target/'qq/opt/QQ/qq'),'--no-sandbox']
    if environment.pop('FFA_SOFTWARE_RENDER',None)=='1':
        environment.update(LIBGL_ALWAYS_SOFTWARE='1',GALLIUM_DRIVER='llvmpipe')
        argv+=['--disable-gpu','--disable-gpu-compositing']
    os.chdir(target)
    os.execvpe(argv[0],argv,environment)


if __name__=='__main__':
    run_napcat(Path(sys.argv[1]).resolve())
