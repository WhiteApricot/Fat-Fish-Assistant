"""Native cold backup/restore of P2F task state; no chat service is launched."""
import os
import sys
import tempfile
import types
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'src'))
from instance import initialize,backup,restore,verify_release,verify_plugins
from p2f.reminders import ReminderStore
from p2c.policy import ScopedPolicy
release=Path(os.environ['FEIYU_TEST_RELEASE'])
with tempfile.TemporaryDirectory(prefix='p2f-deployment-') as temp:
    root=Path(temp);first=root/'instance';second=root/'restored';snapshot=root/'snapshot'
    initialize(first,'100',release,seed_persona=False)
    for name in ['fat_fish_forwards','fat_fish_reminders','fat_fish_guard']:
        assert (first/'data/plugins'/name/'main.py').is_file()
    access=first/'private/p2c-access.json';cfg=json.loads(access.read_text());cfg['global_admins']=['200'];access.write_text(json.dumps(cfg))
    folder=first/'data/plugin_data/fat_fish_reminders';folder.mkdir(parents=True)
    store=ReminderStore(folder/'reminders.sqlite3')
    policy=ScopedPolicy('100',{},global_admins={'200'})
    gateway=types.SimpleNamespace(load=lambda:(cfg,policy),epoch=lambda c,g:0)
    event=types.SimpleNamespace(get_sender_id=lambda:'100',get_group_id=lambda:'',get_platform_id=lambda:'synthetic',get_messages=lambda:[],get_self_id=lambda:'999')
    assert '已创建' in store.command(gateway,event,['add','2027-01-01 20:00提醒我合成迁移任务'])
    expected=store.rows()
    backup(first,snapshot);restore(snapshot,second,release)
    verify_release(release);verify_plugins(second,release)
    assert ReminderStore(second/'data/plugin_data/fat_fish_reminders/reminders.sqlite3').rows()==expected
    assert json.loads((second/'private/p2c-access.json').read_text())['global_admins']==['200']
    assert not (second/'private/RUNNING').exists()
    print('P2F install, plugin materialization, cold backup, restore, authority and reminder persistence: PASS')
