param([Parameter(Mandatory=$true)][string]$FixtureRoot)
$ErrorActionPreference='Stop'
$fixture=[IO.Path]::GetFullPath($FixtureRoot)
if(Test-Path -LiteralPath $fixture){throw 'Use a new isolated directory'}
New-Item -ItemType Directory -Path (Join-Path $fixture 'private'),(Join-Path $fixture 'logs'),(Join-Path $fixture 'engines/llama'),(Join-Path $fixture 'models/embedding')|Out-Null
$compiler=Join-Path $env:SystemRoot 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
'using System.Threading;class Dummy { static void Main(){Thread.Sleep(120000);} }'|Set-Content (Join-Path $fixture 'Dummy.cs')
$exe=Join-Path $fixture 'engines/llama/llama-server.exe'
& $compiler /nologo /target:exe ('/out:'+$exe) (Join-Path $fixture 'Dummy.cs')
if($LASTEXITCODE){throw 'Compiler failed'}
'synthetic'|Set-Content (Join-Path $fixture 'models/embedding/bge-small-zh-v1.5-f16.gguf')
'synthetic-key'|Set-Content (Join-Path $fixture 'private/local-embedding-key.txt')
$lock=Join-Path $fixture 'lock.json'
@('engines/llama/llama-server.exe','models/embedding/bge-small-zh-v1.5-f16.gguf')|ForEach-Object {@{component='Embedding';path=$_;sha256=(Get-FileHash (Join-Path $fixture $_)).Hash.ToLowerInvariant()}}|ConvertTo-Json|Set-Content $lock
function Get-CimInstance {throw 'WMI must not be called'}
function Get-NetTCPConnection {throw 'WMI-based TCP cmdlet must not be called'}
$manager=Join-Path $PSScriptRoot '../scripts/Local-Models.ps1'
$receipt=Join-Path $fixture 'private/Embedding.process.json'
$original=$null
try{
 & $manager -Action Start -Runtime $fixture -ModelLock $lock -Port 16327
 if(-not (& $manager -Action Status -Runtime $fixture)){throw 'Status failed'}
 $original=[IO.File]::ReadAllText($receipt)
 $rejected=$false;try{& $manager -Action Start -Runtime $fixture -ModelLock $lock -Port 16327}catch{$rejected=$_.Exception.Message -match 'already running'}
 if(-not $rejected){throw 'Duplicate not refused'}
 $bad=$original|ConvertFrom-Json;$bad.created_ticks='1';$bad|ConvertTo-Json|Set-Content $receipt
 $refused=$false;try{& $manager -Action Stop -Runtime $fixture}catch{$refused=$_.Exception.Message -match 'identity mismatch'}
 if(-not $refused){throw 'Forged identity accepted'}
 [IO.File]::WriteAllText($receipt,$original)
 & $manager -Action Stop -Runtime $fixture
 if(& $manager -Action Status -Runtime $fixture){throw 'Stop failed'}
 @{native_start_status_stop=$true;duplicate_refused=$true;forged_receipt_refused=$true;wmi_disabled=$true}|ConvertTo-Json|Set-Content (Join-Path $fixture 'results.json')
 'LOCAL_MODELS_PASS'
}finally{
 if($original -and (Test-Path $receipt)){[IO.File]::WriteAllText($receipt,$original);& $manager -Action Stop -Runtime $fixture}
}
