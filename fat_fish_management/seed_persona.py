"""Seed the default persona without fictional dialogue history into an otherwise empty native DB."""
import argparse
import asyncio
import os
import sys
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);p.add_argument('--release',type=Path,required=True);a=p.parse_args()
root=a.instance.resolve();release=a.release.resolve()
os.environ['ASTRBOT_ROOT']=str(root)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fat_fish.core.local_credentials import install_capture,capture_generated
install_capture(root)
sys.path.insert(0,str(release/'astrbot'))
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core import astrbot_config
capture_generated(root,astrbot_config)

async def main():
    db=SQLiteDatabase(str(root/'data/data_v4.db'));await db.initialize()
    try:
        if await db.get_persona_by_id('feiyu-v1'):raise ValueError('Default persona already exists; refusing overwrite')
        # Style examples are reference assets, never pretend user/assistant history.
        await db.insert_persona('feiyu-v1',(root/'persona/Persona_Master.md').read_text(encoding='utf-8-sig'),
             begin_dialogs=[],
             tools=['memorize_long_term_memory','web_search_deepseek','deepseek_fetch_web_page'],skills=[])
        assert not await db.get_conversations()
    finally:await db.engine.dispose()
    print('Default persona initialized without sample history; chat database empty')
asyncio.run(main())
