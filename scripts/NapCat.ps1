param(
 [Parameter(Mandatory=$true)][ValidateSet('Start','Stop','Status','Diagnose')][string]$Action,
 [Parameter(Mandatory=$true)][string]$Root,
 [string]$QQExecutable,
 [string]$LockFile
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'Process-Identity.ps1')
. (Join-Path $PSScriptRoot 'Native-Process.ps1')
if(-not $LockFile){$LockFile=Join-Path $PSScriptRoot '..\manifests\napcat-runtime-lock.json'}
$napRoot=(Resolve-Path -LiteralPath $Root).Path
$executable=Join-Path $napRoot 'NapCatWinBootMain.exe'
$nodeRoot=Split-Path $napRoot -Parent
$nodeMode=((Split-Path $napRoot -Leaf) -ieq 'napcat') -and (Test-Path -LiteralPath (Join-Path $nodeRoot 'wrapper.node')) -and (Test-Path -LiteralPath (Join-Path $nodeRoot 'package.json')) -and (Test-Path -LiteralPath (Join-Path $nodeRoot 'node.exe')) -and (Test-Path -LiteralPath (Join-Path $nodeRoot 'index.js'))
if($nodeMode){$executable=Join-Path $nodeRoot 'node.exe'}
$recordPath=Join-Path $napRoot '.feiyu-process.json'
# Serialize status/start/stop for this owned root, including launch record writes.
$guardPath=Join-Path $napRoot '.feiyu-process.lock'
$guard=[IO.File]::Open($guardPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try{
$processName=if($nodeMode){'node.exe'}else{'NapCatWinBootMain.exe'}
$running=@(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($processName)) -ErrorAction SilentlyContinue|ForEach-Object {Get-NativeProcessInfo $_.Id -ExpectedPath $executable -Scan}|Where-Object {$_.ExecutablePath -eq $executable})
if(Test-Path -LiteralPath $recordPath){
 $ownedRecord=Read-ManagedJson $recordPath
 # Re-query a recorded PID strictly, even if a discovery scan could not read it.
 $ownedInfo=Get-NativeProcessInfo ([int]$ownedRecord.pid)
 if($ownedInfo -and $ownedInfo.ExecutablePath -eq $executable -and [Math]::Abs([decimal]$ownedInfo.CreationDate.ToUniversalTime().Ticks-[decimal]$ownedRecord.created_ticks) -le 10){
  $running=@($running|Where-Object {$_.ProcessId -ne $ownedInfo.ProcessId})+@($ownedInfo)
 }
}
if($Action -eq 'Status'){@{running=($running.Count -gt 0);managed=(Test-Path -LiteralPath $recordPath)}|ConvertTo-Json -Compress;exit 0}
if($Action -eq 'Stop'){
 if(-not(Test-Path -LiteralPath $recordPath)){throw 'No managed launch record; refusing to stop an unrelated launcher'}
 $record=Read-ManagedJson $recordPath
 $target=@($running|Where-Object {$_.ProcessId -eq $record.pid -and [Math]::Abs([decimal]$_.CreationDate.ToUniversalTime().Ticks-[decimal]$record.created_ticks) -le 10})
 if($target.Count -eq 1){
  $processes=@([FeiyuProcessSnapshot]::Read());$parents=@([int]$record.pid);$children=@()
  do{
   $next=@($processes|Where-Object {$_.ParentProcessId -in $parents})
   $verified=@($next|ForEach-Object {Get-NativeProcessInfo $_.ProcessId $_.ParentProcessId}|Where-Object {$_.ExecutablePath -eq $record.qq -and $_.CreationDate -ge $target[0].CreationDate})
   $children+=@($verified);$parents=@($verified|ForEach-Object ProcessId)
  }while($parents.Count -gt 0)
  [array]::Reverse($children)
  foreach($child in $children){Stop-VerifiedProcess $child}
  Stop-VerifiedProcess $target[0]
 }
 Remove-Item -LiteralPath $recordPath
 exit 0
}
$lock=Get-Content -Raw -LiteralPath $LockFile|ConvertFrom-Json
foreach($file in $lock.files){
 $path=Join-Path $napRoot $file.path
 if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256){throw 'NapCat differs from the pinned upstream package; refusing launch'}
}
if($nodeMode){
 $nodeLock=Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '../manifests/napcat-node-lock.json')|ConvertFrom-Json
 foreach($file in $nodeLock.files){
  if((Get-FileHash -LiteralPath (Join-Path $nodeRoot $file.path) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256){throw 'Independent QQ runtime differs from the pinned package; refusing launch'}
 }
}
if($Action -eq 'Diagnose'){'External pinned NapCat runtime integrity passed';exit 0}
if($running.Count -gt 0){throw 'External NapCat already running; refusing duplicate launch'}
$webuiPath=Join-Path $napRoot 'config\webui.json'
if(-not(Test-Path -LiteralPath $webuiPath)){throw 'Initialize owned NapCat loopback WebUI configuration before launch'}
$webui=Get-Content -Raw -LiteralPath $webuiPath|ConvertFrom-Json
if($webui.host -ne '127.0.0.1'){throw 'NapCat WebUI must bind loopback; refusing launch'}
if($nodeMode){
 $logRoot=Join-Path $napRoot 'logs';New-Item -ItemType Directory -Force -Path $logRoot|Out-Null
 $logTag=Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
 $arguments='"'+(Join-Path $nodeRoot 'index.js')+'"'
 $savedEnvironment=@{}
 try{
  # Supported upstream mode: one owned writer, no restart supervisor left behind.
  $launchEnvironment=@{NAPCAT_DISABLE_MULTI_PROCESS='1';NAPCAT_QUICK_ACCOUNT=$null;NAPCAT_QUICK_PASSWORD=$null;NAPCAT_QUICK_PASSWORD_MD5=$null;ACCOUNT=$null}
  foreach($name in $launchEnvironment.Keys){$savedEnvironment[$name]=[Environment]::GetEnvironmentVariable($name,'Process');[Environment]::SetEnvironmentVariable($name,$launchEnvironment[$name],'Process')}
  $process=Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory ([WildcardPattern]::Escape($nodeRoot)) -WindowStyle Hidden -PassThru -RedirectStandardOutput ([WildcardPattern]::Escape((Join-Path $logRoot ($logTag+'-stdout.log')))) -RedirectStandardError ([WildcardPattern]::Escape((Join-Path $logRoot ($logTag+'-stderr.log'))))
 }finally{foreach($name in $savedEnvironment.Keys){[Environment]::SetEnvironmentVariable($name,$savedEnvironment[$name],'Process')}}
 $info=Get-NativeProcessInfo $process.Id
 if(-not $info){throw ('Independent QQ runtime exited; see '+(Join-Path $logRoot ($logTag+'-stderr.log')))}
 @{pid=$process.Id;created_ticks=$info.CreationDate.ToUniversalTime().Ticks.ToString();qq='';runtime='node'}|ConvertTo-Json|Set-Content -LiteralPath $recordPath -Encoding UTF8
 'Independent QQ runtime started; complete QR login manually in the local WebUI.'
 exit 0
}
if(-not $QQExecutable){throw 'Provide an explicit dedicated QQ executable; login remains manual'}
$qqPath=(Resolve-Path -LiteralPath $QQExecutable).Path
$values=@{NAPCAT_PATCH_PACKAGE=(Join-Path $napRoot 'qqnt.json');NAPCAT_LOAD_PATH=(Join-Path $napRoot 'loadNapCat.js');NAPCAT_INJECT_PATH=(Join-Path $napRoot 'NapCatWinBootHook.dll');NAPCAT_LAUNCHER_PATH=$executable;NAPCAT_MAIN_PATH=(Join-Path $napRoot 'napcat.mjs')}
$previous=@{}
try{
 foreach($name in $values.Keys){$previous[$name]=[Environment]::GetEnvironmentVariable($name,'Process');[Environment]::SetEnvironmentVariable($name,$values[$name],'Process')}
 $arguments='"'+$qqPath+'" "'+$values.NAPCAT_INJECT_PATH+'"'
 $process=Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory ([WildcardPattern]::Escape($napRoot)) -WindowStyle Hidden -PassThru
 $info=Get-NativeProcessInfo $process.Id
 if(-not $info){throw 'Launcher exited before its process identity could be recorded'}
 @{pid=$process.Id;created_ticks=$info.CreationDate.ToUniversalTime().Ticks.ToString();qq=$qqPath}|ConvertTo-Json|Set-Content -LiteralPath $recordPath -Encoding UTF8
}finally{foreach($name in $previous.Keys){[Environment]::SetEnvironmentVariable($name,$previous[$name],'Process')}}
'Launcher started; verify local health and complete QQ login manually if required.'

}finally{$guard.Dispose()}
