"""Read-only local NapCat login health; never logs account or login credentials."""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--port',type=int,default=6099);a=p.parse_args()
cfg=json.loads((a.root/'config/webui.json').read_text(encoding='utf-8-sig'))
base='http://127.0.0.1:'+str(a.port)+'/api'
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
result={k:status['data'].get(k) for k in ['isLogin','isOffline','coreReady']}
result['read_only']=True
print(json.dumps(result))
raise SystemExit(not (result['isLogin'] and result['coreReady'] and not result['isOffline']))
