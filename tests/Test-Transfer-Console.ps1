param([string]$Output)
$ErrorActionPreference='Stop'
if([Console]::IsOutputRedirected){throw 'Run this check in a real console/PTY'}
$project=Split-Path $PSScriptRoot -Parent
. (Join-Path $project 'scripts/Desktop-Interaction.ps1')
$script:desktopLog=$Output
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $project 'scripts/Desktop.ps1'),[ref]$null,[ref]$null)
$definition=$ast.EndBlock.Statements|Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq 'ShowProgress'}
. ([scriptblock]::Create($definition.Extent.Text))
for($i=0;$i -le 4;$i++){
 $frame=@{file=('synthetic-'+('long-name-'*12)+'.whl');source='trusted-mirror.example';bytes=$i*262144;total=1048576;eta_seconds=4-$i;done=($i -eq 4)}|ConvertTo-Json -Compress
 ShowProgress ('[Fat-Fish-Assistant] transfer: '+$frame)
 if([Console]::CursorTop -ne $script:transferTop+1){throw 'Download status exceeded two rows'}
 Start-Sleep -Milliseconds 150
}
ClearTransfer
if(@(Get-Content -LiteralPath $Output).Count -ne 5){throw 'Historical progress was not retained in the log'}
'TWO_LINE_PROGRESS_PASS'
