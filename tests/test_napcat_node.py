"""Real pinned Node/QQ pre-login smoke; never scans, logs in, or sends messages."""
import argparse,json,os,socket,subprocess,sys,time,urllib.request
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'scripts'))
import controller as c
from instance import protect_boundary

p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--cache',required=True,type=Path);a=p.parse_args()
root=a.output.resolve();root.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local/ux-validation')
if root.exists():raise ValueError('Use a fresh validation root')
c.owned_root(root,True);protect_boundary(root)
runtime=root/'instance';(runtime/'private').mkdir(parents=True);(runtime/'data').mkdir()
old=root/'services/legacy';(old/'config').mkdir(parents=True)
with socket.socket() as listener:
    listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
c.atomic(old/'config/webui.json',{'host':'127.0.0.1','port':port,'token':'synthetic-node-test-token','autoLoginAccount':'','disableWebUI':False,'accessControlMode':'whitelist','ipWhitelist':['127.0.0.1','::1']})
record={'schema_version':1,'instance':'instance','chat_python':str(Path(sys.executable)),'qq_executable':'C:/synthetic-unused/QQ.exe','components':{'napcat':{'installed':True,'ownership':'managed','path':'services/legacy','version':'napcat-shell-v4.18.41'}}}
c.atomic(root/'installation.json',record)
args=c.parser().parse_args(['--root',str(root),'prepare-napcat','--cache',str(a.cache),'--offline','--accept-external-licenses'])
def prepare():
    run=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT/'Fat-Fish-Assistant.ps1'),'-Root',str(root),'-Python',sys.executable,'-Json','-NonInteractive','prepare-napcat','--cache',str(a.cache),'--offline','--accept-external-licenses'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=300)
    (root/('wrapper-'+str(time.time_ns())+'.log')).write_text(run.stdout+'\n'+run.stderr,encoding='utf-8')
    assert run.returncode==0,run.stdout+run.stderr
    return json.loads(run.stdout)['data']
result=prepare();record=c.state(root)
assert result['prepared'] and not result['system_qq_modified'] and 'qq_executable' not in record
assert (old/'config/webui.json').read_bytes()==(root/record['components']['napcat']['path']/'config/webui.json').read_bytes()
prepare()
flags={'official_bundle_hash':True,'legacy_config_retained':True,'repeat_prepare':True,'system_qq_selected':False,'real_login':False,'real_message':False}
try:
    c.operate(root,record,'start',['napcat'])
    for attempt in range(60):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=2) as response:
                assert response.status==200
            break
        except OSError:
            if attempt==59:raise
            time.sleep(1)
    assert c.component_status(root,record,'napcat')['running']
    flags['real_native_webui_http_200']=True
    login_url=f'http://127.0.0.1:{port}/webui/web_login?token=synthetic-node-test-token'
    with urllib.request.urlopen(login_url,timeout=5) as response:
        assert response.status==200 and response.geturl()==login_url
        assert b'id="root"' in response.read()
    flags['direct_login_http_200_without_query_losing_redirect']=True
    for attempt in range(30):
        login=c.dispatch(c.parser().parse_args(['--root',str(root),'component','napcat','login-status']))
        if login.get('qr_available'):
            flags['real_qrcode_ready_without_login']=True
            break
        if attempt==29:raise AssertionError('Native QQ never provided a login QR code: '+str({k:login.get(k) for k in ['loginPhase','isLogin','login_error_present']}))
        time.sleep(1)
    duplicate=c.operate(root,record,'start',['napcat'])
    assert duplicate['operations'][0]['already_in_requested_state']
    flags['duplicate_start_reused']=True
finally:
    c.operate(root,record,'stop',['napcat'])
assert not c.component_status(root,record,'napcat')['running'];flags['owned_stop']=True
prepare();flags['receipt_valid_after_real_run']=True
entry=root/'services/napcat-node-v4.18.41/index.js';entry.write_bytes(entry.read_bytes()+b'\n// synthetic tamper\n')
try:c.operate(root,record,'start',['napcat'])
except c.Fault:flags['tampered_entry_refused']=True
else:raise AssertionError('Tampered runtime started')
c.atomic(root/'results.json',flags);print(json.dumps(flags))
