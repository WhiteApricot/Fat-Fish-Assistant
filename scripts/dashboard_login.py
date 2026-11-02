"""Create a local native-view login URL; never log passwords/session tokens."""
import argparse,json,time,urllib.parse,urllib.request
from pathlib import Path
import sys,getpass,warnings
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ffa_local_credentials import load_password,save_password


def authenticate(instance, password=None):
    cfg=json.loads((instance/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    dashboard=cfg['dashboard']
    if dashboard.get('host')!='127.0.0.1':raise ValueError('Local dashboard required')
    port=int(dashboard['port'])
    if not 1<=port<=65535:raise ValueError('Invalid local dashboard port')
    base=f'http://127.0.0.1:{port}'
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise ValueError('Auth redirects are forbidden')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def call(path,body=None,token=None):
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        request=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with opener.open(request,timeout=15) as response:
            if urllib.parse.urlsplit(response.url).netloc!=urllib.parse.urlsplit(base).netloc:raise ValueError('Unexpected auth redirect')
            result=json.load(response)
        if result.get('status')!='ok':raise ValueError('Manual dashboard login required')
        return result.get('data') or {}
    username='astrbot'
    if password is None:password=load_password(instance)
    session=call('/api/auth/login',{'username':username,'password':password})
    token=session.get('token')
    if not token or session.get('totp_required'):
        raise ValueError('Manual dashboard login required')
    return base,session,call


def login_url(instance):
    base,session,call=authenticate(instance)
    username='astrbot';token=session['token']
    page=call('/api/v1/plugins/fat_fish_health/views/launcher',token=token)
    path=page.get('content_path','');parsed=urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith('/api/v1/plugins/fat_fish_health/views/launcher/_t/') or '..' in parsed.path.split('/'):
        raise ValueError('Unexpected launcher view path')
    # Fragment contents never enter server access logs or HTTP referrers.
    state={'token':token,'user':username,'expires':int(time.time())+60}
    state.update({x:bool(session.get(x)) for x in ['password_upgrade_required','change_pwd_hint','md5_pwd_hint']})
    state=urllib.parse.quote(json.dumps(state,separators=(',',':')),safe='')
    return base+path+'#ffa-login='+state


def update_password(instance,password):
    authenticate(instance,password)
    save_password(instance,password)
    (instance/'private/dashboard-password.txt').unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--instance',required=True,type=Path);parser.add_argument('--update',action='store_true');args=parser.parse_args()
    try:
        if args.update:
            warnings.simplefilter('error',getpass.GetPassWarning)
            password=getpass.getpass('AstrBot Dashboard 当前密码（输入隐藏）: ')
            update_password(args.instance,password)
            password=None
        print(json.dumps({'ok':True,'data':{'url':login_url(args.instance)},'errors':[]}))
    except Exception:
        print(json.dumps({'ok':False,'data':{},'errors':[{'message':'AstrBot 自动登录未完成：请运行本机凭据更新流程；如启用二次验证，请在 Dashboard 完成验证。','hint':''}]}))
        raise SystemExit(1)
