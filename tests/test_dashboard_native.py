"""Actual isolated AstrBot HTTP login, password change, secure update and restart."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
WORK=Path(os.environ['FFA_FEATURE_WORKSPACE']).resolve()
WORK.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local')
ROOT=WORK/'dashboard-instance'
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE']).resolve()
sys.path[:0]=[str(PROJECT/'scripts'),str(PROJECT/'src')]
from instance import initialize
from dashboard_login import authenticate,login_url,update_password
from ffa_local_credentials import load_password

if ROOT.exists():raise ValueError('Choose a fresh dashboard test workspace')
initialize(ROOT,'100',RELEASE)
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
path=ROOT/'data/cmd_config.json';cfg=json.loads(path.read_text(encoding='utf-8-sig'))
cfg['dashboard']['port']=port
cfg['log_level']='WARNING'
path.write_text(json.dumps(cfg),encoding='utf-8')
assert not (ROOT/'private/dashboard-password.txt').exists()
initial=load_password(ROOT)
assert initial and initial.encode() not in (ROOT/'private/dashboard-credential.dpapi').read_bytes()

def start():
    stream=(WORK/'dashboard-startup.log').open('a',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-X','utf8','-B',str(PROJECT/'scripts/run_chat.py'),'--instance',str(ROOT)],stdout=stream,stderr=stream,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('Isolated AstrBot exited during startup; see local test log')
            try:return process,stream,authenticate(ROOT)
            except Exception:time.sleep(.5)
        raise RuntimeError('Isolated AstrBot startup timed out')
    except BaseException:
        (ROOT/'private/STOP').write_text('test stopped')
        try:process.wait(45)
        except subprocess.TimeoutExpired:process.terminate();process.wait(15)
        stream.close();raise

def stop(process,stream):
    (ROOT/'private/STOP').write_text('test stopped')
    try:process.wait(60)
    except subprocess.TimeoutExpired:process.terminate();process.wait(15);raise
    finally:stream.close()
    assert (ROOT/'private/STOPPED').exists(),'Native shutdown must complete'

process,stream,(_,session,call)=start()
try:
    first=login_url(ROOT)
    state=json.loads(urllib.parse.unquote(urllib.parse.urlsplit(first).fragment[10:]))
    assert state['user']=='astrbot' and state['token']
    new_password='SyntheticRotation456A'
    call('/api/auth/account/edit',{'password':initial,'new_password':new_password,'confirm_password':new_password},session['token'])
    saved=(ROOT/'private/dashboard-credential.dpapi').read_bytes()
    try:authenticate(ROOT)
    except Exception:pass
    else:raise AssertionError('Old credential unexpectedly remained valid')
    try:update_password(ROOT,'IncorrectSynthetic789A')
    except Exception:pass
    else:raise AssertionError('Invalid update unexpectedly succeeded')
    assert saved==(ROOT/'private/dashboard-credential.dpapi').read_bytes()
    update_password(ROOT,new_password)
    assert load_password(ROOT)==new_password
    assert '#ffa-login=' in login_url(ROOT)
finally:stop(process,stream)

process,stream,_=start()
try:
    assert '#ffa-login=' in login_url(ROOT)
    assert load_password(ROOT)==new_password
finally:stop(process,stream)
logs=(WORK/'dashboard-startup.log').read_text(encoding='utf-8',errors='replace')
assert initial not in logs and new_password not in logs
result={'real_initial_login':True,'username':'astrbot','password_changed_via_native_auth':True,'invalid_update_preserves_saved_credential':True,'secure_one_time_update':True,'restart_uses_changed_password':True,'plaintext_password_file':False,'authentication_bypassed':False}
(WORK/'dashboard-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('NATIVE_DASHBOARD_ROTATION_RESTART_PASS')
