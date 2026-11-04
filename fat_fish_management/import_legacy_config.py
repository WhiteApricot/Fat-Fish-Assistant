"""Import private configuration and personas only, with empty chats and memory."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
from contextlib import closing
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from fat_fish_management.instance import initialize

p=argparse.ArgumentParser();p.add_argument('--legacy-runtime',type=Path,required=True);p.add_argument('--destination',type=Path,required=True);p.add_argument('--release',type=Path,required=True);p.add_argument('--persona-assets',type=Path)
a=p.parse_args();old=a.legacy_runtime.resolve();new=a.destination.resolve();release=a.release.resolve()
access=json.loads((old/'private/p2c-access.json').read_text(encoding='utf-8-sig'))
initialize(new,access['owner'],release,seed_persona=False)
def relocate(value):
    if isinstance(value,str):return value.replace(str(old),str(new)).replace(old.as_posix(),new.as_posix())
    if isinstance(value,list):return [relocate(x) for x in value]
    if isinstance(value,dict):return {key:relocate(item) for key,item in value.items()}
    return value
(new/'private/p2c-access.json').write_text(json.dumps(access,ensure_ascii=False),encoding='utf-8')
config=relocate(json.loads((old/'data/cmd_config.json').read_text(encoding='utf-8-sig')))
config['dashboard']['host']='127.0.0.1'
(new/'data/cmd_config.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
if (old/'data/config').exists():
    (new/'data/config').mkdir(exist_ok=True)
    for path in (old/'data/config').glob('*.json'):
        (new/'data/config'/path.name).write_text(json.dumps(relocate(json.loads(path.read_text(encoding='utf-8-sig'))),ensure_ascii=False),encoding='utf-8')
for pattern in ['*key*.txt','dashboard-credential.dpapi']:
    for path in (old/'private').glob(pattern):shutil.copy2(path,new/'private'/path.name)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fat_fish.core.local_credentials import load_password,save_password
if (old/'private/dashboard-password.txt').exists():save_password(new,(old/'private/dashboard-password.txt').read_text(encoding='utf-8-sig').strip())
if a.persona_assets:
    for name in ['Persona_Master.md','Persona_Examples.json']:shutil.copy2(a.persona_assets/name,new/'persona'/name)
os.environ.update(ASTRBOT_ROOT=str(new),FEIYU_RUNTIME_ROOT=str(new),FEIYU_SOURCE_ROOT=str(release/'src'),FEIYU_PERSONA_ROOT=str(new/'persona'))
sys.path[:0]=[str(release/'astrbot'),str(release/'src')]
from astrbot.core.db.sqlite import SQLiteDatabase

async def import_personas():
    with closing(sqlite3.connect((old/'data/data_v4.db').as_uri()+'?mode=ro',uri=True)) as source:
        source.row_factory=sqlite3.Row
        personas=[dict(row) for row in source.execute('SELECT * FROM personas')]
    target=SQLiteDatabase(str(new/'data/data_v4.db'));await target.initialize()
    try:
        for row in personas:
            values={key:row[key] for key in ['persona_id','system_prompt','custom_error_message','sort_order'] if key in row}
            for key in ['begin_dialogs','tools','skills']:
                values[key]=json.loads(row[key]) if isinstance(row.get(key),str) else row.get(key)
            await target.insert_persona(**values)
        assert not await target.get_conversations()
    finally:await target.engine.dispose()
    print(json.dumps({'personas_preserved':len(personas),'chat_history_imported':False,'memory_imported':False,'owner_changed':False,'groups_expanded':False}))

asyncio.run(import_personas())
