"""Seed the unchanged, audited default persona into an otherwise empty native DB."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);p.add_argument('--release',type=Path,required=True);a=p.parse_args()
root=a.instance.resolve();release=a.release.resolve()
os.environ['ASTRBOT_ROOT']=str(root)
os.environ['ASTRBOT_DASHBOARD_INITIAL_PASSWORD']=(root/'private/dashboard-password.txt').read_text().strip()
sys.path.insert(0,str(release/'astrbot'))
from astrbot.core.db.sqlite import SQLiteDatabase

async def main():
    db=SQLiteDatabase(str(root/'data/data_v4.db'));await db.initialize()
    try:
        if await db.get_persona_by_id('feiyu-v1'):raise ValueError('Default persona already exists; refusing overwrite')
        await db.insert_persona('feiyu-v1',(root/'persona/Persona_Master.md').read_text(encoding='utf-8-sig'),
             begin_dialogs=json.loads((root/'persona/Persona_Examples.json').read_text(encoding='utf-8-sig')),
             tools=['memorize_long_term_memory','web_search_deepseek','deepseek_fetch_web_page'],skills=[])
        assert not await db.get_conversations()
    finally:await db.engine.dispose()
    print('Unchanged default persona initialized; chat database empty')
asyncio.run(main())
