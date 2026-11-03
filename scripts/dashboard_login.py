"""Create a local native-view login URL; never log passwords/session tokens."""
import argparse,json,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ffa_local_credentials import load_password,save_password


class DashboardLoginRequired(ValueError):
    """Local Dashboard authentication requires user input."""


class DashboardTwoFactorRequired(DashboardLoginRequired):
    def __init__(self, url):
        super().__init__('Dashboard requires two-factor authentication')
        self.url=url


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
        if result.get('status')!='ok':
            if path=='/api/auth/login':
                raise DashboardLoginRequired('Dashboard rejected the credentials')
            raise ValueError('Dashboard launcher view unavailable')
        return result.get('data') or {}
    username=dashboard.get('username','astrbot')
    if password is None:
        try:
            password=load_password(instance)
        except (ValueError,RuntimeError,OSError) as exc:
            raise DashboardLoginRequired('Saved dashboard credentials need updating') from exc
    try:
        session=call('/api/auth/login',{'username':username,'password':password})
    except urllib.error.HTTPError as exc:
        if exc.code==401:
            with exc:
                try:
                    challenge=json.load(exc)
                except (ValueError,OSError):
                    challenge={}
            data=challenge.get('data') if isinstance(challenge,dict) else None
            if isinstance(data,dict) and data.get('totp_required') is True:
                raise DashboardTwoFactorRequired(base) from exc
            raise DashboardLoginRequired('Dashboard rejected the credentials') from exc
        raise
    token=session.get('token')
    if session.get('totp_required') is True:
        raise DashboardTwoFactorRequired(base)
    if not token:
        raise DashboardLoginRequired('Manual dashboard login required')
    return base,{**session,'username':username},call


def login_url(instance, authenticated=None):
    base,session,call=authenticate(instance) if authenticated is None else authenticated
    username=session.get('username','astrbot');token=session['token']
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
    try:
        result=authenticate(instance,password)
    except DashboardTwoFactorRequired as challenge:
        # This response is issued only after the password was verified.
        result=challenge
    save_password(instance,password)
    (instance/'private/dashboard-password.txt').unlink(missing_ok=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--instance',required=True,type=Path)
    parser.add_argument('--update',action='store_true')
    args=parser.parse_args()
    try:
        authenticated=None
        if args.update:
            payload=json.load(sys.stdin)
            password=payload.pop('password',None)
            if not isinstance(password,str) or not password:
                raise DashboardLoginRequired('Missing password')
            authenticated=update_password(args.instance,password)
            password=None
        if isinstance(authenticated,DashboardTwoFactorRequired):
            raise authenticated
        try:
            url=login_url(args.instance,authenticated)
            data={'url':url,'mode':'automatic','saved':args.update}
        except DashboardLoginRequired:
            raise
        except Exception:
            if authenticated is None:
                raise
            data={'url':authenticated[0],'mode':'manual','saved':True}
    except DashboardTwoFactorRequired as challenge:
        data={'url':challenge.url,'mode':'two_factor','saved':args.update}
    except DashboardLoginRequired:
        if not args.update:
            data={'mode':'credentials_required','saved':False}
        else:
            print(json.dumps({'ok':False,'data':{},'errors':[{'message':'Dashboard 未接受此账号的密码。','hint':''}]},ensure_ascii=True))
            raise SystemExit(1)
    except Exception:
        print(json.dumps({'ok':False,'data':{},'errors':[{'message':'AstrBot 登录或凭据保存未完成。','hint':''}]},ensure_ascii=True))
        raise SystemExit(1)
    print(json.dumps({'ok':True,'data':data,'errors':[]},ensure_ascii=True))
