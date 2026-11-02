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
New-Item -ItemType Directory -Path (Join-Path $fixture 'config')|Out-Null
@{host='127.0.0.1';port=16099}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $fixture 'config\webui.json')
# Simulate an inaccessible unrelated same-name candidate; actual identity is still queried natively.
function Get-Process {
 [CmdletBinding()]param([string[]]$Name,[int]$Id)
 if($PSBoundParameters.ContainsKey('Id')){return Microsoft.PowerShell.Management\Get-Process -Id $Id -ErrorAction SilentlyContinue}
 $found=@(Microsoft.PowerShell.Management\Get-Process -Name $Name -ErrorAction SilentlyContinue)
 return @([pscustomobject]@{Id=4})+$found
}
& $manager -Action Start -Root $fixture -QQExecutable (Join-Path $fixture 'QQ.exe') -LockFile (Join-Path $fixture 'lock.json')
. (Join-Path $PSScriptRoot ../scripts/Native-Process.ps1)
Start-Sleep -Milliseconds 500
$before=@(Get-Process -Name QQ,NapCatWinBootMain -ErrorAction SilentlyContinue|ForEach-Object {Get-NativeProcessInfo $_.Id -Scan} | Where-Object {$_.ExecutablePath -in @((Join-Path $fixture 'QQ.exe'),(Join-Path $fixture 'NapCatWinBootMain.exe'))})
if($before.Count -ne 2){throw 'Dummy parent/child startup failed'}
$duplicateRejected=$false
try{& $manager -Action Start -Root $fixture -QQExecutable (Join-Path $fixture 'QQ.exe') -LockFile (Join-Path $fixture 'lock.json')}catch{
 if($_.Exception.Message -notmatch 'already running'){throw}
 $duplicateRejected=$true
}
if(-not $duplicateRejected){throw 'Duplicate launch was not rejected'}
& $manager -Action Stop -Root $fixture
Start-Sleep -Milliseconds 500
$after=@(Get-Process -Name QQ,NapCatWinBootMain -ErrorAction SilentlyContinue|ForEach-Object {Get-NativeProcessInfo $_.Id -Scan} | Where-Object {$_.ExecutablePath -in @((Join-Path $fixture 'QQ.exe'),(Join-Path $fixture 'NapCatWinBootMain.exe'))})
if($after.Count -ne 0){throw 'Dummy parent/child shutdown failed'}
@{synthetic_launcher_start=$true;verified_parent_child_stop=$true;real_qq_launched=$false;real_napcat_launched=$false}|ConvertTo-Json -Compress
