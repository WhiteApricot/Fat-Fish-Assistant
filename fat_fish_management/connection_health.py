"""Validate fresh private protocol evidence against the actual managed writer."""
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
from pathlib import Path
import time

import psutil

from fat_fish_management.manage import process

FLAGS = ('astrbot_connected', 'onebot_ready', 'qq_online', 'identity_matches', 'core_loaded')


def read_connection(instance):
    try:
        record = json.loads((instance / 'private/runtime-health.json').read_text(encoding='utf-8'))
        writer = process(instance)
        deployment = json.loads((instance / 'instance.json').read_text(encoding='utf-8')).get('deployment_id')
        if (record.get('schema') not in (1, 2) or writer is None or record.get('pid') != writer.pid
                or not deployment or record.get('deployment_id') != deployment
                or not 0 <= time.time() - record['time'] <= 20):
            return {'connected': False, 'reason': 'stale_or_unowned_evidence'}
        result = {key: record[key] for key in FLAGS if type(record.get(key)) is bool}
        connected = record.get('connected') is True
        if record['schema'] == 2:
            connected = connected and all(result.get(key) is True for key in FLAGS[:4])
        result.update(connected=connected, reason=record.get('reason', 'unknown'))
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, psutil.Error):
        return {'connected': False, 'reason': 'protocol_health_unavailable'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(read_connection(args.instance)))
