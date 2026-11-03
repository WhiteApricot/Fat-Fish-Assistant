"""Read authenticated AstrBot status without displaying account IDs or secrets."""
import argparse
import json
from pathlib import Path
from dashboard_login import DashboardLoginRequired, authenticate
from connection_health import read_connection

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);p.add_argument('--allow-login-required',action='store_true');a=p.parse_args()
cfg=json.loads((a.instance/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
try:
    _,session,call=authenticate(a.instance)
except DashboardLoginRequired:
    if not a.allow_login_required:
        raise
    dashboard={'authenticated_dashboard':'login_required'}
else:
    data=call('/api/platform/stats',token=session['token'])
    dashboard={'authenticated_dashboard':'pass','platform_statistics_type':type(data).__name__}
dashboard.update(configured_platforms=len(cfg['platform']),onebot_connection=read_connection(a.instance))
print(json.dumps(dashboard))
