param([Parameter(Mandatory=$true)][string]$FixtureRoot)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$fixture=[IO.Path]::GetFullPath($FixtureRoot)
if(Test-Path -LiteralPath $fixture){throw 'Use a new fixture directory'}
foreach($name in @('private','logs','engines/llama','fake-speech','foreign','models/sensevoice','release/src/p2d')){New-Item -ItemType Directory -Path (Join-Path $fixture $name) -Force|Out-Null}
$compiler=Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source=Join-Path $fixture 'Dummy.cs'
'using System.Threading;class Dummy { static void Main(){Thread.Sleep(120000);} }'|Set-Content -LiteralPath $source
$engine=Join-Path $fixture 'engines/llama/llama-server.exe'
$speech=Join-Path $fixture 'fake-speech/python.exe'
$foreignExe=Join-Path $fixture 'foreign/python.exe'
foreach($path in @($engine,$speech,$foreignExe)){
 & $compiler /nologo /target:exe (('/out:')+$path) $source
 if($LASTEXITCODE -ne 0){throw 'Synthetic compiler failed'}
}
$model=Join-Path $fixture 'models/sensevoice/synthetic.txt';'synthetic'|Set-Content -LiteralPath $model
$lock=Join-Path $fixture 'model-lock.json'
@(@{component='Embedding';path='engines/llama/llama-server.exe';sha256=(Get-FileHash -LiteralPath $engine).Hash.ToLowerInvariant()},@{component='Speech';path='models/sensevoice/synthetic.txt';sha256=(Get-FileHash -LiteralPath $model).Hash.ToLowerInvariant()})|ConvertTo-Json|Set-Content -LiteralPath $lock -Encoding UTF8
'synthetic-key'|Set-Content -LiteralPath (Join-Path $fixture 'private/local-embedding-key.txt')
@{release=(Join-Path $fixture 'release')}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $fixture 'instance.json') -Encoding UTF8
$manager=Join-Path $PSScriptRoot '..\scripts\Local-Models.ps1'
$foreign=$null
$owned=@()
$results=@{}
try{
 foreach($component in @('Embedding','Speech')){
  $listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0);$listener.Start();$freePort=$listener.LocalEndpoint.Port;$listener.Stop()
  & $manager -Action Start -Runtime $fixture -Component $component -SpeechPython $speech -ModelLock $lock -Port $freePort
  $pidFile=Join-Path $fixture ('private/'+$component+'.pid')
  $identityFile=Join-Path $fixture ('private/'+$component+'.process.json')
  $ownedPid=[int](Get-Content -LiteralPath $pidFile)
  $owned+=Get-Process -Id $ownedPid
  $identity=Get-Content -Raw -Encoding UTF8 -LiteralPath $identityFile|ConvertFrom-Json
  if($identity.instance -ne $fixture){throw 'Unicode process metadata did not round-trip'}
  $foreign=Start-Process -FilePath $foreignExe -ArgumentList ('"speech_server.py" --instance-runtime "'+$fixture+'-other"') -WindowStyle Hidden -PassThru
  $foreign.Id|Set-Content -LiteralPath $pidFile
  if($component -eq 'Speech'){Remove-Item -LiteralPath $identityFile} # Exercise the legacy PID-only exact-root fallback.
  $blocked=$false
  try{& $manager -Action Stop -Runtime $fixture -Component $component}catch{$blocked=$true}
  if(-not $blocked -or $foreign.HasExited -or -not(Get-Process -Id $ownedPid -ErrorAction SilentlyContinue)){throw 'Unrelated PID was adopted or a service was lost'}
  $ownedPid|Set-Content -LiteralPath $pidFile
  & $manager -Action Stop -Runtime $fixture -Component $component
  if(Get-Process -Id $ownedPid -ErrorAction SilentlyContinue){throw 'Owned service did not exit'}
  if(-not $foreign.HasExited){$foreign.Kill();$foreign.WaitForExit()};$foreign.Dispose();$foreign=$null
  $results[$component]=$true
 }
 $results.unicode_metadata=$true;$results.unrelated_processes_preserved=$true;$results.real_models_started=$false
 $results|ConvertTo-Json -Compress
}finally{
 if($foreign){if(-not $foreign.HasExited){$foreign.Kill();$foreign.WaitForExit()};$foreign.Dispose()}
 foreach($process in $owned){if(-not $process.HasExited){$process.Kill();$process.WaitForExit()};$process.Dispose()}
}
