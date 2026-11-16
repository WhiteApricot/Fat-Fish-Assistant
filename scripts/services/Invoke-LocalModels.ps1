param(
 [Parameter(Mandatory=$true)][ValidateSet('Start','Stop','Status')][string]$Action,
 [Parameter(Mandatory=$true)][string]$Runtime,
 [ValidateSet('Embedding')][string]$Component='Embedding',
 [string]$ModelLock,
 [ValidateRange(0,65535)][int]$Port=0
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
. (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts/process/Import-ProcessIdentity.ps1')
. (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts/process/Import-NativeProcess.ps1')
if(-not $ModelLock){$ModelLock=Join-Path $PSScriptRoot '..\..\manifests\model-runtime-lock.json'}
$instanceRoot=(Resolve-Path -LiteralPath $Runtime).Path
# Serialize only this instance/component, including ownership registration.
$controlLock=[IO.File]::Open((Join-Path $instanceRoot ('private\'+$Component+'.control.lock')),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try{
$pidFile=Join-Path $instanceRoot ("private\"+$Component+'.pid')
$identityFile=Join-Path $instanceRoot ("private\"+$Component+'.process.json')
$identity=if(Test-Path -LiteralPath $identityFile){Read-ManagedJson $identityFile}else{$null}
$existing=$null
if((Test-Path -LiteralPath $pidFile) -or $identity){
 $servicePid=if(Test-Path -LiteralPath $pidFile){[int](Get-Content -LiteralPath $pidFile)}else{[int]$identity.pid}
 $existing=Get-NativeProcessInfo $servicePid
 if($existing){
  # No global scan or unreadable-PID bypass. Old incomplete receipts require
  # operator review rather than guessing instance ownership from an exe name.
  if(-not $identity -or -not $identity.executable -or $identity.pid -ne $servicePid -or $identity.instance -ne $instanceRoot -or [Math]::Abs([decimal]$identity.created_ticks-[decimal]$existing.CreationDate.ToUniversalTime().Ticks) -gt 10 -or $identity.executable -ne $existing.ExecutablePath){throw 'Recorded process creation identity mismatch; refusing process control'}
  if($Component -eq 'Embedding' -and $existing.ExecutablePath -ne (Join-Path $instanceRoot 'engines\llama\llama-server.exe')){throw 'Embedding process identity mismatch'}
 }
}
if($Action -eq 'Status'){[bool]$existing;exit 0}
if($Action -eq 'Stop'){
 if($existing){
  Stop-VerifiedProcess $existing
 }
 if(Test-Path -LiteralPath $pidFile){Remove-Item -LiteralPath $pidFile}
 if(Test-Path -LiteralPath $identityFile){Remove-Item -LiteralPath $identityFile}
 exit 0
}
if($existing){throw 'Service already running'}
$lockedFiles=@((Get-Content -Raw -LiteralPath $ModelLock|ConvertFrom-Json)|Where-Object {$_.component -eq $Component})
if($lockedFiles.Count -eq 0){throw 'No locked artifacts for component'}
foreach($artifact in $lockedFiles){
 $artifactPath=[IO.Path]::GetFullPath((Join-Path $instanceRoot $artifact.path))
 if(-not $artifactPath.StartsWith($instanceRoot+[IO.Path]::DirectorySeparatorChar)){throw 'Artifact path escapes instance'}
 if((Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $artifact.sha256){throw 'Model or engine integrity mismatch; refusing launch'}
}
$listenPort=if($Port -gt 0){$Port}else{6203}
# Binding the exact loopback endpoint avoids WMI and global network-table queries.
$probe=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,$listenPort)
$probe.Server.ExclusiveAddressUse=$true
try{$probe.Start()}catch{throw ('Cannot reserve local model port; occupied or unavailable: '+$listenPort)}finally{$probe.Stop()}
$executable=Join-Path $instanceRoot 'engines\llama\llama-server.exe'
 $model=Join-Path $instanceRoot 'models\embedding\bge-small-zh-v1.5-f16.gguf'
 $key=Join-Path $instanceRoot 'private\local-embedding-key.txt'
 if(-not(Test-Path -LiteralPath $key)){throw 'Initialize a private embedding key before launch'}
 $arguments="-m `"$model`" --embedding --pooling cls --host 127.0.0.1 --port $listenPort --threads 1 --threads-batch 1 --parallel 1 --batch-size 512 --ubatch-size 512 --ctx-size 512 --sleep-idle-seconds 20 --api-key-file `"$key`" --alias feiyu-bge-small-zh"
 $outputLog=[WildcardPattern]::Escape((Join-Path $instanceRoot ("logs\"+$Component+'.stdout.log')))
 $errorLog=[WildcardPattern]::Escape((Join-Path $instanceRoot ("logs\"+$Component+'.stderr.log')))
 $process=Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory ([WildcardPattern]::Escape($instanceRoot)) -WindowStyle Hidden -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog -PassThru
 $info=$null
 for($attempt=0;$attempt -lt 40;$attempt++){
  if($process.HasExited){throw 'Service exited before process ownership could be recorded'}
  $info=Get-NativeProcessInfo $process.Id
  if($info -and $info.CreationDate -and $info.ExecutablePath){break}
  Start-Sleep -Milliseconds 100
 }
 if(-not $info -or -not $info.CreationDate -or -not $info.ExecutablePath){throw 'Cannot verify new service process identity; inspect its logs before retry'}
 if($info.ExecutablePath -ne $executable -or [Math]::Abs([decimal]$process.StartTime.ToUniversalTime().Ticks-[decimal]$info.CreationDate.ToUniversalTime().Ticks) -gt 10){throw 'New service identity differs from the launched process; refusing registration'}
 $pending=$identityFile+'.pending'
 @{schema=1;pid=$process.Id;created_ticks=$info.CreationDate.ToUniversalTime().Ticks.ToString();instance=$instanceRoot;executable=$executable}|ConvertTo-Json|Set-Content -LiteralPath $pending -Encoding UTF8
 Move-Item -LiteralPath $pending -Destination $identityFile -Force
 $process.Id|Set-Content -LiteralPath $pidFile
'Launch requested; validate authenticated model identity before using it.'

}finally{$controlLock.Dispose()}
