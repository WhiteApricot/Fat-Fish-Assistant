"""Headless launcher transport/CMD verification; does not click or automate any GUI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'scripts'))
import controller as c
from instance import protect_boundary
from migration_package import pack

p=argparse.ArgumentParser();p.add_argument('--software-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--launch',action='store_true');a=p.parse_args()
software=a.software_root.resolve();output=a.output.resolve()
for path in [software,output]:path.relative_to(PROJECT.parent/'Fat-Fish-Assistant.local/ux-validation')
if output.exists():raise ValueError('Use a new test output directory')
root=c.owned_root(output,True);protect_boundary(root)
record=c.state(software);python=c.chat_python(software,record)
c.atomic(root/'installation.json',{'schema_version':1,'project':'Fat-Fish-Assistant','chat_python':str(python),'components':{}})
snapshot=root/'snapshot';snapshot.mkdir();(snapshot/'manifest.json').write_text('{"schema":999}')
package=root/'synthetic-incompatible.ffa.zip';password='合成口令-用于验证隐藏输入传输'
pack(snapshot,package,password)
harness=root/'launcher-harness.ps1'
harness.write_text(r'''param([string]$Desktop,[string]$Root,[string]$Package,[string]$Python)
$ErrorActionPreference='Stop'
$project=Split-Path (Split-Path $Desktop -Parent) -Parent
. (Join-Path $project 'scripts/Desktop-Interaction.ps1')
$script:desktopLog=Join-Path $Root 'Desktop-test.log'
'synthetic'|Set-Content $script:desktopLog
$ast=[Management.Automation.Language.Parser]::ParseFile($Desktop,[ref]$null,[ref]$null)
foreach($definition in $ast.EndBlock.Statements){
 if($definition -is [Management.Automation.Language.FunctionDefinitionAst]){. ([scriptblock]::Create($definition.Extent.Text))}
}
$payload=[Console]::In.ReadToEnd()|ConvertFrom-Json
$check=Api @('check')
if(-not $check.ready){throw 'Preflight did not report ready'}
$progress=RunJson $Python @('-c',"import sys,json; print('[Fat-Fish-Assistant] synthetic progress',file=sys.stderr,flush=True); print(json.dumps({'ok':True,'code':0,'data':{'transport':True},'errors':[]}))") $null
if(-not $progress.transport){throw 'Progress transport failed'}
$matched=$false
try{SecretApi @('migration-import','--package',$Package,'--confirm-source-offline') @{password=$payload.password}|Out-Null}
catch{if($_.Exception.Message.Contains('Unsupported snapshot schema')){$matched=$true}else{throw}}
if(-not $matched){throw 'Unicode password transport or schema rejection failed'}
$recordFile=Join-Path $Root 'installation.json';$originalRecord=[IO.File]::ReadAllText($recordFile)
$service=Join-Path $Root 'synthetic-napcat/config';New-Item -ItemType Directory -Force -Path $service|Out-Null
$localToken='synthetic-local-token-not-api-key'
@{host='127.0.0.1';port=16099;token=$localToken}|ConvertTo-Json|Set-Content -Encoding UTF8 -LiteralPath (Join-Path $service 'webui.json')
@{instance='instance';components=@{napcat=@{installed=$true;path='synthetic-napcat'}}}|ConvertTo-Json -Depth 5|Set-Content -Encoding UTF8 -LiteralPath $recordFile
$script:opened=@()
$script:loginChecks=0;$script:loginRestarts=0
function Start-Process([string]$FilePath){$script:opened+=$FilePath}
function Api([string[]]$Arguments){
 if($Arguments[0] -eq 'daily-start'){return @{warnings=@();connection=@{qq_login=$false}}}
 if($Arguments[0] -eq 'config'){return @{dashboard=@{port=16185};dashboard_password_file='synthetic-password-location'}}
 if($Arguments[2] -eq 'login-status'){
  $script:loginChecks++
  if($script:loginChecks -eq 1){return @{isLogin=$false;coreReady=$false;isOffline=$false;loginPhase='reconnecting'}}
  return @{isLogin=$true;coreReady=$true;isOffline=$false;loginPhase='ready'}
 }
 if($Arguments[2] -eq 'recover-login'){$script:loginRestarts++;return @{restarted=$true}}
 if($Arguments[1] -eq 'astrbot'){return @{onebot_connection=@{connected=$true}}}
 return @{healthy=$true}
}
try{Launch}finally{[IO.File]::WriteAllText($recordFile,$originalRecord)}
if($script:opened -notcontains ('http://127.0.0.1:16099/webui/web_login?token='+$localToken)){throw 'NapCat login URL must include the pinned /webui/ basename and token'}
if($script:loginRestarts -ne 1){throw 'The launcher did not request exactly one controlled login recovery'}
@{json_api=$true;stderr_progress=$true;unicode_secret_stdin=$true;no_gui_automation=$true;local_login_token_autofill=$true;controlled_login_recovery=$true}|ConvertTo-Json -Compress
''',encoding='utf-8')
environment=c.clean_environment();environment['TEMP']=environment['TMP']=str(root)
# Reproduce the manual install failure: `show` must never bind to -Python.
direct=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT/'Fat-Fish-Assistant.ps1'),'-Root',str(software),'-Json','-NonInteractive','config','show'],capture_output=True,text=True,encoding='utf-8',errors='replace',env=environment,timeout=180)
(root/'direct-wrapper.log').write_text(direct.stdout+'\n'+direct.stderr,encoding='utf-8')
assert direct.returncode==0,direct.stdout+direct.stderr
assert json.loads(direct.stdout.splitlines()[-1])['data']['identity_valid']
proc=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(harness),'-Desktop',str(PROJECT/'scripts/Desktop.ps1'),'-Root',str(root),'-Package',str(package),'-Python',str(python)],input=json.dumps({'password':password}),text=True,encoding='utf-8',errors='replace',capture_output=True,env=environment,timeout=180)
(root/'transport.log').write_text(proc.stdout+'\n'+proc.stderr,encoding='utf-8')
assert proc.returncode==0,(proc.stdout,proc.stderr)
assert password not in proc.stdout+proc.stderr
assert 'synthetic-local-token-not-api-key' not in proc.stdout+proc.stderr
assert not (root/'instance').exists()
results=json.loads(proc.stdout.splitlines()[-1])
results['direct_config_subcommand_binding']=True
if a.launch:
    assert c.read(c.instance(software,record)/'private/instance-identity.json')['owner']=='100'
    assert not record['components'].get('napcat',{}).get('installed')
    try:
        launch=subprocess.run(['cmd.exe','/d','/c',str(PROJECT/'Start-Fat-Fish-Assistant.cmd'),'-Root',str(software),'-NoBrowser'],input='\n',text=True,encoding='utf-8',errors='replace',capture_output=True,env=environment,timeout=900)
        (root/'cmd-start.log').write_text(launch.stdout+'\n'+launch.stderr,encoding='utf-8')
        assert launch.returncode==0,launch.stdout+launch.stderr
        results['actual_cmd_start']=True
    finally:
        stop=subprocess.run([str(python),'-B',str(PROJECT/'ffa.py'),'--root',str(software),'stop'],capture_output=True,text=True,env=environment,timeout=180)
        (root/'cmd-stop.log').write_text(stop.stdout+'\n'+stop.stderr,encoding='utf-8')
        assert stop.returncode==0,stop.stderr
(root/'results.json').write_text(json.dumps(results,indent=2));print(json.dumps(results))
