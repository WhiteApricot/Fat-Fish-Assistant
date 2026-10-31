"""Real Chat with a synthetic OneBot peer, then migration rejection boundaries.

No QQ process, real account, GUI automation, or outbound message is used.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'scripts'))
import controller as c
import migration
from migration_package import pack,unpack
from cryptography.exceptions import InvalidTag

p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
root=a.root.resolve();output=a.output.resolve()
for path in [root,output]:path.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local/ux-validation')
if output.exists():raise ValueError('Use new validation output')
output.mkdir();record=c.state(root);runtime=c.instance(root,record);python=c.chat_python(root,record)
if c.read(runtime/'private/instance-identity.json')['owner']!='100' or record['components'].get('napcat',{}).get('installed'):raise ValueError('Only a synthetic non-QQ installation may be used')
os.environ.update(TEMP=str(output),TMP=str(output),PYTHONDONTWRITEBYTECODE='1')
results={};number=0
def mark(name,value=True):
    results[name]=value;(output/'results.json').write_text(json.dumps(results,indent=2));print(name,value,flush=True)
def api(*args,payload=None,success=True):
    global number
    number+=1
    proc=subprocess.run([str(python),'-B',str(PROJECT/'ffa.py'),'--root',str(root),*map(str,args)],input=json.dumps(payload) if payload is not None else None,capture_output=True,text=True,env=c.clean_environment(),timeout=900)
    (output/('api-'+str(number)+'.log')).write_text(proc.stdout+'\n'+proc.stderr,encoding='utf-8')
    result=json.loads(proc.stdout)
    if success and not result['ok']:raise AssertionError(result['errors'])
    return result

identity=runtime/'private/bot-identity.json'
if identity.exists():raise ValueError('Do not replace an existing Bot identity even in this fixture')
c.atomic(identity,{'bot_id':'555'})
try:
    api('daily-start')
    cfg=c.read(runtime/'data/cmd_config.json');platform=next(p for p in cfg['platform'] if p.get('id')=='feiyu-onebot')
    token=(runtime/'private/onebot-token.txt').read_text().strip();actions=[]
    async def protocol():
        from websockets.asyncio.client import connect
        connected=asyncio.Event();disconnect=asyncio.Event()
        async def peer():
            async with connect(f"ws://127.0.0.1:{platform['ws_reverse_port']}/ws",additional_headers={'Authorization':'Bearer '+token,'X-Self-ID':'555','X-Client-Role':'Universal'}) as ws:
                connected.set()
                async for message in ws:
                    request=json.loads(message);action=request.get('action');actions.append(action)
                    data={'online':True,'good':True} if action=='get_status' else {'user_id':555,'nickname':'synthetic'} if action=='get_login_info' else {}
                    await ws.send(json.dumps({'status':'ok','retcode':0,'data':data,'echo':request.get('echo')}))
                    if disconnect.is_set():break
        task=asyncio.create_task(peer());await asyncio.wait_for(connected.wait(),20)
        ready=False
        for _ in range(30):
            await asyncio.sleep(1)
            health=runtime/'private/runtime-health.json'
            if health.exists() and c.read(health).get('connected') is True:ready=True;break
            if task.done():await task
        mark('actual_chat_synthetic_reverse_ws_confirmed',ready)
        mark('only_readonly_protocol_actions',bool(actions) and all(action in {'get_status','get_login_info'} for action in actions))
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await asyncio.sleep(7)
        mark('disconnection_clears_protocol_health',c.read(runtime/'private/runtime-health.json').get('connected') is False)
    asyncio.run(protocol())
finally:
    api('stop')
    identity.unlink(missing_ok=True)

package=output/'synthetic-final.ffa.zip';password='synthetic-final-migration-password'
exported=api('migration-export','--destination',package,payload={'password':password})['data']
mark('final_export_includes_version_plan',exported['encrypted'])
decoded=output/'decoded';unpack(package,decoded,password)
manifest,plan=migration.validate_snapshot(decoded)
mark('actual_dependency_versions_and_component_state',bool(plan['dependencies']['chat']) and bool(plan['component_state']))
mark('plugin_directory_config_included',any(row['path']=='data/plugins/meme_manager/image_host/config.json' for row in manifest['files']))

def reject(name,archive,passphrase,exception):
    target=c.owned_root(output/name,True)
    try:migration.import_package(target,archive,passphrase,True)
    except exception:mark(name,not (target/'instance').exists())
    else:raise AssertionError('Unsafe import accepted: '+name)

reject('wrong_password_no_instance',package,'synthetic-wrong-password',InvalidTag)
with zipfile.ZipFile(package) as z:data=bytearray(z.read('snapshot.aesgcm'))
data[-1]^=1;damaged=output/'damaged.ffa.zip'
with zipfile.ZipFile(damaged,'w') as z:z.writestr('snapshot.aesgcm',data)
reject('damaged_package_no_instance',damaged,password,InvalidTag)
plan['locks']['sources.json']='0'*64;c.atomic(decoded/'installation-plan.json',plan)
incompatible=output/'incompatible.ffa.zip';pack(decoded,incompatible,password)
reject('incompatible_version_no_instance',incompatible,password,ValueError)
plan['locks']['sources.json']=c.digest(c.PROJECT/'manifests/sources.json');c.atomic(decoded/'installation-plan.json',plan)
file=decoded/'payload/data/plugin_data/custom/settings.json';file.write_text('{"synthetic":"changed without updating manifest"}')
invalid_sha=output/'bad-sha.ffa.zip';pack(decoded,invalid_sha,password)
reject('manifest_sha_mismatch_no_instance',invalid_sha,password,ValueError)
before=(runtime/'private/p2c-access.json').read_bytes()
try:migration.import_package(root,package,password,True)
except ValueError:mark('existing_target_preserved',(runtime/'private/p2c-access.json').read_bytes()==before)
else:raise AssertionError('Existing target accepted')
mark('real_qq_used',False)
passed=all(value for name,value in results.items() if name!='real_qq_used')
mark('passed',passed)
if not passed:raise SystemExit(1)
