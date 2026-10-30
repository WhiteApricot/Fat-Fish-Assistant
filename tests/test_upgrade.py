"""Exercise activation rollback after failure and reject corrupt releases."""
import importlib.util
import json
import sys
import tempfile
import subprocess
from pathlib import Path

project=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(project/'scripts'))
from instance import initialize,materialize,backup
from manage import activate,recover_upgrade,start

release=Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='feiyu-upgrade-') as tmp:
    root=Path(tmp);instance=root/'instance';candidate=root/'release-next'
    initialize(instance,'100',release)
    materialize(candidate,offline=True)
    pointer=instance/'instance.json';before=pointer.read_bytes()
    before_database=(instance/'data/data_v4.db').read_bytes()
    plugin=instance/'data/plugins/astrbot_plugin_livingmemory/main.py';before_plugin=plugin.read_bytes()
    try:activate(instance,candidate,True)
    except RuntimeError:pass
    assert pointer.read_bytes()==before
    assert plugin.read_bytes()==before_plugin
    activate(instance,candidate)
    previous=json.loads((instance/'private/previous-release.json').read_text())
    assert previous['release']==str(release)
    assert json.loads(pointer.read_text())['release']==str(candidate)
    activate(instance,Path(previous['release']))
    assert json.loads(pointer.read_text())['release']==str(release)
    crash=subprocess.run([sys.executable,str(project/'scripts/manage.py'),'upgrade','--instance',str(instance),'--release',str(candidate),'--simulate-crash'],capture_output=True,timeout=90)
    assert crash.returncode==88,crash.stderr
    assert (instance/'private/upgrade-journal.json').exists()
    try:backup(instance,root/'interrupted-backup')
    except ValueError:pass
    else:raise AssertionError('Interrupted upgrade allowed snapshot')
    assert not (root/'interrupted-backup').exists()
    try:start(instance,Path(sys.executable))
    except ValueError:pass
    else:raise AssertionError('Interrupted upgrade allowed startup')
    recover_upgrade(instance)
    assert json.loads(pointer.read_text())['release']==str(release)
    assert (instance/'data/data_v4.db').read_bytes()==before_database
    assert plugin.read_bytes()==before_plugin
    assert not (instance/'private/upgrade-journal.json').exists()
    source=candidate/'src/p2c/scoped_gateway.py';source.write_text('corrupt fixture')
    try:activate(instance,candidate)
    except ValueError:pass
    else:raise AssertionError('Corrupt release accepted')
    assert json.loads(pointer.read_text())['release']==str(release)
print('UPGRADE_RESULT='+json.dumps({'post_activation_failure_reverts_pointer':True,'successful_activation':True,'explicit_rollback':True,'corrupt_release_refused':True,'process_crash_fail_closed':True,'process_crash_recovery':True}))
