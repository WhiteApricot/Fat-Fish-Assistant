"""Read authenticated AstrBot status without displaying account IDs or secrets."""
import argparse
import json
from pathlib import Path
from dashboard_login import authenticate
from connection_health import read_connection

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);a=p.parse_args()
cfg=json.loads((a.instance/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
_,session,call=authenticate(a.instance)
data=call('/api/platform/stats',token=session['token'])
print(json.dumps({'authenticated_dashboard':'pass','platform_statistics_type':type(data).__name__,'configured_platforms':len(cfg['platform']),'onebot_connection':read_connection(a.instance)}))
