param(
 [Parameter(Mandatory=$true)][ValidateSet('Start','Stop','Status','Diagnose')][string]$Action,
 [Parameter(Mandatory=$true)][string]$Root,
 [string]$QQExecutable,
 [string]$LockFile
)
$ErrorActionPreference='Stop'
if(-not $LockFile){$LockFile=Join-Path $PSScriptRoot '..\manifests\napcat-runtime-lock.json'}
$napRoot=(Resolve-Path -LiteralPath $Root).Path
$executable=Join-Path $napRoot 'NapCatWinBootMain.exe'
$recordPath=Join-Path $napRoot '.feiyu-process.json'
$running=@(Get-CimInstance Win32_Process -Filter "Name='NapCatWinBootMain.exe'"|Where-Object {$_.ExecutablePath -eq $executable})
if($Action -eq 'Status'){@{running=($running.Count -gt 0);managed=(Test-Path -LiteralPath $recordPath)}|ConvertTo-Json -Compress;exit 0}
if($Action -eq 'Stop'){
 if(-not(Test-Path -LiteralPath $recordPath)){throw 'No managed launch record; refusing to stop an unrelated launcher'}
 $record=Get-Content -Raw -LiteralPath $recordPath|ConvertFrom-Json
 $target=@($running|Where-Object {$_.ProcessId -eq $record.pid -and $_.CreationDate.ToUniversalTime().Ticks.ToString() -eq $record.created_ticks})
 if($target.Count -eq 1){
  $processes=@(Get-CimInstance Win32_Process);$parents=@([int]$record.pid);$children=@()
  do{
   $next=@($processes|Where-Object {$_.ParentProcessId -in $parents -and $_.ExecutablePath -eq $record.qq})
   $children+=@($next);$parents=@($next|ForEach-Object ProcessId)
  }while($parents.Count -gt 0)
  [array]::Reverse($children)
  foreach($child in $children){Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue}
  Stop-Process -Id $record.pid -ErrorAction SilentlyContinue
 }
 Remove-Item -LiteralPath $recordPath
 exit 0
}
$lock=Get-Content -Raw -LiteralPath $LockFile|ConvertFrom-Json
foreach($file in $lock.files){
 $path=Join-Path $napRoot $file.path
 if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256){throw 'NapCat differs from the pinned upstream package; refusing launch'}
}
if($Action -eq 'Diagnose'){'External pinned NapCat runtime integrity passed';exit 0}
if($running.Count -gt 0){throw 'External NapCat already running; refusing duplicate launch'}
$webuiPath=Join-Path $napRoot 'config\webui.json'
if(-not(Test-Path -LiteralPath $webuiPath)){throw 'Initialize owned NapCat loopback WebUI configuration before launch'}
$webui=Get-Content -Raw -LiteralPath $webuiPath|ConvertFrom-Json
if($webui.host -ne '127.0.0.1'){throw 'NapCat WebUI must bind loopback; refusing launch'}
if(-not $QQExecutable){throw 'Provide an explicit dedicated QQ executable; login remains manual'}
$qqPath=(Resolve-Path -LiteralPath $QQExecutable).Path
$values=@{NAPCAT_PATCH_PACKAGE=(Join-Path $napRoot 'qqnt.json');NAPCAT_LOAD_PATH=(Join-Path $napRoot 'loadNapCat.js');NAPCAT_INJECT_PATH=(Join-Path $napRoot 'NapCatWinBootHook.dll');NAPCAT_LAUNCHER_PATH=$executable;NAPCAT_MAIN_PATH=(Join-Path $napRoot 'napcat.mjs')}
$previous=@{}
try{
 foreach($name in $values.Keys){$previous[$name]=[Environment]::GetEnvironmentVariable($name,'Process');[Environment]::SetEnvironmentVariable($name,$values[$name],'Process')}
 $arguments='"'+$qqPath+'" "'+$values.NAPCAT_INJECT_PATH+'"'
 $process=Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $napRoot -WindowStyle Hidden -PassThru
 $info=Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)"
 @{pid=$process.Id;created_ticks=$info.CreationDate.ToUniversalTime().Ticks.ToString();qq=$qqPath}|ConvertTo-Json|Set-Content -LiteralPath $recordPath
}finally{foreach($name in $previous.Keys){[Environment]::SetEnvironmentVariable($name,$previous[$name],'Process')}}
'Launcher started; verify local health and complete QQ login manually if required.'
