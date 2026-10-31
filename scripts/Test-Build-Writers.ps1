param([Parameter(Mandatory=$true)][string]$Destination,[Parameter(Mandatory=$true)][int]$ControllerPid)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$target=[IO.Path]::GetFullPath($Destination)
$all=@(Get-CimInstance Win32_Process)
$excluded=@([int]$PID,$ControllerPid)
$cursor=$ControllerPid
while($cursor -gt 0){
 $info=$all|Where-Object {$_.ProcessId -eq $cursor}|Select-Object -First 1
 if(-not $info -or $info.ParentProcessId -eq 0 -or $info.ParentProcessId -in $excluded){break}
 $cursor=[int]$info.ParentProcessId;$excluded+=$cursor
}
$writers=@($all|Where-Object {$_.ProcessId -notin $excluded -and $_.Name -match '^python(?:w|3(?:\.\d+)?)?\.exe$' -and $_.CommandLine -and $_.CommandLine.IndexOf($target,[StringComparison]::OrdinalIgnoreCase) -ge 0})
@{active_count=$writers.Count}|ConvertTo-Json -Compress
