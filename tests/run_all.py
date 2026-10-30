"""Execute synthetic regression, migration and deployment rollback checks."""
import argparse
import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--include-migration',action='store_true',help='Explicit future VM/data-migration validation only');p.add_argument('--timeout',type=int,default=600,help='Per test process, including cold Windows imports and source assembly');a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=True)
project=Path(__file__).resolve().parents[1]
def source_state():
    rows={}
    for name in ['ffa.py','Fat-Fish-Assistant.ps1']:
        rows[name]=hashlib.sha256((project/name).read_bytes()).hexdigest()
    for name in ['scripts','src','tests','examples','manifests','patches']:
        for path in (project/name).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                with path.open('rb') as stream:rows[path.relative_to(project).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()
    return rows
before=source_state()
env=dict(os.environ,FEIYU_TEST_RELEASE=str(a.release.resolve()),FFA_TEST_RELEASE=str(a.release.resolve()))
env.pop('PYTHONPATH',None)
tests=['test_decision_adapter.py','test_group_media.py','test_participation.py','Verify-P2C-Gateway.py','Verify-P2C-Scoped.py','Verify-P2C.py',
       'Verify-P2C-NativeBoundary.py','Verify-P2C-Pipeline.py','Verify-P2C-Cancellation.py','Verify-P2C-NativeGroups.py','Verify-P2C-GroupQuotes.py',
       'test_lock.py','test_upgrade.py','test_management.py','test_dependencies.py','test_service_health.py','test_audit.py','test_chat_configuration.py','test_onebot_configuration.py']
if a.include_migration:tests.append('test_migration.py')
rows=[]
for name in tests:
    args=[sys.executable,str(Path(__file__).with_name(name))]
    if name in {'test_migration.py','test_upgrade.py'}:args.append(str(a.release.resolve()))
    with (a.output/(name+'.log')).open('wb') as log:
        try:run=subprocess.run(args,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=a.timeout);code=run.returncode
        except subprocess.TimeoutExpired:code=124
    rows.append({'test':name,'exit_code':code})
    (a.output/'results.json').write_text(json.dumps(rows,indent=2))
    print(name,code,flush=True)
unchanged=before==source_state()
(a.output/'source-state.json').write_text(json.dumps({'unchanged_during_run':unchanged,'files':before},indent=2))
print('Source unchanged during run:',unchanged,flush=True)
raise SystemExit(not unchanged or any(row['exit_code'] for row in rows))
