"""Read authenticated AstrBot status without displaying account IDs or secrets."""
import argparse
import json
import urllib.request
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);a=p.parse_args()
cfg=json.loads((a.instance/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
base='http://127.0.0.1:'+str(cfg['dashboard']['port'])
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def call(path,body=None,token=None):
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    with opener.open(req,timeout=15) as response:return json.load(response)
password=(a.instance/'private/dashboard-password.txt').read_text().strip()
login=call('/api/auth/login',{'username':cfg['dashboard']['username'],'password':password})
assert login.get('status')=='ok'
result=call('/api/platform/stats',token=login['data']['token'])
assert result.get('status')=='ok'
data=result.get('data')
from connection_health import read_connection
print(json.dumps({'authenticated_dashboard':'pass','platform_statistics_type':type(data).__name__,'configured_platforms':len(cfg['platform']),'onebot_connection':read_connection(a.instance)}))
