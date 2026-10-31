"""Read-only checks. Never prints configuration values or secrets."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from instance import verify_release,verify_plugins
from data_integrity import sqlite_checks

p=argparse.ArgumentParser();p.add_argument('--instance',required=True,type=Path);a=p.parse_args();root=a.instance.resolve()
config=json.loads((root/'instance.json').read_text());verify_release(Path(config['release']))
verify_plugins(root,Path(config['release']))
owner=json.loads((root/'private/instance-identity.json').read_text())['owner']
access=json.loads((root/'private/p2c-access.json').read_text())
assert access['owner']==owner
cfg=json.loads((root/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
assert cfg['dashboard']['host']=='127.0.0.1'
assert not (root/'private/DIRTY').exists()
checks=sqlite_checks(root)
print(json.dumps({'release_integrity':'pass','identity':'pass','loopback':'pass','sqlite_checked':checks,'limits':'Model identity and QQ login require configured provider tests; not inferred from database integrity'}))
