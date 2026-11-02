param([string]$Output)
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Force -Path $Output|Out-Null
$manager=Join-Path $PSScriptRoot '../scripts/NapCat.ps1'
$node=Join-Path $Output 'napcat';$shell=Join-Path $Output 'shell'
New-Item -ItemType Directory -Path $node,$shell|Out-Null
'fixture'|Set-Content (Join-Path $Output 'node.exe');'fixture'|Set-Content (Join-Path $Output 'index.js')
function Get-Process {
 [CmdletBinding()]param([string[]]$Name)
 $global:observedName=$Name[0]
 # Discovery cannot authorize control over this unrelated system process.
 return [pscustomobject]@{Id=4}
}
$result=& $manager -Action Status -Root $node|ConvertFrom-Json
if($global:observedName -ne 'NapCatWinBootMain' -or $result.running){throw 'Incomplete parent files incorrectly select Node mode'}
'fixture'|Set-Content (Join-Path $Output 'wrapper.node');'{}'|Set-Content (Join-Path $Output 'package.json')
$result=& $manager -Action Status -Root $node|ConvertFrom-Json
if($global:observedName -ne 'node' -or $result.running){throw 'Node mode or unrelated-process filtering failed'}
$result=& $manager -Action Status -Root $shell|ConvertFrom-Json
if($global:observedName -ne 'NapCatWinBootMain' -or $result.running){throw 'Unrelated Node parent misclassified Shell root'}
. (Join-Path $PSScriptRoot '../scripts/Native-Process.ps1')
$self=Get-NativeProcessInfo $PID
if($self.ProcessId -ne $PID -or -not $self.CreationDate -or -not $self.ExecutablePath){throw 'Limited-rights identity query failed'}
if(Get-NativeProcessInfo $PID -ExpectedPath (Join-Path $Output 'unrelated.exe') -Scan){throw 'Wrong executable accepted'}
$unreadable=$false
try{Get-NativeProcessInfo 4|Out-Null}catch{$unreadable=$true}
if($unreadable){
 @{pid=4;created_ticks='1';qq='';runtime='node'}|ConvertTo-Json|Set-Content (Join-Path $node '.feiyu-process.json') -Encoding UTF8
 $refused=$false;try{& $manager -Action Status -Root $node|Out-Null}catch{$refused=$true}
 if(-not $refused){throw 'Unreadable recorded PID must fail closed'}
 Remove-Item -LiteralPath (Join-Path $node '.feiyu-process.json')
}
@{node_mode=$true;shell_mode=$true;unrelated_candidates_ignored=$true;pid_creation_path_preserved=$true;unreadable_recorded_pid_refused=$unreadable;sandboxie='not tested'}|ConvertTo-Json|Set-Content (Join-Path $Output 'results.json') -Encoding UTF8
'NapCat discovery passed'
