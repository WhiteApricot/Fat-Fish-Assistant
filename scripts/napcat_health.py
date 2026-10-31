"""Read-only local NapCat login health; never logs account or login credentials."""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--port',type=int);p.add_argument('--allow-pending',action='store_true');a=p.parse_args()
cfg=json.loads((a.root/'config/webui.json').read_text(encoding='utf-8-sig'))
if cfg.get('host')!='127.0.0.1':raise ValueError('Managed NapCat WebUI must bind loopback')
base='http://127.0.0.1:'+str(a.port or cfg.get('port',6099))+'/api'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def call(path,body,token=None):
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    request=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers=headers)
    with opener.open(request,timeout=15) as response:return json.load(response)
login=call('/auth/login',{'hash':hashlib.sha256((cfg['token']+'.napcat').encode()).hexdigest()})
assert login['code']==0
status=call('/QQLogin/CheckLoginStatus',{},login['data']['Credential'])
assert status['code']==0
result={k:status['data'].get(k) for k in ['isLogin','isOffline','coreReady','loginPhase','qrLoginAccepted']}
result['qr_available']=bool(status['data'].get('qrcodeurl'))
result['login_error_present']=bool(status['data'].get('loginError'))
result['read_only']=True
print(json.dumps(result))
raise SystemExit(not a.allow_pending and not (result['isLogin'] and result['coreReady'] and not result['isOffline']))
