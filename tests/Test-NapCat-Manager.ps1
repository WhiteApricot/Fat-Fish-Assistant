param([Parameter(Mandatory=$true)][string]$FixtureRoot)
$ErrorActionPreference='Stop'
$fixture=[IO.Path]::GetFullPath($FixtureRoot)
if(Test-Path -LiteralPath $fixture){throw 'Use a new fixture directory'}
New-Item -ItemType Directory -Path $fixture | Out-Null
$compiler=Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if(-not(Test-Path -LiteralPath $compiler)){throw 'Framework compiler unavailable; fixture cannot run'}
$qqSource='using System.Threading;class DummyQQ { static void Main(){Thread.Sleep(120000);} }'
$bootSource='using System.Diagnostics;class DummyBoot { static void Main(string[] a){var p=Process.Start(new ProcessStartInfo(a[0]){UseShellExecute=false,CreateNoWindow=true});p.WaitForExit();} }'
$qqSource|Set-Content -LiteralPath (Join-Path $fixture 'DummyQQ.cs')
$bootSource|Set-Content -LiteralPath (Join-Path $fixture 'DummyBoot.cs')
& $compiler /nologo /target:exe (('/out:')+(Join-Path $fixture 'QQ.exe')) (Join-Path $fixture 'DummyQQ.cs')
if($LASTEXITCODE -ne 0){throw 'Dummy QQ compilation failed'}
& $compiler /nologo /target:exe (('/out:')+(Join-Path $fixture 'NapCatWinBootMain.exe')) (Join-Path $fixture 'DummyBoot.cs')
if($LASTEXITCODE -ne 0){throw 'Dummy launcher compilation failed'}
foreach($name in @('napcat.mjs','loadNapCat.js','NapCatWinBootHook.dll','qqnt.json')){'dummy fixture only'|Set-Content -LiteralPath (Join-Path $fixture $name)}
$files=@('napcat.mjs','loadNapCat.js','NapCatWinBootMain.exe','NapCatWinBootHook.dll','qqnt.json')|ForEach-Object {@{path=$_;sha256=(Get-FileHash -LiteralPath (Join-Path $fixture $_)).Hash.ToLowerInvariant()}}
@{version='synthetic-process-fixture';files=@($files)}|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $fixture 'lock.json')
$manager=Join-Path $PSScriptRoot '..\scripts\NapCat.ps1'
& $manager -Action Start -Root $fixture -QQExecutable (Join-Path $fixture 'QQ.exe') -LockFile (Join-Path $fixture 'lock.json')
Start-Sleep -Milliseconds 500
$before=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -in @((Join-Path $fixture 'QQ.exe'),(Join-Path $fixture 'NapCatWinBootMain.exe'))})
if($before.Count -ne 2){throw 'Dummy parent/child startup failed'}
& $manager -Action Stop -Root $fixture
Start-Sleep -Milliseconds 500
$after=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -in @((Join-Path $fixture 'QQ.exe'),(Join-Path $fixture 'NapCatWinBootMain.exe'))})
if($after.Count -ne 0){throw 'Dummy parent/child shutdown failed'}
@{synthetic_launcher_start=$true;verified_parent_child_stop=$true;real_qq_launched=$false;real_napcat_launched=$false}|ConvertTo-Json -Compress
