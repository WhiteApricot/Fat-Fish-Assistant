"""Read-only checks. Never prints configuration values or secrets."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from fat_fish_management.instance import require_verified_instance,verify_release,verify_plugins
from fat_fish_management.data_integrity import sqlite_checks

p=argparse.ArgumentParser();p.add_argument('--instance',required=True,type=Path);a=p.parse_args();root=a.instance.resolve()
config=json.loads((root/'instance.json').read_text());verify_release(Path(config['release']))
verify_plugins(root,Path(config['release']))
require_verified_instance(root)
cfg=json.loads((root/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
assert cfg['dashboard']['host']=='127.0.0.1'
assert not (root/'private/DIRTY').exists()
checks=sqlite_checks(root)
print(json.dumps({'release_integrity':'pass','identity':'pass','loopback':'pass','sqlite_checked':checks,'limits':'Model identity and QQ login require configured provider tests; not inferred from database integrity'}))
