"""Reproduce offline P2F acceptance against an already built, pinned release."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--python',type=Path,default=Path(sys.executable))
    args=parser.parse_args();project=Path(__file__).resolve().parents[1]
    output=args.output.resolve()
    if output==project or project in output.parents:raise ValueError('Test artifacts must stay outside the formal source tree')
    output.mkdir(parents=True,exist_ok=True);(output/'temp').mkdir(exist_ok=True)
    env=dict(os.environ,FEIYU_TEST_RELEASE=str(args.release.resolve()),PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',TEMP=str(output/'temp'),TMP=str(output/'temp'))
    env.pop('DEEPSEEK_API_KEY',None);env.pop('FFA_TEST_KEY_FILE',None)
    names=['test_p2f_r1.py','test_p2f_permissions.py','test_p2f.py','Verify-P2C-Scoped.py','test_p2f_native.py','test_p2f_loading.py','test_p2f_deployment.py','Verify-P2C-NativeBoundary.py','Verify-P2C-Pipeline.py','Verify-P2C-Cancellation.py','Verify-P2C-NativeGroups.py','Verify-P2C-GroupQuotes.py','test_group_media.py','test_feature_repairs.py']
    results=[]
    for name in names:
        start=time.monotonic()
        with (output/(name+'.log')).open('w',encoding='utf-8') as log:
            try:code=subprocess.run([str(args.python),'-u',str(project/'tests/test_worker.py'),str(project/'tests'/name)],env=env,cwd=project,stdout=log,stderr=log,timeout=240).returncode
            except subprocess.TimeoutExpired:code=124
        results.append({'test':name,'exit':code,'seconds':round(time.monotonic()-start,2)})
        (output/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(name,code,flush=True)
    return int(any(r['exit'] for r in results))

if __name__=='__main__':raise SystemExit(main())
