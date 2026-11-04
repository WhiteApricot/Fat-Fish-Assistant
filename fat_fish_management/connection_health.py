"""Validate fresh private protocol evidence against the actual managed writer."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import time
from fat_fish_management.manage import process

def read_connection(instance):
    try:
        record=json.loads((instance/'private/runtime-health.json').read_text())
        writer=process(instance)
        deployment=json.loads((instance/'instance.json').read_text()).get('deployment_id')
        if record.get('schema')!=1 or writer is None or record.get('pid')!=writer.pid or record.get('deployment_id')!=deployment or not 0<=time.time()-record['time']<=20:
            return {'connected':False,'reason':'stale_or_unowned_evidence'}
        return {'connected':record.get('connected') is True,'reason':record.get('reason','unknown')}
    except Exception:return {'connected':False,'reason':'protocol_health_unavailable'}
