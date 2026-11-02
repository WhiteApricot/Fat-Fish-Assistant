param([Parameter(Mandatory=$true)][string]$Destination,[Parameter(Mandatory=$true)][int]$ControllerPid)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$target=[IO.Path]::GetFullPath($Destination)
. (Join-Path $PSScriptRoot 'Native-Process.ps1')
$all=@([FeiyuProcessSnapshot]::Read())
$excluded=@([int]$PID,$ControllerPid)
$cursor=$ControllerPid
while($cursor -gt 0){
 $info=$all|Where-Object {$_.ProcessId -eq $cursor}|Select-Object -First 1
 if(-not $info -or $info.ParentProcessId -eq 0 -or $info.ParentProcessId -in $excluded){break}
 $cursor=[int]$info.ParentProcessId;$excluded+=$cursor
}
$writers=@($all|Where-Object {$_.ProcessId -notin $excluded -and $_.Name -match '^python(?:w|3(?:\.\d+)?)?\.exe$'}|ForEach-Object {
 # Only Python candidates are inspected; no WMI or global command-line query.
 # Query failures retain the partial environment rather than guessing it is idle.
 $info=Get-NativeProcessInfo $_.ProcessId
 if($info){
  $line=[FeiyuIdentity]::CommandLine($_.ProcessId)
  $again=Get-NativeProcessInfo $_.ProcessId
  if(-not $again -or $again.CreationDate -ne $info.CreationDate){throw 'Build writer identity changed during query'}
  if($line.IndexOf($target,[StringComparison]::OrdinalIgnoreCase) -ge 0){$info}
 }
})
@{active_count=$writers.Count}|ConvertTo-Json -Compress
