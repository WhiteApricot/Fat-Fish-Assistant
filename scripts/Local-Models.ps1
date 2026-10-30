param(
 [Parameter(Mandatory=$true)][ValidateSet('Start','Stop','Status')][string]$Action,
 [Parameter(Mandatory=$true)][string]$Runtime,
 [ValidateSet('Embedding','Speech')][string]$Component='Embedding',
 [string]$SpeechPython,
 [string]$ModelLock,
 [ValidateRange(0,65535)][int]$Port=0
)
$ErrorActionPreference='Stop'
if(-not $ModelLock){$ModelLock=Join-Path $PSScriptRoot '..\manifests\model-runtime-lock.json'}
$instanceRoot=(Resolve-Path -LiteralPath $Runtime).Path
$pidFile=Join-Path $instanceRoot ("private\"+$Component+'.pid')
$existing=$null
if(Test-Path -LiteralPath $pidFile){
 $servicePid=[int](Get-Content -LiteralPath $pidFile)
 $existing=Get-CimInstance Win32_Process -Filter "ProcessId=$servicePid"
 if($existing -and $existing.CommandLine -notlike "*$instanceRoot*"){throw 'PID ownership mismatch; refusing process control'}
 if($existing -and $Component -eq 'Embedding' -and $existing.ExecutablePath -ne (Join-Path $instanceRoot 'engines\llama\llama-server.exe')){throw 'Embedding process identity mismatch'}
 if($existing -and $Component -eq 'Speech' -and $existing.CommandLine -notlike '*speech_server.py*'){throw 'Speech process identity mismatch'}
}
if($Action -eq 'Status'){[bool]$existing;exit 0}
if($Action -eq 'Stop'){
 if($existing){
  $children=@(Get-CimInstance Win32_Process | Where-Object {$_.ParentProcessId -eq $existing.ProcessId -and $_.CommandLine -like "*$instanceRoot*"})
  foreach($child in $children){Stop-Process -Id $child.ProcessId -ErrorAction Stop}
  Stop-Process -Id $existing.ProcessId -ErrorAction SilentlyContinue
 }
 if(Test-Path -LiteralPath $pidFile){Remove-Item -LiteralPath $pidFile}
 exit 0
}
if($existing){throw 'Service already running'}
$lockedFiles=@(Get-Content -Raw -LiteralPath $ModelLock|ConvertFrom-Json|Where-Object component -eq $Component)
if($lockedFiles.Count -eq 0){throw 'No locked artifacts for component'}
foreach($artifact in $lockedFiles){
 $artifactPath=[IO.Path]::GetFullPath((Join-Path $instanceRoot $artifact.path))
 if(-not $artifactPath.StartsWith($instanceRoot+[IO.Path]::DirectorySeparatorChar)){throw 'Artifact path escapes instance'}
 if((Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $artifact.sha256){throw 'Model or engine integrity mismatch; refusing launch'}
}
$listenPort=if($Port -gt 0){$Port}elseif($Component -eq 'Embedding'){6203}else{6202}
if(Get-NetTCPConnection -State Listen -LocalPort $listenPort -ErrorAction SilentlyContinue){throw 'Port occupied; refusing duplicate or unrelated service'}
if($Component -eq 'Embedding'){
 $executable=Join-Path $instanceRoot 'engines\llama\llama-server.exe'
 $model=Join-Path $instanceRoot 'models\embedding\bge-small-zh-v1.5-f16.gguf'
 $key=Join-Path $instanceRoot 'private\local-embedding-key.txt'
 if(-not(Test-Path -LiteralPath $key)){throw 'Initialize a private embedding key before launch'}
 $arguments="-m `"$model`" --embedding --pooling cls --host 127.0.0.1 --port $listenPort --threads 1 --threads-batch 1 --parallel 1 --batch-size 512 --ubatch-size 512 --ctx-size 512 --sleep-idle-seconds 20 --api-key-file `"$key`" --alias feiyu-bge-small-zh"
}else{
 if(-not $SpeechPython){throw 'Specify the independent speech Python executable'}
 $executable=$SpeechPython
 $release=(Get-Content -Raw -LiteralPath (Join-Path $instanceRoot 'instance.json')|ConvertFrom-Json).release
 $script=Join-Path $release 'src\p2d\speech_server.py'
 $arguments="-u `"$script`" --instance-runtime `"$instanceRoot`""
}
$previousRoot=$env:FEIYU_RUNTIME_ROOT
$previousSpeechPort=$env:FEIYU_SPEECH_PORT
try{
 $env:FEIYU_RUNTIME_ROOT=$instanceRoot
 $env:FEIYU_SPEECH_PORT=[string]$listenPort
 $process=Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $instanceRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $instanceRoot ("logs\"+$Component+'.stdout.log')) -RedirectStandardError (Join-Path $instanceRoot ("logs\"+$Component+'.stderr.log')) -PassThru
 $process.Id|Set-Content -LiteralPath $pidFile
}finally{$env:FEIYU_RUNTIME_ROOT=$previousRoot;$env:FEIYU_SPEECH_PORT=$previousSpeechPort}
'Launch requested; validate authenticated model identity before using it.'
